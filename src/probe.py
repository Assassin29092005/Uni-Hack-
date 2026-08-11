"""Adversarial probe for the validation pass.

    python -m src.probe

Plants known errors in enriched records and checks the validator catches them.
This exists because "the AI validates its output" is a claim, and a validator
that has never objected to anything is indistinguishable from one that always
says `pass`. Our first live run returned 0 issues across 5 records — clean data
and a rubber stamp look identical from the outside, so this tells them apart.

Each case targets one numbered check in `VALIDATE_SYSTEM`, so a miss says which
instruction isn't landing rather than just "validation is bad".

**The control case is the point.** A validator that flags everything catches
every planted error and is still useless. `clean-control` must come back with
zero issues, or the other results mean nothing.

Costs real API calls (one per case). Not part of `test_pipeline.py`, which stays
offline and free.
"""

import sys
import time

from .enrich import is_unaudited, validate_one
from .models import Product, RawRecord, Sourced, Spec

# Seconds between cases. Free tiers throttle per minute and these prompts are
# large; without pacing, later cases burn their retries and return the
# "unaudited" fallback instead of a verdict.
PACE_SECONDS = 20


def _s(value, confidence=0.9, unit=None, evidence="Stated in the input record.",
       source="input") -> Sourced:
    return Sourced(value=value, unit=unit, source=source,
                   evidence=evidence, confidence=confidence)


# Each case: (name, which validator check it targets, raw record, enriched product,
# expect_issues). The planted fault is described so a demo can narrate it.
def _cases():
    relay_raw = RawRecord(
        sku="RLY-24DC-4PDT",
        attributes={"name": "Miniature Power Relay", "brand": "Omron",
                    "operating voltage": "24 V DC"},
        text="4PDT contacts rated 5A at 250VAC. DIN rail mount. Weighs 85 grams.",
    )

    def relay(**over) -> Product:
        # The control has to be genuinely defensible, not merely factually right.
        # First version of this fixture used "Stated in the input record." as
        # evidence for a name that was actually derived and a description that
        # was synthesized — and the validator correctly flagged both as
        # unsupported. Evidence that doesn't establish its value IS a defect,
        # so the control now cites specifically.
        base = dict(
            sku="RLY-24DC-4PDT",
            name=_s("Miniature Power Relay", confidence=1.0,
                    evidence="Verbatim from the input field 'name: Miniature Power Relay'."),
            brand=_s("Omron", confidence=1.0, evidence="Verbatim from 'brand: Omron'."),
            category=_s("Power Relay", confidence=0.9, source="inference",
                        evidence="The input name 'Miniature Power Relay' states the "
                                 "product type; 'Power Relay' is its category."),
            description=_s(
                "A miniature 4PDT power relay with a 24 V DC coil, rated 5 A at "
                "250 V AC, for DIN rail mounting.",
                confidence=0.9, source="inference",
                evidence="Assembled from 'name: Miniature Power Relay', "
                         "'operating voltage: 24 V DC', and the free text "
                         "'4PDT contacts rated 5A at 250VAC. DIN rail mount.'"),
            specs=[Spec(name="operating voltage", value="24", unit="V DC",
                        source="input", evidence="Verbatim from 'operating voltage: 24 V DC'.",
                        confidence=1.0)],
        )
        return Product(**{**base, **over})

    return [
        (
            "contradiction",
            "check 1 — value conflicts with the raw input",
            "Input says the coil is 24 V DC; the record claims 240 V.",
            relay_raw,
            relay(specs=[Spec(name="operating voltage", value="240", unit="V DC",
                              source="input", evidence="operating voltage: 24 V DC",
                              confidence=0.95)]),
            True,
        ),
        (
            "circular-evidence",
            "check 2 — evidence does not support the value",
            "Evidence restates the claim instead of establishing it.",
            relay_raw,
            relay(brand=_s("Siemens", evidence="The brand is Siemens because it is a "
                                               "Siemens relay.", confidence=0.9)),
            True,
        ),
        (
            "implausible",
            "check 3 — physically implausible for the product class",
            "A miniature DIN-rail relay given a 45 kg mass (input says 85 grams).",
            relay_raw,
            relay(specs=[Spec(name="weight", value="45", unit="kg", source="input",
                              evidence="Weighs 85 grams.", confidence=0.9)]),
            True,
        ),
        (
            "wrong-unit",
            "check 4 — unit inconsistent with the quantity",
            "Contact current rating expressed in millimetres.",
            relay_raw,
            relay(specs=[Spec(name="current rating", value="5", unit="mm",
                              source="input", evidence="contacts rated 5A",
                              confidence=0.9)]),
            True,
        ),
        (
            "overconfident-guess",
            "check 5 — confidence unjustified by the evidence",
            "An admitted guess asserted at 0.99 confidence.",
            relay_raw,
            relay(category=_s("Automotive Starter Motor", confidence=0.99,
                              source="inference",
                              evidence="Guessing based on nothing in particular; the "
                                       "record does not indicate the product type.")),
            True,
        ),
        (
            "clean-control",
            "CONTROL — nothing is wrong",
            "A correct record. Any issue raised here is a false positive.",
            relay_raw,
            relay(),
            False,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    # Optional case-name filter, so tuning one prompt instruction doesn't cost a
    # full sweep of API calls: `python -m src.probe clean-control`
    wanted = set(argv if argv is not None else sys.argv[1:])
    cases = [c for c in _cases() if not wanted or c[0] in wanted]
    if not cases:
        print(f"No case matches {sorted(wanted)}. "
              f"Available: {', '.join(c[0] for c in _cases())}")
        return 2

    print("Probing the validator with planted errors "
          "(a real API call per case)...\n")
    rows, failures, errors = [], 0, 0

    for index, (name, target, planted, raw, product, expect_issues) in enumerate(cases):
        if index:
            # Free tiers throttle per minute. Without this the last cases exhaust
            # their retries and return the "unaudited" fallback, which would
            # otherwise be scored as a result.
            time.sleep(PACE_SECONDS)

        report = validate_one(raw, product)

        if is_unaudited(report):
            # The audit never ran. Counting this as "found an issue" would let an
            # API failure masquerade as a successful detection — which is exactly
            # what the first run of this probe did.
            errors += 1
            rows.append((name, target, planted, expect_issues, None,
                         report.verdict, None, []))
            continue

        found = len(report.issues)
        ok = (found > 0) if expect_issues else (found == 0)
        failures += not ok

        rows.append((name, target, planted, expect_issues, found,
                     report.verdict, ok, report.issues))

    width = max(len(r[0]) for r in rows)
    for name, target, planted, expected, found, verdict, ok, issues in rows:
        mark = "ERR " if ok is None else ("PASS" if ok else "FAIL")
        want = "should flag" if expected else "should be silent"
        shown = "n/a" if found is None else found
        print(f"[{mark}] {name:<{width}}  {want:<16} found={shown:<3} verdict={verdict}")
        print(f"         {target}")
        print(f"         planted: {planted}")
        for issue in issues:
            print(f"         -> {issue.severity} on {issue.field}: {issue.detail} "
                  f"(confidence -> {issue.suggested_confidence})")
        print()

    scored = [r for r in rows if r[6] is not None]  # exclude cases that never ran
    caught = sum(1 for r in scored if r[3] and r[6])
    planted_total = sum(1 for r in scored if r[3])
    controls = [r for r in scored if not r[3]]
    control_ok = bool(controls) and all(r[6] for r in controls)

    if not controls:
        control_note = "NOT MEASURED — the control case did not run, so the catch rate is meaningless"
    elif control_ok:
        control_note = "yes"
    else:
        control_note = "NO — the validator flags everything, so the catches above prove nothing"

    print(f"Planted errors caught: {caught}/{planted_total}")
    print(f"Control (clean record stayed silent): {control_note}")
    if errors:
        print(f"Cases that never ran (API failure, excluded from scoring): {errors}")

    if failures:
        print(f"\n{failures} case(s) failed. Tune VALIDATE_SYSTEM in src/enrich.py "
              f"against the specific check named above.")
    if errors:
        print(f"Re-run the errored case(s) alone, e.g. "
              f"`python -m src.probe clean-control`.")
    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    # Paced runs take minutes; unbuffer so progress is visible as it happens.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
