"""Field-level accuracy against Unilog's own labelled rows.

    python -m src.truth                 score the catalog against their file
    python -m src.truth --control       score their file against itself
    python -m src.truth --detail        also print every disagreement in full

The brief asks submissions to show "field-level accuracy against the 200
known-good rows". This is that instrument. `golden.py` answers a different and
weaker question — *we* wrote the expectations there, so it measures whether the
model invents things, not whether our output matches what the client actually
wants. Only their delivery sheet can answer the second one.

**What it scores.** Only the columns we generate. Scoring all 252 would drown
the signal in ~170 columns nothing in a 6-column input could ground, and would
let a submission look 70% accurate for emitting blanks. Passthrough columns
(Part_Desc, Dept/Class/Fine, PART_NUMBER) are excluded for the mirror-image
reason: they are copied, so scoring them measures the CSV reader, not the
pipeline.

**The four verdicts, and why `missing` is not `differs`.** A blank where the
truth has a value is a *gap* — recoverable, honest, and exactly what this
project ships when it cannot ground a field. A different value is an *error*:
it reaches a buyer looking indistinguishable from a right answer. Averaging
them into one "accuracy" number hides the only distinction that matters, so
`differs` is the headline and the others are reported beside it.

**The control.** `--control` scores their file against itself and must come
back perfect. A comparator that cannot score a known-good row as correct tells
you nothing about a row it grades badly — the same reason `probe.py` carries a
clean control record. It is wired into the test suite, so it runs for free
rather than when someone remembers to pass the flag.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from . import delivery, store
from .models import Product

GROUND_TRUTH = Path("data/ground_truth_delivery.csv")

# Their sheet keys rows by the manufacturer part number, and so do we.
KEY_COLUMN = "Mfg_Part_Num"

# Attribute triplets are compared as a label-keyed set, never slot by slot:
# their slot 3 and our slot 3 holding different attributes is an ordering
# difference, not a wrong value, and a positional diff would report it as two
# errors instead of one ordering note.
_ATTRIBUTE_SLOT = re.compile(r"^ATTRIBUTE_(LABEL|VALUE|UOM) (\d+)$")


def _norm(value: str) -> str:
    """Casefold and collapse whitespace. Deliberately nothing else.

    In particular ® and ™ survive: the guide requires brand names to match the
    approved list "symbols and all", so folding them away would convert a real
    conformance failure into a silent pass. `_is_near` exists to annotate that
    case without forgiving it.
    """
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _is_near(ours: str, theirs: str) -> bool:
    """True when two values differ only in punctuation or symbols.

    'FRIGIDAIRE' vs 'FRIGIDAIRE®' is a different failure from 'Whirlpool' vs
    'Rheem Manufacturing', and a human triaging the report needs to tell them
    apart at a glance. Both still count as `differs` — this only labels them.
    """
    strip = lambda text: re.sub(r"[^\w]+", "", text, flags=re.UNICODE).casefold()
    return bool(strip(ours)) and strip(ours) == strip(theirs)


def _attributes(row: dict[str, str]) -> dict[str, tuple[str, str]]:
    """The 50 triplets as {label: (value, uom)}, skipping empty slots.

    Their sheet keeps a label with an empty value when a category defines an
    attribute nobody filled in ("Model", "Plug Type", "Color" on the dishwasher
    row). Those are dropped here: a label with no value asserts nothing, and
    counting them as expectations would score us for failing to reproduce their
    blanks.
    """
    out: dict[str, tuple[str, str]] = {}
    slots: dict[str, dict[str, str]] = {}
    for column, value in row.items():
        match = _ATTRIBUTE_SLOT.match(column)
        if match:
            part, index = match.groups()
            slots.setdefault(index, {})[part] = value or ""
    for slot in slots.values():
        label, value, uom = slot.get("LABEL", ""), slot.get("VALUE", ""), slot.get("UOM", "")
        if label.strip() and value.strip():
            out[_norm(label)] = (value.strip(), uom.strip())
    return out


def compare_row(ours: dict[str, str], theirs: dict[str, str],
                columns: list[str]) -> list[dict]:
    """One row pair -> one verdict per scored column, plus the attribute set."""
    verdicts = []
    for column in columns:
        mine, yours = (ours.get(column) or "").strip(), (theirs.get(column) or "").strip()
        if not mine and not yours:
            continue  # neither side claims anything; nothing to be right about
        if _norm(mine) == _norm(yours):
            verdict = "match"
        elif mine and yours:
            verdict = "differs"
        elif yours:
            verdict = "missing"
        else:
            verdict = "extra"
        verdicts.append({
            "column": column, "verdict": verdict, "ours": mine, "theirs": yours,
            "near": verdict == "differs" and _is_near(mine, yours),
        })

    mine_attributes, their_attributes = _attributes(ours), _attributes(theirs)
    for label, (value, uom) in their_attributes.items():
        expected = f"{value} {uom}".strip()
        if label not in mine_attributes:
            verdict, actual = "missing", ""
        else:
            got_value, got_uom = mine_attributes[label]
            actual = f"{got_value} {got_uom}".strip()
            verdict = "match" if _norm(actual) == _norm(expected) else "differs"
        verdicts.append({
            "column": f"ATTRIBUTE[{label}]", "verdict": verdict,
            "ours": actual, "theirs": expected,
            "near": verdict == "differs" and _is_near(actual, expected),
        })
    for label in mine_attributes.keys() - their_attributes.keys():
        value, uom = mine_attributes[label]
        verdicts.append({
            "column": f"ATTRIBUTE[{label}]", "verdict": "extra",
            "ours": f"{value} {uom}".strip(), "theirs": "", "near": False,
        })
    return verdicts


def scored_columns() -> list[str]:
    """The columns this scorer holds us to: the ones we assert.

    Taken from `delivery.GENERATED_COLUMNS` rather than listed again here, so a
    new generated column is scored the day it starts shipping instead of the
    day someone remembers to add it to a second list.
    """
    return list(delivery.GENERATED_COLUMNS)


def load_truth(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. It is Unilog's labelled delivery sheet — the only\n"
            f"source of field-level accuracy. Copy their CSV there and re-run."
        )
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {(row.get(KEY_COLUMN) or "").strip(): row
            for row in rows if (row.get(KEY_COLUMN) or "").strip()}


def score(db_path: Path | None, truth_path: Path, control: bool = False) -> dict:
    """Compare every ground-truth row we hold an enriched record for.

    Returns the tallies. Raises SystemExit when nothing overlaps — a scorer
    that prints "100% accurate (0 records)" is worse than one that refuses.
    """
    truth = load_truth(truth_path)

    if control:
        # Their file graded against itself. Any verdict other than `match`
        # means the comparator is broken and every other number it has ever
        # printed is meaningless.
        pairs = [(sku, dict(row), row) for sku, row in truth.items()]
    else:
        conn = store.connect(db_path)
        pairs = []
        for sku, row in truth.items():
            product = store.load(conn, sku)
            if product is None:
                continue
            stored = conn.execute(
                "SELECT raw FROM products WHERE sku = ?", (sku,)).fetchone()
            raw = json.loads(stored["raw"]) if stored and stored["raw"] else {}
            pairs.append((sku, delivery.to_row(product, raw), row))
        conn.close()

    if not pairs:
        raise SystemExit(
            f"None of the {len(truth)} ground-truth SKUs are in the catalog, so there is\n"
            f"nothing to score. Enrich them first:\n\n"
            f"    python -m src.pipeline data/ground_truth_input.csv\n\n"
            f"({len(truth)} record(s) x 2 model calls.) No accuracy figure is printed\n"
            f"for an empty comparison."
        )

    columns = scored_columns()
    tally = {"match": 0, "differs": 0, "missing": 0, "extra": 0}
    per_column: dict[str, dict[str, int]] = {}
    findings: list[dict] = []
    for sku, ours, theirs in pairs:
        for verdict in compare_row(ours, theirs, columns):
            tally[verdict["verdict"]] += 1
            bucket = per_column.setdefault(verdict["column"], dict.fromkeys(tally, 0))
            bucket[verdict["verdict"]] += 1
            if verdict["verdict"] != "match":
                findings.append({"sku": sku, **verdict})

    return {"records": len(pairs), "tally": tally,
            "per_column": per_column, "findings": findings,
            "ground_truth_records": len(truth), "control": control}


def report(result: dict, detail: bool = False) -> None:
    """Print the scorecard. `differs` leads because it is the only tally that
    represents a value a buyer could act on and be wrong."""
    tally = result["tally"]
    scored = sum(tally.values())
    print(f"\nScored {result['records']} of {result['ground_truth_records']} "
          f"ground-truth record(s), {scored} field comparison(s).\n")

    print(f"  differs   {tally['differs']:>4}   wrong value — the number that matters")
    print(f"  missing   {tally['missing']:>4}   we left it blank; they have a value (a gap, not an error)")
    print(f"  extra     {tally['extra']:>4}   we filled it; their sheet is blank")
    print(f"  match     {tally['match']:>4}")
    if scored:
        agreed = tally["match"] / scored
        print(f"\n  exact agreement: {agreed:.0%} of compared fields")
        if tally["differs"] == 0:
            print("  no field contradicts their sheet")

    print("\nPer column (match/differs/missing/extra):")
    for column, bucket in sorted(result["per_column"].items()):
        flag = "  <-- wrong" if bucket["differs"] else ""
        print(f"  {column:<34} {bucket['match']:>3}/{bucket['differs']:>3}/"
              f"{bucket['missing']:>3}/{bucket['extra']:>3}{flag}")

    disagreements = [f for f in result["findings"] if f["verdict"] == "differs"]
    if disagreements:
        print(f"\nDisagreements ({len(disagreements)}):")
        for finding in disagreements if detail else disagreements[:12]:
            tag = " [punctuation/symbol only]" if finding["near"] else ""
            print(f"  {finding['sku']} · {finding['column']}{tag}")
            print(f"      ours:   {finding['ours'][:110]!r}")
            print(f"      theirs: {finding['theirs'][:110]!r}")
        if not detail and len(disagreements) > 12:
            print(f"  ... and {len(disagreements) - 12} more (--detail for all)")

    gaps = [f for f in result["findings"] if f["verdict"] == "missing"]
    if gaps and not detail:
        print(f"\nBlank where they have a value ({len(gaps)}): "
              + ", ".join(sorted({f['column'] for f in gaps}))[:200])


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="field-level accuracy against Unilog's labelled rows")
    parser.add_argument("--db", type=Path, default=None, help="catalog to score")
    parser.add_argument("--file", type=Path, default=GROUND_TRUTH,
                        help="their delivery-format CSV")
    parser.add_argument("--control", action="store_true",
                        help="score their file against itself; must be perfect")
    parser.add_argument("--detail", action="store_true", help="print every finding")
    args = parser.parse_args(argv)

    result = score(args.db, args.file, control=args.control)

    if args.control:
        # The instrument checking itself. Anything but a clean sweep means the
        # comparator is broken, and every accuracy figure it has printed is
        # unsafe to quote.
        wrong = {k: v for k, v in result["tally"].items() if k != "match" and v}
        report(result)
        if wrong:
            print(f"\nCONTROL FAILED: {wrong} on a row scored against itself.")
            print("The comparator is broken; ignore any accuracy it has reported.")
            return 1
        print("\nCONTROL PASSED: their own rows score as a clean match.")
        return 0

    report(result, detail=args.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ponytail: two labelled rows, because that is what the pack contained. The
# scorer is row-count agnostic — point `--file` at the full 200-row Delivery
# Format sheet and it scores all of it. Attribute comparison is by label, so it
# measures whether we produced the right attribute, not whether we produced it
# in their slot order; slot-order conformance needs the LOV's fixed sequence
# per category and is unmeasurable until that file exists.

