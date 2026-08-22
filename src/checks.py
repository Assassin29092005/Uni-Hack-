"""Deterministic validation rules — the half of the audit that costs nothing.

Runs *alongside* `enrich.validate_one`, never instead of it. The model catches
what no table can (a 30 kg handheld tool, evidence that restates its own claim);
these rules catch what a model should never have been asked to catch, and catch
it identically every time, for free, and — the part that matters on free-tier
quota — **even when the API call failed entirely**. A record whose validation
pass died still gets audited by everything in this file.

Six rules, each aimed at a failure this project has actually observed rather
than one we imagined:

1. `unsupported_input_claims` — a field whose `source` is `input` but whose value
   is not in the record. Claiming provenance you do not have is mechanically
   checkable and always wrong, whatever the value's merits.
2. `identifier_decoded_specs` — BUG-004. A spec inferred from the part number
   ("`6205-2RS-C3` means 25 mm bore, C3 clearance") states something the record
   never did. Deliberately limited to specs: naming the likely manufacturer or
   family from a series prefix is legitimate inference, inventing its dimensions
   is not.
3. `brand_not_in_record` — an inferred brand the record never names. Its own
   rule because a wrong manufacturer is the most expensive error in this job.
4. `undocumented_provenance` — a field citing a `document` or the `web` when no
   document was retrieved. Found in the stored catalog: 12 fields claimed
   `document` while quoting the description column at themselves.
5. `unit_problems` — a dimensioned quantity carrying a unit from the wrong family
   (current rated in mm), or carrying no unit at all.
6. `contradictory_specs` — two specs that normalize to the same attribute and
   disagree about the value.

Everything here returns `Issue` objects, the same type the LLM validator
produces, so `apply_report` applies both through one code path and the audit
trail cannot tell you a finding was "only" a rule. The severity vocabulary is
shared for the same reason.
"""

import re

from .models import Issue, Product, RawRecord, Sourced

# Attribute-name keyword -> units that quantity may legitimately carry, in the
# canonical spellings `normalize.normalize_unit` produces. Matched as a substring
# of the canonical spec name and resolved LONGEST-FIRST, so "power supply" reads
# as a voltage rather than being dragged into "power" and flagged for not being
# in watts.
QUANTITY_UNITS: dict[str, set[str]] = {
    "power supply": {"V", "V DC", "V AC", "kV", "mV"},
    "voltage": {"V", "V DC", "V AC", "kV", "mV"},
    # "current" and "amperage" both listed: `normalize` canonicalises current
    # attributes to Unilog's label "amperage rating", which contains neither the
    # word "current" nor any other key here. Without this entry the unit check
    # silently stopped firing on every current spec the moment the canonical
    # name changed — a rule that quietly matches nothing looks identical to a
    # rule that finds no problems.
    "current": {"A", "mA", "kA", "µA"},
    "amperage": {"A", "mA", "kA", "µA"},
    "power": {"W", "kW", "mW", "hp", "VA"},
    "frequency": {"Hz", "kHz", "MHz"},
    "weight": {"kg", "g", "lb", "oz", "t"},
    "mass": {"kg", "g", "lb", "oz"},
    "pressure": {"bar", "psi", "Pa", "kPa", "MPa"},
    "torque": {"N·m", "Nm", "kgf·m", "lb-ft", "lbf-ft", "in-lb"},
    "temperature": {"°C", "°F", "K"},
    "speed": {"rpm", "m/s", "mm/s", "km/h"},
    "diameter": {"mm", "cm", "m", "in", "ft"},
    "bore": {"mm", "cm", "m", "in", "ft"},
    "stroke": {"mm", "cm", "m", "in", "ft"},
    "length": {"mm", "cm", "m", "in", "ft"},
    "width": {"mm", "cm", "m", "in", "ft"},
    "height": {"mm", "cm", "m", "in", "ft"},
    "thickness": {"mm", "cm", "m", "in", "ft"},
    "pitch": {"mm", "cm", "in"},
}

# Language that says "I read this off the part number". BUG-004's signature: the
# model recognises a numbering scheme and reports what that scheme conventionally
# means as though the record had stated it.
_IDENTIFIER_LANGUAGE = re.compile(
    r"part number|model number|part no|designation|nomenclature|suffix|prefix|"
    r"series|scheme|denotes|indicates that|standard designation|typical for|"
    r"conventionally|by convention|commonly|usually|known to be|catalog(?:ue)? number",
    re.IGNORECASE,
)

# A bare number or numeric range: "5", "3.4", "10-30", "1,200". Only these need a
# unit — "DC", "IP67", and "316L" are values that are complete without one, and
# demanding a unit for them is how a unit check turns into noise.
_NUMERIC = re.compile(r"^[\d.,]+(?:\s*-\s*[\d.,]+)?$")

# More than half a value's words missing from the record is the line between
# "composed from what we were given" and "arrived from somewhere else". Tuned
# to let the flagship case through: the name "Bosch Rexroth HDX-4025-200
# Hydraulic Cylinder" is assembled entirely from tokens the record contains, so
# it scores 0.0 and stays silent, while a fabricated "Allen-Bradley" scores 1.0.
UNSUPPORTED_SHARE = 0.5


def _tokens(text: str) -> list[str]:
    """Word runs, unicode-aware. Splits on punctuation so "Allen-Bradley" and
    "Allen Bradley" compare equal, and keeps CJK runs whole."""
    return [t for t in re.findall(r"[^\W_]+", text.lower()) if len(t) > 1]


def _unsupported_share(value: str, haystack: str) -> float:
    """Fraction of a value's words that do not appear in the record.

    Token overlap rather than exact substring on purpose. An enriched value is
    usually *composed* from the input ("Bosch Rexroth" + the SKU + the category
    noun), so requiring the whole string to appear verbatim would flag every
    well-built name. Requiring the words to have come from somewhere is the
    weaker claim that is actually true.
    """
    tokens = _tokens(value)
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t not in haystack) / len(tokens)


def _haystack(record: RawRecord) -> str:
    """The distributor's record, lowercased — the only legitimate source for a
    value claiming `source: input`.

    `as_input_block`, NOT `as_prompt_block`: once manufacturer documentation is
    attached, the prompt contains text the record never said. Building the
    haystack from the whole prompt would let a spec lifted off a datasheet claim
    `source: input` and pass, which inverts the one thing this rule checks.
    Document-sourced values are legitimate — they just have to say `document`.
    """
    return record.as_input_block().lower()


def _quantity_for(name: str) -> tuple[str, set[str]] | None:
    """Longest matching quantity keyword, or None if the name isn't dimensioned."""
    matches = [(k, u) for k, u in QUANTITY_UNITS.items() if k in name.lower()]
    return max(matches, key=lambda kv: len(kv[0])) if matches else None


def unsupported_input_claims(record: RawRecord, product: Product) -> list[Issue]:
    """Fields sourced to `input` whose value is not in the input."""
    haystack = _haystack(record)
    issues = []
    named: list[tuple[str, Sourced]] = [
        ("name", product.name), ("brand", product.brand),
        ("category", product.category), ("description", product.description),
    ]
    named += [(f"spec:{s.name}", s) for s in product.specs]

    for field, sourced in named:
        if sourced.source != "input" or not sourced.value:
            continue
        share = _unsupported_share(sourced.value, haystack)
        if share > UNSUPPORTED_SHARE:
            issues.append(Issue(
                field=field, severity="unsupported",
                detail=f"Claims source 'input', but {share:.0%} of "
                       f"{sourced.value!r} does not appear in the record.",
                suggested_confidence=0.3,
            ))
    return issues


def identifier_decoded_specs(record: RawRecord, product: Product) -> list[Issue]:
    """BUG-004: specs read off the part number rather than out of the record.

    Three conditions must hold together, because any one alone has honest
    explanations: the spec is an inference, its value is absent from the record,
    AND its own evidence says it came from the identifier. A spec that satisfies
    all three is the model reporting what a designation conventionally means —
    true of the part, unstated by this record, and exactly what a buyer must not
    be allowed to order against.

    Confidence drops to just under `CONFIDENCE_FLOOR` rather than to zero: the
    value stops being published but stays visible with its reason, which is the
    difference between the catalog admitting a gap and the catalog forgetting a
    claim was ever made.
    """
    haystack = _haystack(record)
    issues = []
    for spec in product.specs:
        if spec.source != "inference" or not spec.value:
            continue
        if _unsupported_share(spec.value, haystack) <= UNSUPPORTED_SHARE:
            continue  # the record does contain it; inference merely joined it up
        if not _IDENTIFIER_LANGUAGE.search(spec.evidence):
            continue
        issues.append(Issue(
            field=f"spec:{spec.name}", severity="unsupported",
            detail=f"{spec.name!r} = {spec.value!r} is decoded from the identifier, "
                   f"not stated by the record. A part number resembling a known "
                   f"series is not a statement of that series' properties.",
            suggested_confidence=0.4,
        ))
    return issues


def brand_not_in_record(record: RawRecord, product: Product) -> list[Issue]:
    """A brand the record never names is a decoded part number, not a finding.

    The companion to `identifier_decoded_specs`, which is deliberately limited to
    specs. Brand needed its own rule because it is the most expensive field to
    get wrong — a wrong manufacturer on a live catalog page misroutes warranty
    claims and returns — and because house brands and third-party parts copy
    famous numbering schemes constantly, so "the SKU looks like Allen-Bradley's"
    is evidence of nothing.

    Only `inference` brands are checked. A brand copied from a column or quoted
    from the text is grounded by definition, and `unsupported_input_claims`
    already polices whether that claim is true.
    """
    brand = product.brand
    if not brand.is_grounded or not brand.value or brand.source != "inference":
        return []

    haystack = _haystack(record).casefold()
    # Match on the longest word so "3M" inside "3M 775L Stikit" counts, while a
    # multi-word invention like "Allen-Bradley" has to appear as written.
    needle = max(brand.value.split(), key=len, default="").casefold().strip(".,()")
    if len(needle) >= 2 and needle in haystack:
        return []

    return [Issue(
        field="brand", severity="unsupported",
        detail=f"Brand {brand.value!r} appears nowhere in the record — inferred "
               f"from the part number, which names no manufacturer.",
        suggested_confidence=0.0)]


def undocumented_provenance(record: RawRecord, product: Product) -> list[Issue]:
    """A field citing a document when no document was attached.

    Found by auditing the stored catalog: 12 fields claimed `source: document`
    with evidence like "Free text: 'PDSH4816AF Dishwasher SS'". No document was
    ever retrieved for that record — the model was using `document` to mean
    "the description column", which is `input`.

    Harmless-looking until `src/sources.py` made real documents possible. Now
    the label is the only thing distinguishing a genuine datasheet citation from
    the model renaming the distributor's own blurb, and a provenance system
    whose strongest label can be self-awarded is not a provenance system.

    Two outcomes, because these are different failures:

    - the value IS in the record — a mislabel of real data. Flagged, not
      retracted: the value is grounded and only its paperwork is wrong, and
      retracting a correct value teaches nobody anything.
    - the value is NOT in the record — an invented citation, the worst case in
      the whole taxonomy. A fabricated spec attributed to a datasheet that does
      not exist is precisely the confident hallucination this project refuses,
      dressed in the most authoritative label available. Retracted to 0.0.
    """
    if record.document:
        return []  # a document is attached; citing it is legitimate

    haystack = _haystack(record)
    issues = []
    named: list[tuple[str, Sourced]] = [
        ("name", product.name), ("brand", product.brand),
        ("category", product.category), ("description", product.description),
    ]
    named += [(f"spec:{s.name}", s) for s in product.specs]

    for field, sourced in named:
        if sourced.source not in ("document", "web") or not sourced.value:
            continue
        invented = _unsupported_share(sourced.value, haystack) > UNSUPPORTED_SHARE
        issues.append(Issue(
            field=field, severity="unsupported",
            detail=(
                f"claims source {sourced.source!r}, but no document was retrieved "
                f"for this record and the value does not appear in it either — "
                f"the citation is invented."
                if invented else
                f"claims source {sourced.source!r}, but no document was retrieved "
                f"for this record. The value is in the input, so this is a "
                f"mislabel: the correct source is 'input'."
            ),
            suggested_confidence=0.0 if invented else sourced.confidence,
        ))
    return issues


def unit_problems(product: Product) -> list[Issue]:
    """Dimensioned quantities with the wrong unit family, or none at all.

    Only fires on numeric values. "IP67", "316L", and "DC" are complete without
    a unit, and a rule that demands one for them produces noise that trains
    everybody to ignore the unit severity.
    """
    issues = []
    for spec in product.specs:
        if not spec.value or not _NUMERIC.match(spec.value.strip()):
            continue
        quantity = _quantity_for(spec.name)
        if quantity is None:
            continue  # not a dimensioned attribute — "number of poles" needs no unit
        keyword, allowed = quantity

        if not spec.unit:
            issues.append(Issue(
                field=f"spec:{spec.name}", severity="unit",
                detail=f"{spec.name!r} is a numeric {keyword} with no unit; "
                       f"expected one of {', '.join(sorted(allowed))}.",
                suggested_confidence=0.6,
            ))
        elif spec.unit not in allowed:
            issues.append(Issue(
                field=f"spec:{spec.name}", severity="unit",
                detail=f"{spec.name!r} is measured in {spec.unit!r}, which is not a "
                       f"unit of {keyword} (expected {', '.join(sorted(allowed))}).",
                suggested_confidence=0.6,
            ))
    return issues


def contradictory_specs(product: Product) -> list[Issue]:
    """Two specs that canonicalise to one attribute and disagree.

    `normalize_specs` has already merged the exact duplicates, so anything still
    sharing a name here genuinely conflicts. Both copies are flagged, not one:
    the rule knows they disagree and has no grounds to pick a winner.
    """
    by_name: dict[str, list] = {}
    for spec in product.specs:
        by_name.setdefault(spec.name, []).append(spec)

    issues = []
    for name, group in by_name.items():
        values = {f"{s.value} {s.unit or ''}".strip() for s in group if s.value}
        if len(values) > 1:
            issues.append(Issue(
                field=f"spec:{name}", severity="contradiction",
                detail=f"{name!r} is claimed as {' and '.join(sorted(repr(v) for v in values))} "
                       f"in the same record.",
                suggested_confidence=0.3,
            ))
    return issues


# Unilog's hard limits for the delivery-format description variants:
# (column, min chars or None, max chars or None, must be ALL CAPS).
# A field that breaks these is rejected on format regardless of how good its
# content is, so it is worth catching here — for free, every time — rather than
# discovering it in a delivery review.
DESCRIPTION_RULES = {
    "INVOICE_DESC": (None, 40, True),
    "MOBILE_DESC": (60, 80, False),
}


def description_format(product: Product) -> list[Issue]:
    """Character limits and casing for the five description variants.

    Purely mechanical, so the model is never asked to count its own characters —
    something LLMs are famously unreliable at. It writes the text; arithmetic
    stays here.
    """
    issues: list[Issue] = []
    for column, field in product.delivery_descriptions.items():
        if not field.value or column not in DESCRIPTION_RULES:
            continue
        low, high, caps = DESCRIPTION_RULES[column]
        length = len(field.value)

        if high is not None and length > high:
            issues.append(Issue(
                field=column, severity="unsupported",
                detail=f"{column} is {length} characters; the limit is {high}.",
                suggested_confidence=0.0))
        elif low is not None and length < low:
            # Under-length is a review flag, not a defect. A product whose
            # manufacturer, brand, type and part number are all short words
            # cannot reach 60 characters without padding, and padding is
            # inventing — the exact thing this project refuses to do. So the
            # value is still emitted, flagged for a human to extend from the
            # manufacturer's own page. Confidence is left alone: the text is
            # true, it is merely short.
            issues.append(Issue(
                field=column, severity="unsupported",
                detail=f"{column} is {length} characters, under the {low} minimum — "
                       f"needs a human to extend it from manufacturer copy "
                       f"(padding it here would be invention).",
                suggested_confidence=field.confidence))

        if caps and field.value != field.value.upper():
            issues.append(Issue(
                field=column, severity="unsupported",
                detail=f"{column} must be ALL CAPS.",
                suggested_confidence=0.0))
    return issues


def classpath_depth(product: Product) -> list[Issue]:
    """Classpath must be three levels: Dept > Class > Fine.

    The exporter splits it into those three columns, so a two-level path leaves
    `Fine` silently empty — a formatting failure that looks like missing data.
    """
    value = product.category.value
    if not value or not product.category.is_grounded:
        return []
    levels = [part.strip() for part in value.split(">") if part.strip()]
    if len(levels) == 3:
        return []
    return [Issue(
        field="category", severity="unsupported",
        detail=f"Classpath has {len(levels)} level(s), needs 3 "
               f"(Dept > Class > Fine): {value!r}",
        suggested_confidence=min(product.category.confidence, 0.4))]


def run_checks(record: RawRecord, product: Product) -> list[Issue]:
    """Every deterministic rule, in one list. No model calls, no network."""
    return [
        *unsupported_input_claims(record, product),
        *identifier_decoded_specs(record, product),
        *brand_not_in_record(record, product),
        *undocumented_provenance(record, product),
        *unit_problems(product),
        *contradictory_specs(product),
    ]


def delivery_checks(product: Product) -> list[Issue]:
    """Delivery-format compliance — deliberately NOT part of `run_checks`.

    These ask "is this written the way Unilog requires?", which is a different
    question from "is this value true?". Folding them into `run_checks` would
    push format findings through `apply_report`, downgrading the confidence of a
    perfectly well-grounded category because its Classpath has two levels — and
    that would silently move the grounding and abstention numbers the golden
    baseline is measured on.

    So format compliance is reported on its own axis, which is also the
    "character-limit compliance" metric the brief asks submissions to show.
    """
    return [*description_format(product), *classpath_depth(product)]


def merge(record: RawRecord, product: Product, report):
    """Fold deterministic findings into the model's report, returning a new one.

    The input report is never mutated — `probe.py` measures the LLM validator
    alone, and an instrument that silently receives rule output would report our
    lookup tables as the model's judgement.

    A `pass` verdict is downgraded to `revise` when a rule fires. The model said
    the record was publishable; a rule disagrees on grounds that do not depend on
    the model's opinion, and the stricter of the two is the one to keep.
    """
    from .models import ValidationReport

    found = run_checks(record, product)
    if not found:
        return report
    verdict = "revise" if report.verdict == "pass" else report.verdict
    return ValidationReport(issues=[*report.issues, *found], verdict=verdict)

# ponytail: rule 2 requires the evidence to admit where the value came from, so a
# spec inferred silently is not caught. The broader rule — flag ANY inferred spec
# whose value is absent from the record — is one condition away, but it would
# also suppress legitimate inference (a unit conversion, a value assembled from
# two fields), and the golden set is the only instrument that can tell those
# apart. Widen it once grounding can be re-measured, not before.
