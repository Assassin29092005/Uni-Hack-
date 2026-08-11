"""Accuracy harness — scores enrichment against hand-checked expectations.

    python -m src.golden              full set
    python -m src.golden --quick 3    first 3 records only

`data/golden.json` holds records whose correct output we worked out by hand.
Without it, every prompt edit is a guess: the output looks plausible either way,
and plausible-looking wrong data is the exact failure this project exists to
prevent.

The headline number is **hallucination count** — values the model produced that
the input did not support. Everything else here is secondary. A run that grounds
every requested field and invents three specs is a worse result than one that
grounds fewer and invents none, because a buyer can work with a gap and cannot
work with a confident lie.

Costs real API calls (two per record).
"""

import json
import sys
import time
from pathlib import Path

from .enrich import is_unaudited, process
from .models import Product
from .normalize import normalize_record

GOLDEN = Path("data/golden.json")
PACE_SECONDS = 20  # free tiers throttle per minute


def _core(product: Product) -> dict:
    return {"name": product.name, "brand": product.brand,
            "category": product.category, "description": product.description}


def score(product: Product, expect: dict) -> dict:
    """Compare one enriched record against its hand-checked expectations."""
    core = _core(product)
    result = {"grounded_ok": 0, "grounded_total": 0,
              "abstained_ok": 0, "abstained_total": 0,
              "value_ok": 0, "value_total": 0,
              "hallucinations": [], "misses": []}

    # Fields that should carry a grounded value.
    for field in expect.get("grounded", []):
        result["grounded_total"] += 1
        if core[field].is_grounded:
            result["grounded_ok"] += 1
        else:
            result["misses"].append(f"{field} not grounded (expected a value)")

    # Fields the input cannot support — inventing one is a hallucination.
    for field in expect.get("absent_core", []):
        result["abstained_total"] += 1
        if core[field].is_grounded:
            result["hallucinations"].append(
                f"{field} = {core[field].value!r} (input supports no value)")
        else:
            result["abstained_ok"] += 1

    # Exact values, checked loosely: the model may add words around a correct one.
    for field, expected in expect.get("values", {}).items():
        result["value_total"] += 1
        actual = core[field].value or ""
        if expected.lower() in actual.lower():
            result["value_ok"] += 1
        else:
            result["misses"].append(f"{field} = {actual!r}, expected to contain {expected!r}")

    spec_names = [s.name.lower() for s in product.specs if s.is_grounded]

    # Specs the input never mentions. This is the sharpest hallucination test:
    # these are all real, well-known attributes of the part — the model plausibly
    # "knows" them and must still refuse, because this record didn't state them.
    for banned in expect.get("forbidden_specs", []):
        result["abstained_total"] += 1
        hit = next((n for n in spec_names if banned.lower() in n), None)
        if hit:
            result["hallucinations"].append(f"spec {hit!r} invented (not in input)")
        else:
            result["abstained_ok"] += 1

    for required in expect.get("required_specs", []):
        result["grounded_total"] += 1
        if any(required.lower() in n for n in spec_names):
            result["grounded_ok"] += 1
        else:
            result["misses"].append(f"no spec matching {required!r}")

    if "max_specs" in expect:
        result["abstained_total"] += 1
        if len(spec_names) > expect["max_specs"]:
            result["hallucinations"].append(
                f"{len(spec_names)} grounded spec(s); input supports at most "
                f"{expect['max_specs']}")
        else:
            result["abstained_ok"] += 1

    return result


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    limit = None
    if "--quick" in argv:
        index = argv.index("--quick")
        limit = int(argv[index + 1]) if len(argv) > index + 1 else 3

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))[:limit]
    print(f"Scoring {len(cases)} golden record(s). Two API calls each.\n")

    totals = {"grounded_ok": 0, "grounded_total": 0, "abstained_ok": 0,
              "abstained_total": 0, "value_ok": 0, "value_total": 0}
    all_hallucinations, errors, elapsed = [], 0, []

    for index, case in enumerate(cases):
        if index:
            time.sleep(PACE_SECONDS)

        record = normalize_record(case["sku"], case.get("attributes", {}), case.get("text", ""))
        started = time.monotonic()
        try:
            product, report = process(record)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR ] {case['sku']}: {type(exc).__name__}: {exc}")
            errors += 1
            continue
        took = time.monotonic() - started
        elapsed.append(took)

        if is_unaudited(report):
            # Scoring an unaudited record would mix an API failure into the
            # accuracy numbers. Exclude it and say so.
            print(f"[ERR ] {case['sku']}: validation never ran (API failure)")
            errors += 1
            continue

        result = score(product, case["expect"])
        for key in totals:
            totals[key] += result[key]
        all_hallucinations += [(case["sku"], h) for h in result["hallucinations"]]

        mark = "FAIL" if result["hallucinations"] else "ok  "
        print(f"[{mark}] {case['sku']:<14} grounded {result['grounded_ok']}/"
              f"{result['grounded_total']}  abstained {result['abstained_ok']}/"
              f"{result['abstained_total']}  values {result['value_ok']}/"
              f"{result['value_total']}  {took:.1f}s")
        print(f"        {case['note']}")
        for problem in result["hallucinations"]:
            print(f"        HALLUCINATION: {problem}")
        for problem in result["misses"]:
            print(f"        miss: {problem}")
        print()

    def rate(ok: int, total: int) -> str:
        return f"{ok}/{total} ({ok / total:.0%})" if total else "n/a"

    print("=" * 66)
    print(f"Grounding  (filled what it should)  {rate(totals['grounded_ok'], totals['grounded_total'])}")
    print(f"Abstention (refused what it should) {rate(totals['abstained_ok'], totals['abstained_total'])}")
    print(f"Values     (matched known answers)  {rate(totals['value_ok'], totals['value_total'])}")
    print(f"HALLUCINATIONS                      {len(all_hallucinations)}   <-- the number that matters")
    if elapsed:
        print(f"Latency    mean {sum(elapsed) / len(elapsed):.1f}s/record "
              f"(2 calls each, incl. free-tier throttling)")
    if errors:
        print(f"Excluded from scoring (API failures) {errors}")

    for sku, problem in all_hallucinations:
        print(f"  {sku}: {problem}")

    return 1 if (all_hallucinations or errors) else 0


if __name__ == "__main__":
    # Paced runs take minutes; unbuffer so progress is visible as it happens.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
