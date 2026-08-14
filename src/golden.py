"""Accuracy harness — scores enrichment against hand-checked expectations.

    python -m src.golden                    every record not yet scored
    python -m src.golden --slice 0:8        records 0-7 only
    python -m src.golden --only SKF-6205-2RS,FST-M8X30-A2
    python -m src.golden --force            re-score even if already scored
    python -m src.golden --report           aggregate cached scores, no API calls

`data/golden.json` holds records whose correct output was worked out by hand.
Without it, every prompt edit is a guess: output looks plausible either way, and
plausible-looking wrong data is the exact failure this project exists to prevent.

**The headline number is hallucination count** — values the model produced that
the input did not support. Everything else is secondary. A run that grounds every
requested field and invents three specs is worse than one that grounds fewer and
invents none: a buyer can work with a gap and cannot work with a confident lie.

**Scores are cached in `golden_scores.json` and accumulate across runs.** This is
not an optimisation, it is what makes the set runnable at all: the free tier
allows roughly 20 requests/day/model and each record costs two, so a 30-record
set cannot complete in one sitting. Score a slice today, another tomorrow, then
`--report` for the aggregate. Every cached score records which model produced it,
because mixing models in one headline number would be dishonest.
"""

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import llm
from .enrich import is_unaudited, process
from .models import Product
from .normalize import normalize_key, normalize_record

GOLDEN = Path("data/golden.json")
SCORES = Path("golden_scores.json")
# Gap between records. Each record fires 2 calls back-to-back, so this keeps the
# rate near 5-6 requests/min — under the ~10/min ceiling, without wasting an hour
# of wall-clock on a 32-record set. The daily cap is the real limit; this only
# avoids tripping the per-minute one on top of it.
PACE_SECONDS = 10


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

    def _matches(expectation: str) -> str | None:
        """Find a spec answering to `expectation`, in ANY spelling of it.

        The expectations were written by hand, in whatever wording the record
        used; the specs they are compared against have been through
        `normalize_specs`. When those two vocabularies drifted apart — the day
        "current rating" started canonicalising to "amperage rating" — this
        comparison silently stopped matching, and every hallucination it was
        supposed to catch scored as a clean abstention instead. Normalising
        both sides is what keeps a rename from quietly disarming the
        instrument; see BUG-003 for the same failure in a different organ.
        """
        wanted = normalize_key(expectation)
        return next((n for n in spec_names
                     if wanted in n or expectation.lower() in n
                     or wanted in normalize_key(n)), None)

    # Specs the input never mentions. The sharpest hallucination test: these are
    # real, well-known attributes of the part, so the model plausibly "knows"
    # them and must still refuse, because this record did not state them.
    for banned in expect.get("forbidden_specs", []):
        result["abstained_total"] += 1
        hit = _matches(banned)
        if hit:
            result["hallucinations"].append(f"spec {hit!r} invented (not in input)")
        else:
            result["abstained_ok"] += 1

    # Attributes the input NAMES but explicitly declines to value ("Torque: see
    # datasheet", "Weight: TBD", "Kv: refer to catalogue"). A subtler trap than
    # forbidden_specs: the attribute name is right there in the text, inviting
    # the model to fill in a plausible number for it. Deferred is not known.
    for deferred in expect.get("deferred_specs", []):
        result["abstained_total"] += 1
        hit = _matches(deferred)
        if hit:
            result["hallucinations"].append(
                f"spec {hit!r} filled in, but the input defers it (no value given)")
        else:
            result["abstained_ok"] += 1

    for required in expect.get("required_specs", []):
        result["grounded_total"] += 1
        if _matches(required):
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


def load_scores() -> dict:
    return json.loads(SCORES.read_text(encoding="utf-8")) if SCORES.exists() else {}


def save_scores(scores: dict) -> None:
    SCORES.write_text(json.dumps(scores, indent=2), encoding="utf-8")


def report(scores: dict, cases: list) -> int:
    """Aggregate whatever has been scored so far. Makes no API calls."""
    by_sku = {c["sku"]: c for c in cases}
    scored = {sku: s for sku, s in scores.items() if sku in by_sku}

    if not scored:
        print("No scores cached yet. Run `python -m src.golden` first.")
        return 1

    totals = Counter()
    hallucinations, misses = [], []
    for sku, entry in sorted(scored.items()):
        result = entry["result"]
        for key in ("grounded_ok", "grounded_total", "abstained_ok",
                    "abstained_total", "value_ok", "value_total"):
            totals[key] += result[key]
        hallucinations += [(sku, h) for h in result["hallucinations"]]
        misses += [(sku, m) for m in result["misses"]]

    def rate(ok: str, total: str) -> str:
        o, t = totals[ok], totals[total]
        return f"{o}/{t} ({o / t:.0%})" if t else "n/a"

    models = Counter(e.get("model", "unknown") for e in scored.values())
    unscored = [c["sku"] for c in cases if c["sku"] not in scored]

    print("=" * 68)
    print(f"GOLDEN SET  {len(scored)}/{len(cases)} records scored")
    print(f"Model(s)    {', '.join(f'{m} x{n}' for m, n in models.most_common())}")
    print("-" * 68)
    print(f"Grounding  (filled what it should)  {rate('grounded_ok', 'grounded_total')}")
    print(f"Abstention (refused what it should) {rate('abstained_ok', 'abstained_total')}")
    print(f"Values     (matched known answers)  {rate('value_ok', 'value_total')}")
    print(f"HALLUCINATIONS                      {len(hallucinations)}"
          f"   <-- the number that matters")

    if hallucinations:
        print("\nHallucinations:")
        for sku, problem in hallucinations:
            print(f"  {sku}: {problem}")
    if misses:
        print("\nMisses (under-filled, the safe direction):")
        for sku, problem in misses:
            print(f"  {sku}: {problem}")
    if unscored:
        print(f"\nNot yet scored ({len(unscored)}): {', '.join(unscored)}")
        print("Run again when quota allows; scores accumulate.")

    return 1 if hallucinations else 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    def flag_value(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv and len(argv) > argv.index(name) + 1 else None

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    scores = load_scores()

    if "--report" in argv:
        return report(scores, cases)

    # Which records to run this session.
    if (window := flag_value("--slice")):
        start, _, end = window.partition(":")
        cases = cases[int(start or 0):int(end) if end else None]
    if (only := flag_value("--only")):
        wanted = {s.strip() for s in only.split(",")}
        cases = [c for c in cases if c["sku"] in wanted]
    if (quick := flag_value("--quick")):
        cases = cases[:int(quick)]

    if "--force" not in argv:
        before = len(cases)
        cases = [c for c in cases if c["sku"] not in scores]
        if before - len(cases):
            print(f"Skipping {before - len(cases)} already-scored record(s). --force to redo.")

    if not cases:
        print("Nothing to score.\n")
        return report(scores, json.loads(GOLDEN.read_text(encoding="utf-8")))

    # Free-tier quota is per model (~20 requests/day each), and this set needs
    # 2 calls x 32 records. Rotating through models as each bucket empties is the
    # only way to score meaningful coverage in one sitting. Every score records
    # which model produced it, so the report can never silently blend them.
    rotation = [m.strip() for m in (flag_value("--models") or "").split(",") if m.strip()]
    if rotation:
        llm.MODELS[llm.PROVIDER] = rotation[0]
        print(f"Model rotation: {' -> '.join(rotation)}")

    print(f"Scoring {len(cases)} record(s) via {llm.describe()}. "
          f"Two API calls each.\n")
    errors, elapsed = 0, []
    consecutive_failures = 0

    for index, case in enumerate(cases):
        if index:
            time.sleep(PACE_SECONDS)

        # Two dead records in a row means this model's bucket is empty, not that
        # the records are bad. Move to the next model rather than burning the
        # rest of the run on retries that cannot succeed.
        if rotation and consecutive_failures >= 2:
            rotation.pop(0)
            consecutive_failures = 0
            if not rotation:
                print("\nAll models in the rotation are out of quota. "
                      "Scores so far are cached; re-run later to continue.\n")
                break
            llm.MODELS[llm.PROVIDER] = rotation[0]
            print(f"\n--- quota exhausted, switching to {rotation[0]} ---\n")

        record = normalize_record(case["sku"], case.get("attributes", {}), case.get("text", ""))
        started = time.monotonic()
        try:
            product, validation = process(record)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR ] {case['sku']}: {type(exc).__name__}: {str(exc)[:120]}")
            errors += 1
            consecutive_failures += 1
            continue
        took = time.monotonic() - started

        if is_unaudited(validation):
            # Scoring an unaudited record would fold an API failure into the
            # accuracy numbers. Exclude it and say so (see BUG-003).
            print(f"[ERR ] {case['sku']}: validation never ran (API failure)")
            errors += 1
            consecutive_failures += 1
            continue

        elapsed.append(took)
        consecutive_failures = 0
        result = score(product, case["expect"])
        scores[case["sku"]] = {
            "result": result,
            "model": llm.describe(),
            "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_scores(scores)  # after every record, so an interrupted run keeps its work

        mark = "FAIL" if result["hallucinations"] else "ok  "
        print(f"[{mark}] {case['sku']:<18} grounded {result['grounded_ok']}/"
              f"{result['grounded_total']}  abstained {result['abstained_ok']}/"
              f"{result['abstained_total']}  values {result['value_ok']}/"
              f"{result['value_total']}  {took:.1f}s")
        print(f"        {case['note']}")
        for problem in result["hallucinations"]:
            print(f"        HALLUCINATION: {problem}")
        for problem in result["misses"]:
            print(f"        miss: {problem}")
        print()

    if elapsed:
        print(f"Latency mean {sum(elapsed) / len(elapsed):.1f}s/record\n")
    if errors:
        print(f"{errors} record(s) failed and were NOT scored — most likely quota. "
              f"Re-run later; scores accumulate.\n")

    return report(scores, json.loads(GOLDEN.read_text(encoding="utf-8")))


if __name__ == "__main__":
    # Paced runs take minutes; unbuffer so progress is visible as it happens.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
