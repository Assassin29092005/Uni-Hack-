"""Orchestration and CLI.

    ingest -> normalize -> enrich -> validate -> score -> persist

Every stage takes records and returns records, so any stage can be skipped,
re-run, or tested alone. That uniformity is what makes the run resumable.

    python -m src.pipeline data/sample_products.csv
    python -m src.pipeline data/sample_products.csv --force   # re-enrich everything
    python -m src.pipeline --show HDX-4025                    # inspect one record
    python -m src.pipeline --export data/demo_catalog.json    # dump the catalog
    python -m src.pipeline --seed data/demo_catalog.json      # load it, no API calls
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from . import llm, store
from .enrich import process_batch
from .models import Product, RawRecord
from .normalize import normalize_key, normalize_record

# Columns whose content is prose rather than an attribute — these become the
# free-text blob the model reads for spec extraction instead of a key/value pair.
TEXT_COLUMNS = {"description", "notes", "details", "datasheet", "text"}


def ingest_csv(path: Path) -> list[RawRecord]:
    """CSV -> normalized records. Header names are canonicalised through the
    same alias table the rest of the pipeline uses, so 'MPN', 'Part No', and
    'sku' all resolve to the SKU column without per-file configuration."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path} has no data rows.")

    sku_column = next((c for c in rows[0] if normalize_key(c) == "sku"), None)
    if sku_column is None:
        raise SystemExit(
            f"No SKU column in {path}. Expected one of: sku, mpn, part no, item code."
        )

    records = []
    for row in rows:
        sku = (row.get(sku_column) or "").strip()
        if not sku:
            continue  # a blank SKU is a trailing empty line, not a product
        text = " ".join(
            (row.get(c) or "").strip()
            for c in row
            if normalize_key(c) in TEXT_COLUMNS
        )
        attributes = {c: v for c, v in row.items()
                      if c != sku_column and normalize_key(c) not in TEXT_COLUMNS}
        records.append(normalize_record(sku, attributes, text))
    return records


def run(csv_path: Path, db_path: Path, force: bool = False, workers: int | None = None) -> dict:
    """Enrich a CSV end to end. Skips SKUs already in the database unless
    --force, which is the whole of the resumability story: interrupt it, run it
    again, and it picks up where it stopped without re-paying for finished work."""
    conn = store.connect(db_path)
    records = ingest_csv(csv_path)

    if force:
        todo = records
    else:
        todo = [r for r in records if not store.is_done(conn, r.sku)]
        skipped = len(records) - len(todo)
        if skipped:
            print(f"Skipping {skipped} already-enriched record(s). --force to redo.")

    if not todo:
        print("Nothing to do.")
        stats = store.summary(conn)
        conn.close()
        return stats

    workers = workers or llm.workers_default()
    print(f"Enriching {len(todo)} record(s) via {llm.describe()}, {workers} worker(s)...")
    for record, product, report, error in process_batch(todo, workers=workers):
        if error or product is None or report is None:
            store.save_error(conn, record.sku, error or "unknown failure")
            print(f"  {record.sku}: FAILED — {error}")
            continue
        store.save(conn, product, report)
        gaps = f", gaps: {', '.join(product.gaps)}" if product.gaps else ""
        print(
            f"  {record.sku}: {report.verdict} "
            f"(complete {product.completeness:.0%}, confidence {product.mean_confidence:.2f}, "
            f"{len(report.issues)} issue(s){gaps})"
        )

    stats = store.summary(conn)
    conn.close()  # Windows keeps the file locked while any handle is open
    print("\nCatalog:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    return stats


def show(db_path: Path, sku: str) -> None:
    """Print one record with its provenance — the explainability view, and the
    thing to put on screen when a judge asks 'why does it say that?'."""
    conn = store.connect(db_path)
    product = store.load(conn, sku)
    conn.close()
    if product is None:
        raise SystemExit(f"{sku} not found in {db_path}.")

    print(f"{product.sku}  —  complete {product.completeness:.0%}, "
          f"confidence {product.mean_confidence:.2f}\n")
    fields = [("name", product.name), ("brand", product.brand),
              ("category", product.category), ("description", product.description)]
    fields += [(f"spec:{s.name}", s) for s in product.specs]

    for label, field in fields:
        value = f"{field.value} {field.unit or ''}".strip() if field.value else "— (no grounded value)"
        print(f"{label:>28}: {value}")
        print(f"{'':>28}  [{field.source}, confidence {field.confidence:.2f}] {field.evidence}")


def export_catalog(db_path: Path, out_path: Path) -> int:
    """Write the enriched catalog to portable JSON.

    The pair to `--seed`, and the reason the demo doesn't depend on quota: run
    the pipeline once while the API is up, export, commit the file. See
    `store.export_all`.
    """
    conn = store.connect(db_path)
    entries = store.export_all(conn)
    conn.close()
    if not entries:
        raise SystemExit(f"{db_path} has no enriched records to export.")
    out_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {len(entries)} record(s) to {out_path}.")
    return len(entries)


def seed_catalog(db_path: Path, in_path: Path) -> int:
    """Load an exported catalog into the database. Zero model calls.

    Safe to run on a machine with no API key at all — which is the point, since
    that is what a demo laptop usually is.
    """
    if not in_path.exists():
        raise SystemExit(f"{in_path} not found. Create one with --export first.")
    entries = json.loads(in_path.read_text(encoding="utf-8"))
    conn = store.connect(db_path)
    count = store.seed(conn, entries, source=str(in_path))
    stats = store.summary(conn)
    conn.close()
    print(f"Seeded {count} record(s) from {in_path} into {db_path}. No API calls made.")
    print("Catalog:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    return count


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 and mangle the °, ·, and — characters
    # that industrial units and this CLI's output are full of.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="AI product intelligence pipeline")
    parser.add_argument("csv", nargs="?", type=Path, help="input CSV of sparse product records")
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--force", action="store_true", help="re-enrich records already stored")
    parser.add_argument("--workers", type=int, default=None,
                        help="default depends on the provider's rate limit")
    parser.add_argument("--show", metavar="SKU", help="print one stored record with provenance")
    parser.add_argument("--export", metavar="PATH", type=Path,
                        help="dump the enriched catalog to JSON")
    parser.add_argument("--seed", metavar="PATH", type=Path,
                        help="load an exported catalog; makes no API calls")
    args = parser.parse_args(argv)

    if args.show:
        show(args.db, args.show)
        return 0
    if args.export:
        export_catalog(args.db, args.export)
        return 0
    if args.seed:
        seed_catalog(args.db, args.seed)
        return 0
    if not args.csv:
        parser.error("give a CSV to enrich, or use --show / --export / --seed")
    try:
        run(args.csv, args.db, force=args.force, workers=args.workers)
    except llm.ProviderError as exc:
        # Setup problems (missing key, Ollama not running) are the user's to
        # fix — a traceback buries the one line that tells them how.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
