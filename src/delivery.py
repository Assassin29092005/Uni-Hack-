"""Export enriched records in Unilog's 252-column delivery format.

    python -m src.delivery out.csv              every stored record
    python -m src.delivery out.csv --limit 50

The header row is a **fixed contract**: the brief says "Contains the static
headers your solution must populate. Please do not change or modify the
headers." So the column list is loaded from `data/delivery_headers.json` —
captured verbatim from the sheet they supplied — and written in that exact
order, every time, including the ~170 columns we cannot ground and leave blank.

Two files come out of a run, and the split is deliberate:

  out.csv           the graded artifact. Their 252 columns, nothing added.
  out.provenance.csv  one row per emitted value: where it came from, the
                      evidence, the confidence, and whether a human should look.

Their schema has nowhere to record *why* a value is what it is, and we were told
not to add columns. Dropping provenance to fit would throw away the thing the
brief calls "a genuinely valuable feature" ("a confidence score or a 'needs
human review' flag"). So it ships beside the deliverable rather than inside it.

**Blank is a real answer here.** A column we cannot ground stays empty rather
than being filled with something plausible — the guide is explicit that "a
fluent description made of invented values scores zero".
"""

import csv
import json
import sys
from pathlib import Path

from . import checks, store
from .models import CONFIDENCE_FLOOR, Product, Sourced

HEADERS_FILE = Path("data/delivery_headers.json")

# How many ATTRIBUTE_LABEL/VALUE/UOM triplets the sheet provides.
ATTRIBUTE_SLOTS = 50


def headers() -> list[str]:
    """The 252 column names, in their order. Never rewritten by us."""
    return json.loads(HEADERS_FILE.read_text(encoding="utf-8"))


# Words that stay lowercase inside an attribute label, and initialisms that stay
# upper. "Number Of Wash Cycles" and "Ip Rating" both read as mistakes.
_MINOR_WORDS = {"of", "per", "and", "or", "to", "in", "at", "by", "for", "with"}
_INITIALISMS = {"ip", "uom", "id", "od", "npt", "awg", "rpm", "dc", "ac", "led", "usb", "pdf"}


def _title_case(label: str) -> str:
    """Unilog house style for attribute labels: Title Case, minor words lower."""
    words = label.split()
    out = []
    for index, word in enumerate(words):
        lower = word.lower()
        if lower in _INITIALISMS:
            out.append(lower.upper())
        elif index and lower in _MINOR_WORDS:
            out.append(lower)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def _emit(field: Sourced) -> str:
    """A value only reaches the deliverable if we would stand behind it.

    Below the confidence floor we emit nothing at all rather than a hedged
    guess: an empty cell is a known gap a human can fill, while a wrong cell is
    indistinguishable from a right one and propagates into a live catalog.
    """
    return field.value if field.is_grounded and field.value else ""


def to_row(product: Product, raw: dict[str, str] | None = None) -> dict[str, str]:
    """One enriched product -> one 252-column row.

    `raw` is the original input row; its columns pass through untouched because
    they are the distributor's own identifiers, not something for us to improve.
    """
    raw = raw or {}
    row = {column: "" for column in headers()}

    # --- passthrough: the distributor's own fields ------------------------
    # Deliberately copied, not regenerated. Rewriting a customer's part number
    # would be a data-integrity bug dressed up as enrichment.
    part_number = raw.get("Mfg_Part_Num", "") or product.sku
    row["Mfg_Part_Num"] = part_number
    row["MANUFACTURER_PART_NUMBER"] = part_number
    row["PART_NUMBER"] = raw.get("PART_NUMBER", "")
    row["SKU - MY_PART_NUMBER"] = raw.get("SKU - MY_PART_NUMBER", "")
    row["Part_Desc"] = raw.get("Part_Desc", "")
    row["Part_Manuf"] = raw.get("Part_Manuf", "")
    # Placeholder brands ("-- Unbranded --") are filtered during normalization,
    # so anything surviving here is a real value.
    for column in ("E1_Brand", "Unilog_Brand", "DIB_Brand"):
        row[column] = raw.get(column, "")

    # --- enriched identity ------------------------------------------------
    brand = _emit(product.brand)
    row["BRAND_NAME"] = brand
    row["MANUFACTURER_NAME"] = brand  # no approved manufacturer list shipped; see README
    row["Product Name"] = _emit(product.name)

    # --- taxonomy ---------------------------------------------------------
    # Classpath is "A > B > C"; Dept/Class/Fine are its three levels. Splitting
    # rather than asking the model for four separate fields keeps them
    # consistent by construction — they cannot disagree with each other.
    classpath = _emit(product.category)
    row["Classpath"] = classpath
    levels = [part.strip() for part in classpath.split(">")] if classpath else []
    for column, level in zip(("Dept", "Class", "Fine"), levels):
        row[column] = level

    # --- the five descriptions --------------------------------------------
    # Each is generated to its own rule (length, casing, word order) rather than
    # derived from one another, so an ungrounded variant is blank instead of a
    # truncation of a different sentence.
    for column, field in product.delivery_descriptions.items():
        row[column] = _emit(field)

    # --- attributes -> the 50 triplets ------------------------------------
    # Spec(name, value, unit) lines up 1:1 with LABEL/VALUE/UOM. Keeping units
    # in their own field from the start is what makes this a copy rather than a
    # parse. Ungrounded specs are skipped, not blanked-in-place, so the slots
    # stay dense and a reader can trust that slot N being empty means "no more
    # attributes", not "one was dropped here".
    grounded = [spec for spec in product.specs if spec.is_grounded]
    for index, spec in enumerate(grounded[:ATTRIBUTE_SLOTS], start=1):
        # Labels are stored lowercase so dedup and contradiction-matching are
        # case-insensitive; Unilog's house style is Title Case. Converting here
        # keeps the canonical internal form and the required external form
        # without either one having to compromise.
        row[f"ATTRIBUTE_LABEL {index}"] = _title_case(spec.name)
        row[f"ATTRIBUTE_VALUE {index}"] = spec.value or ""
        row[f"ATTRIBUTE_UOM {index}"] = spec.unit or ""

    return row


def provenance_rows(product: Product, raw: dict[str, str] | None = None) -> list[dict]:
    """The audit sidecar: one row per value we emitted, plus every refusal.

    Refusals are included on purpose. A reviewer needs to see that `brand` is
    empty *because the record named no manufacturer*, not because the pipeline
    skipped it — that distinction is the whole argument for trusting the cells
    that are filled.
    """
    part_number = (raw or {}).get("Mfg_Part_Num", "") or product.sku
    fields: list[tuple[str, Sourced]] = [
        ("Product Name", product.name),
        ("BRAND_NAME", product.brand),
        ("Classpath", product.category),
        ("LONG_DESC1", product.description),
    ]
    fields += [(f"ATTRIBUTE_VALUE ({spec.name})", spec) for spec in product.specs]

    return [
        {
            "Mfg_Part_Num": part_number,
            "column": column,
            "value": field.value or "",
            "emitted": "yes" if field.is_grounded else "no",
            "source": field.source,
            "confidence": f"{field.confidence:.2f}",
            "needs_review": "yes" if (field.value and not field.is_grounded) else "",
            "evidence": field.evidence,
        }
        for column, field in fields
    ]


def export(out_path: Path, limit: int | None = None) -> tuple[int, int]:
    """Write the deliverable and its provenance sidecar. Returns (rows, blanks)."""
    conn = store.connect()
    query = "SELECT sku, data, raw FROM products ORDER BY sku"
    if limit:
        query += f" LIMIT {int(limit)}"
    try:
        records = conn.execute(query).fetchall()
    except Exception:
        # `raw` column predates this exporter; fall back so an older database
        # still exports rather than erroring at submission time.
        records = conn.execute(
            "SELECT sku, data, NULL AS raw FROM products ORDER BY sku"
        ).fetchall()
    conn.close()

    columns = headers()
    provenance: list[dict] = []
    format_issues: list[tuple[str, str]] = []
    filled_cells = 0
    # `--out build/x.csv` should just work; failing on a missing parent dir is a
    # pointless obstacle at submission time.
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for record in records:
            product = Product.model_validate_json(record["data"])
            raw = json.loads(record["raw"]) if record["raw"] else {}
            row = to_row(product, raw)
            filled_cells += sum(1 for value in row.values() if value)
            writer.writerow(row)
            provenance += provenance_rows(product, raw)
            format_issues += [(product.sku, issue.detail)
                              for issue in checks.delivery_checks(product)]

    sidecar = out_path.with_suffix(".provenance.csv")
    if provenance:
        with open(sidecar, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(provenance[0]))
            writer.writeheader()
            writer.writerows(provenance)

    total_cells = len(records) * len(columns)
    print(f"{out_path}  {len(records)} row(s) x {len(columns)} columns")
    print(f"{sidecar}  {len(provenance)} provenance row(s)")
    if total_cells:
        print(f"populated cells: {filled_cells}/{total_cells} "
              f"({filled_cells / total_cells:.0%}) — blanks are ungrounded, not skipped")

    # Format compliance is its own axis, separate from whether the values are
    # true. The brief asks submissions to show character-limit compliance, so
    # it is reported on every export rather than left for a reviewer to find.
    compliant = len(records) - len({sku for sku, _ in format_issues})
    print(f"delivery-format compliant rows: {compliant}/{len(records)}")
    for sku, detail in format_issues[:10]:
        print(f"  {sku}: {detail}")
    if len(format_issues) > 10:
        print(f"  ... and {len(format_issues) - 10} more")
    return len(records), total_cells - filled_cells


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip().split("\n\n")[1])
        return 2
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    export(Path(argv[0]), limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ponytail: MANUFACTURER_NAME currently mirrors BRAND_NAME. The brief's
# UniCat_Manufacturer_and_Brand_List.xlsx (27k approved manufacturer/brand pairs
# with exact legal casing and ®/™) is the correct source and was not in the file
# pack we received — see README → Missing reference data. Upgrade path: fuzzy
# match Part_Manuf against that list, then take the paired canonical brand.
