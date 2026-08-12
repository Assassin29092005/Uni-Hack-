"""Deterministic validation rules — the half of the audit that costs nothing.

Runs *alongside* `enrich.validate_one`, never instead of it. The model catches
what no table can (a 30 kg handheld tool, evidence that restates its own claim);
these rules catch what a model should never have been asked to catch, and catch
it identically every time, for free, and — the part that matters on free-tier
quota — **even when the API call failed entirely**. A record whose validation
pass died still gets audited by everything in this file.

Four rules, each aimed at a failure this project has actually observed rather
than one we imagined:

1. `unsupported_input_claims` — a field whose `source` is `input` but whose value
   is not in the record. Claiming provenance you do not have is mechanically
   checkable and always wrong, whatever the value's merits.
2. `identifier_decoded_specs` — BUG-004. A spec inferred from the part number
   ("`6205-2RS-C3` means 25 mm bore, C3 clearance") states something the record
   never did. Deliberately limited to specs: naming the likely manufacturer or
   family from a series prefix is legitimate inference, inventing its dimensions
   is not.
3. `unit_problems` — a dimensioned quantity carrying a unit from the wrong family
   (current rated in mm), or carrying no unit at all.
4. `contradictory_specs` — two specs that normalize to the same attribute and
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
    "current": {"A", "mA", "kA", "µA"},
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
    """Everything the model was shown, lowercased — the only legitimate source
    for a value claiming `source: input`."""
    return record.as_prompt_block().lower()


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


def run_checks(record: RawRecord, product: Product) -> list[Issue]:
    """Every deterministic rule, in one list. No model calls, no network."""
    return [
        *unsupported_input_claims(record, product),
        *identifier_decoded_specs(record, product),
        *unit_problems(product),
        *contradictory_specs(product),
    ]


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
