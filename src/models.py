"""Data model for product intelligence.

One definition, three uses: this file is the JSON schema handed to Claude for
structured output, the validation layer for anything we persist, AND the shape
the API/CLI returns. A parallel type definition anywhere else will drift from
this one within a day. Don't add one.

The central design decision lives in `Sourced`: an enriched value is never a bare
string, it is a value bundled with where it came from and how sure we are. That
makes the "every field carries provenance" rule structural rather than a
convention someone has to remember.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Where a value came from, ordered roughly most trustworthy to least.
# `input` and `document` are grounded in text we were actually given;
# `inference` is the model reasoning from product knowledge; `web` is external.
Source = Literal["input", "document", "inference", "web"]

# Below this, a field is shown as a gap rather than an answer. Tuned by hand
# against the golden set — it is a product decision, not a mathematical one.
CONFIDENCE_FLOOR = 0.45


class Sourced(BaseModel):
    """A single enriched value plus its justification.

    `value` is deliberately nullable: returning null with a reason is a correct
    answer, and is what we want whenever the record gives no grounds for a
    guess. A confident fabricated spec is worse than an admitted gap.
    """

    # Deliberately NOT `extra: "forbid"`. That makes pydantic emit
    # `additionalProperties: false`, which Anthropic requires but Gemini rejects
    # outright with a 400. Providers that need the closed-object marker add it
    # themselves in src/llm.py; keeping it out of the shared model is what lets
    # one schema serve all three. Ignoring an unexpected extra key is also the
    # more forgiving failure mode for model output.

    value: str | None = Field(description="The value, or null if it cannot be grounded.")
    unit: str | None = Field(default=None, description="Unit symbol, e.g. 'mm', 'V', 'kg'.")
    source: Source = Field(description="Where this value came from.")
    evidence: str = Field(
        description="Quote from the input, or the reasoning. If value is null, the reason it is null."
    )
    confidence: float = Field(description="0.0-1.0. Be honest; low confidence is useful signal.")

    @property
    def is_grounded(self) -> bool:
        """True when we have a value we would actually stand behind."""
        return self.value is not None and self.confidence >= CONFIDENCE_FLOOR


def not_generated(field: str) -> Sourced:
    """An explicitly-absent value, for a field no run has produced yet.

    Confidence 0.0 keeps it below the floor, so it is never emitted and never
    counted as grounded. The reason is spelled out in `evidence` rather than
    left blank, because "the pipeline never generated this" and "the record did
    not support it" are different facts and a reviewer needs to tell them apart.
    """
    return Sourced(value=None, source="input", confidence=0.0,
                   evidence=f"Not generated: this catalog record predates the {field} field.")


class Spec(Sourced):
    """A technical attribute. Same provenance contract, plus a name.

    Specs are open-ended (voltage, thread pitch, IP rating, ...) so they can't be
    fixed fields on Product — hence a list of named values.
    """

    name: str = Field(description="Attribute name, lowercase, e.g. 'operating voltage'.")


class RawRecord(BaseModel):
    """A product before any AI touches it: whatever the source file gave us."""

    sku: str
    attributes: dict[str, str] = Field(default_factory=dict)
    text: str = Field(default="", description="Free-text blob: description, datasheet excerpt, etc.")
    # The source row exactly as read, before aliasing or placeholder stripping.
    # The delivery format passes several distributor columns straight through,
    # and they must survive verbatim — normalization is for what we reason over,
    # not for what we hand back. Excluded from `as_prompt_block`, so the model
    # still only ever sees the cleaned view.
    raw: dict[str, str] = Field(default_factory=dict)
    # Manufacturer documentation retrieved for this record, and where from.
    # Kept separate from `text` on purpose: `text` is the distributor's own
    # blurb, this is the manufacturer speaking, and the two carry different
    # provenance (`input` vs `document`). Merging them would make the model
    # guess which it was reading — and `checks.unsupported_input_claims`
    # treats everything in the prompt block as the record, so a document's
    # contents would start counting as things "the input said".
    document: str = Field(default="")
    document_source: str = Field(default="")

    def as_input_block(self) -> str:
        """The distributor's record alone — no retrieved documentation.

        Exists so `checks.unsupported_input_claims` can ask "did *the record*
        say this?" and get an honest answer. Once a document joins the prompt,
        a haystack built from the whole prompt would quietly accept a value the
        manufacturer's page stated as though the input had stated it, which is
        the precise claim that check exists to falsify.
        """
        lines = [f"SKU: {self.sku}"]
        lines += [f"{k}: {v}" for k, v in self.attributes.items() if v]
        if self.text:
            lines.append(f"Free text: {self.text}")
        return "\n".join(lines)

    def as_prompt_block(self) -> str:
        """Flatten to the text we actually show the model. Kept here so the
        prompt and the data model can't disagree about what the model saw."""
        lines = [self.as_input_block()]
        if self.document:
            # Fenced and labelled so the model can tell manufacturer
            # documentation from the distributor's row, and cite the source in
            # its evidence rather than claiming the record stated it.
            lines.append(
                f'<document source="{self.document_source}">\n'
                f"{self.document}\n"
                f"</document>"
            )
        return "\n".join(lines)


class Product(BaseModel):
    """The enriched, commerce-ready record. This is what Claude fills in."""

    # See the note on Sourced: closed-object markers are added per provider.

    sku: str = Field(description="Copy the SKU through unchanged.")
    name: Sourced = Field(description="Item type only, e.g. 'Dishwasher', 'Ball Bearing'.")
    brand: Sourced = Field(description="Manufacturer or brand.")
    category: Sourced = Field(
        description="Classpath, three levels separated by ' > ', "
                    "e.g. 'Appliances & Consumer Electronics > Kitchen Appliances > Built-In Dishwashers'.")
    description: Sourced = Field(description="Long description. Full sentence-case detail.")
    specs: list[Spec] = Field(description="Technical attributes. Only what you can ground.")

    # --- the delivery format's five description variants -------------------
    # Unilog rewrites the same product at five lengths and casings — till
    # receipt, mobile app, search result, product page, marketing copy. The
    # brief calls getting these right "most of the task", and each has its own
    # hard rule, so they are separate fields with separate evidence rather than
    # one description the exporter truncates. Truncating would produce text that
    # is the right length and the wrong sentence.
    # Defaulted rather than required, for one practical reason: records enriched
    # before these fields existed must still load. A schema change that bricks
    # the demo catalog is a worse failure than a variant the model skipped, and
    # the prompt asks for all five explicitly either way.
    invoice_desc: Sourced = Field(
        default_factory=lambda: not_generated("invoice_desc"),
        description="<=40 chars, ALL CAPS, abbreviated. e.g. "
                    "'DISHWASHER LEG 5 SST 120V 15A 50-1/4IN'. Null if not derivable.")
    mobile_desc: Sourced = Field(
        default_factory=lambda: not_generated("mobile_desc"),
        description="60-80 chars. 'Manufacturer Brand, Item Type, Series, MPN'. "
                    "Null if not derivable.")
    short_desc: Sourced = Field(
        default_factory=lambda: not_generated("short_desc"),
        description="Product title: Brand + Series + MPN + Item Type + key attributes. "
                    "Null if not derivable.")
    retail_desc: Sourced = Field(
        default_factory=lambda: not_generated("retail_desc"),
        description="Shopper-facing title, no brand/MPN. e.g. "
                    "'Professional Series Dishwasher, Leg Mounting, 5-Wash Cycle'. Null if not derivable.")

    # --- derived, never requested from the model ---------------------------
    # These are @property rather than pydantic fields on purpose: properties are
    # absent from the generated JSON schema, so Claude is never asked to grade
    # its own output. Scoring stays deterministic and auditable.

    @property
    def core_fields(self) -> list[Sourced]:
        # Deliberately still the original four. The delivery-format description
        # variants are additional output, not additional *core* content, and
        # folding them in here would silently redefine `completeness` — making
        # the 32-record golden baseline incomparable with everything after it.
        return [self.name, self.brand, self.category, self.description]

    @property
    def delivery_descriptions(self) -> dict[str, Sourced]:
        """The five description variants, keyed by their delivery column."""
        return {
            "INVOICE_DESC": self.invoice_desc,
            "MOBILE_DESC": self.mobile_desc,
            "SHORT_DESC": self.short_desc,
            "RETAIL_DESC": self.retail_desc,
            "LONG_DESC1": self.description,
        }

    @property
    def completeness(self) -> float:
        """Fraction of publishable slots we filled. Specs count as ONE slot no
        matter how many there are — otherwise a record with 40 junk specs
        outscores one with 4 good ones, and the metric starts rewarding padding."""
        filled = sum(1 for f in self.core_fields if f.is_grounded)
        total = len(self.core_fields) + 1  # +1 for the specs slot
        if any(s.is_grounded for s in self.specs):
            filled += 1
        return round(filled / total, 3)

    @property
    def mean_confidence(self) -> float:
        scored = [f.confidence for f in self.core_fields + self.specs if f.value is not None]
        return round(sum(scored) / len(scored), 3) if scored else 0.0

    @property
    def gaps(self) -> list[str]:
        """Field names we could not ground — the honest-answer surface. This is
        what the UI shows instead of a guess, and what a judge should poke at."""
        named = {"name": self.name, "brand": self.brand,
                 "category": self.category, "description": self.description}
        out = [k for k, v in named.items() if not v.is_grounded]
        out += [f"spec:{s.name}" for s in self.specs if not s.is_grounded]
        return out


class Issue(BaseModel):
    """One problem found by the validation pass."""

    # See the note on Sourced: closed-object markers are added per provider.

    field: str = Field(description="Field name, or 'spec:<name>' for a spec.")
    severity: Literal["contradiction", "implausible", "unsupported", "unit"] = Field(
        description="contradiction=conflicts with input; implausible=out of physical range; "
        "unsupported=evidence does not back the value; unit=wrong or missing unit."
    )
    detail: str = Field(description="One sentence: what is wrong.")
    suggested_confidence: float = Field(
        description="What the confidence should be given this issue. 0.0 to retract entirely."
    )


class ValidationReport(BaseModel):
    """Output of the validation pass — a distinct step from generation.

    Kept separate from Product because asking one call to both write and grade
    its own work produces a rubber stamp. A fresh call, shown the claim and its
    evidence, and asked only 'does this hold up?', actually finds things.
    """

    # See the note on Sourced: closed-object markers are added per provider.

    issues: list[Issue] = Field(description="Empty list if the record holds up.")
    verdict: Literal["pass", "revise", "reject"] = Field(
        description="pass=publishable; revise=fixable problems; reject=fundamentally ungrounded."
    )
