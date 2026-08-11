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

    model_config = {"extra": "forbid"}  # structured outputs require closed objects

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

    def as_prompt_block(self) -> str:
        """Flatten to the text we actually show the model. Kept here so the
        prompt and the data model can't disagree about what the model saw."""
        lines = [f"SKU: {self.sku}"]
        lines += [f"{k}: {v}" for k, v in self.attributes.items() if v]
        if self.text:
            lines.append(f"Free text: {self.text}")
        return "\n".join(lines)


class Product(BaseModel):
    """The enriched, commerce-ready record. This is what Claude fills in."""

    model_config = {"extra": "forbid"}

    sku: str = Field(description="Copy the SKU through unchanged.")
    name: Sourced = Field(description="Commercial product name.")
    brand: Sourced = Field(description="Manufacturer or brand.")
    category: Sourced = Field(description="Industrial product category.")
    description: Sourced = Field(description="2-3 sentence commerce-ready description.")
    specs: list[Spec] = Field(description="Technical attributes. Only what you can ground.")

    # --- derived, never requested from the model ---------------------------
    # These are @property rather than pydantic fields on purpose: properties are
    # absent from the generated JSON schema, so Claude is never asked to grade
    # its own output. Scoring stays deterministic and auditable.

    @property
    def core_fields(self) -> list[Sourced]:
        return [self.name, self.brand, self.category, self.description]

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

    model_config = {"extra": "forbid"}

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

    model_config = {"extra": "forbid"}

    issues: list[Issue] = Field(description="Empty list if the record holds up.")
    verdict: Literal["pass", "revise", "reject"] = Field(
        description="pass=publishable; revise=fixable problems; reject=fundamentally ungrounded."
    )
