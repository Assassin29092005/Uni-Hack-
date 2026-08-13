"""SQLite persistence plus an audit trail.

Two tables on purpose. `products` is the current state of the catalog; `audit`
is append-only history of what changed and why. Explainability is a judging
criterion, and "why does this record say 24V?" has to be answerable after the
fact, not just while the response is still in memory.

SQLite because it needs zero setup and zero running service — the demo may well
happen on a stranger's laptop, and a connection-refused error on stage costs
more than any query-planner advantage Postgres would have bought.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Product, ValidationReport

# The UI takes no arguments (it is `python -m src.app`), so an env var is the
# only way to point it at a catalog other than the one in the working directory.
DB_PATH = Path(os.getenv("CATALOG_DB", "catalog.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    data            TEXT NOT NULL,   -- full Product JSON, provenance included
    raw             TEXT,            -- the input row verbatim, as JSON
    completeness    REAL NOT NULL,
    mean_confidence REAL NOT NULL,
    verdict         TEXT NOT NULL,
    issue_count     INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Append-only. Never UPDATE or DELETE here; this is the record of what we did.
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sku        TEXT NOT NULL,
    stage      TEXT NOT NULL,       -- 'enrich' | 'validate' | 'error' | 'seed'
    detail     TEXT NOT NULL,       -- JSON payload for that stage
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_sku ON audit(sku);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and ensure the schema exists.

    `None` rather than `DB_PATH` as the default on purpose: a default argument
    binds once at import, which froze the UI to whatever `DB_PATH` was at module
    load and made it impossible to point anywhere else — including at a
    temporary database in a test.
    """
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a database
    # created before `raw` existed keeps the old shape and the exporter breaks.
    # Add the column in place rather than making the user delete their catalog.
    if "raw" not in {row["name"] for row in conn.execute("PRAGMA table_info(products)")}:
        conn.execute("ALTER TABLE products ADD COLUMN raw TEXT")
        conn.commit()
    return conn


def is_done(conn: sqlite3.Connection, sku: str) -> bool:
    """Has this SKU already been enriched?

    This is what makes a catalog run resumable: `pipeline` calls it before
    spending a model call, so killing and restarting a 10k-row job re-pays for
    nothing. Interruptions during a demo are a certainty, not a risk.
    """
    row = conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone()
    return row is not None


def save(conn: sqlite3.Connection, product: Product, report: ValidationReport,
         at: str | None = None, raw: dict | None = None) -> None:
    """Upsert the record and append its audit rows. One transaction, so a record
    can never end up persisted without the trail that explains it.

    `at` overrides the timestamp, and exists only for `seed`: importing a catalog
    that was enriched last Tuesday must not stamp every record as enriched now.
    An audit trail that lies about when is barely better than no audit trail.

    `raw` is the input row verbatim. The delivery format passes several of the
    distributor's own columns straight through, and re-deriving them from the
    enriched record would risk "improving" a customer's part number.
    """
    at = at or _now()
    with conn:  # commits on success, rolls back on exception
        conn.execute(
            """INSERT INTO products
                   (sku, data, raw, completeness, mean_confidence, verdict, issue_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET
                   data=excluded.data,
                   raw=COALESCE(excluded.raw, products.raw),
                   completeness=excluded.completeness,
                   mean_confidence=excluded.mean_confidence,
                   verdict=excluded.verdict,
                   issue_count=excluded.issue_count,
                   updated_at=excluded.updated_at""",
            (product.sku, product.model_dump_json(),
             json.dumps(raw) if raw else None, product.completeness,
             product.mean_confidence, report.verdict, len(report.issues), at),
        )
        conn.executemany(
            "INSERT INTO audit (sku, stage, detail, created_at) VALUES (?, ?, ?, ?)",
            [
                (product.sku, "enrich",
                 json.dumps({"completeness": product.completeness, "gaps": product.gaps}), at),
                (product.sku, "validate", report.model_dump_json(), at),
            ],
        )


def save_error(conn: sqlite3.Connection, sku: str, error: str) -> None:
    """Record a failure in the audit trail without writing a products row.

    A failed record must stay absent from the catalog — a half-enriched product
    is worse than a missing one — but the attempt still has to be traceable, or
    a rerun looks identical to a run that never saw the record.
    """
    with conn:
        conn.execute(
            "INSERT INTO audit (sku, stage, detail, created_at) VALUES (?, ?, ?, ?)",
            (sku, "error", json.dumps({"error": error}), _now()),
        )


def load(conn: sqlite3.Connection, sku: str) -> Product | None:
    row = conn.execute("SELECT data FROM products WHERE sku = ?", (sku,)).fetchone()
    return Product.model_validate_json(row["data"]) if row else None


def load_report(conn: sqlite3.Connection, sku: str) -> ValidationReport | None:
    """The most recent validation report for a SKU.

    The report was always persisted; nothing read it back, so what the validator
    actually found lived in the database and appeared nowhere a human could see.
    The UI uses this to show the findings rather than just the fact that a
    validation stage ran.
    """
    row = conn.execute(
        "SELECT detail FROM audit WHERE sku = ? AND stage = 'validate' "
        "ORDER BY id DESC LIMIT 1",
        (sku,),
    ).fetchone()
    return ValidationReport.model_validate_json(row["detail"]) if row else None


def export_all(conn: sqlite3.Connection) -> list[dict]:
    """The whole catalog as portable JSON: record, report, and when it was made.

    Exists so a demo never depends on live quota. Enrich once when the API is
    available, export, commit the file, and `seed` it back on any machine — the
    UI then has real, model-produced content with its real timestamps, and makes
    zero API calls to show it.
    """
    rows = conn.execute("SELECT sku, data, updated_at FROM products ORDER BY sku").fetchall()
    out = []
    for row in rows:
        report = load_report(conn, row["sku"])
        out.append({
            "sku": row["sku"],
            "product": json.loads(row["data"]),
            # A record with no stored report predates the audit trail; an empty
            # pass is the honest reading, not an invented set of findings.
            "report": json.loads(report.model_dump_json()) if report else
                      {"issues": [], "verdict": "pass"},
            "enriched_at": row["updated_at"],
        })
    return out


def seed(conn: sqlite3.Connection, entries: list[dict], source: str) -> int:
    """Load an `export_all` dump into the catalog. Makes no model calls.

    Every record keeps its original enrichment timestamp, and gains one extra
    audit row recording that it arrived by import — so the trail says both when
    the record was enriched and when this database learned about it, rather than
    quietly presenting imported data as freshly generated.
    """
    for entry in entries:
        product = Product.model_validate(entry["product"])
        report = ValidationReport.model_validate(entry["report"])
        save(conn, product, report, at=entry.get("enriched_at"))
        with conn:
            conn.execute(
                "INSERT INTO audit (sku, stage, detail, created_at) VALUES (?, ?, ?, ?)",
                (product.sku, "seed", json.dumps({"source": source}), _now()),
            )
    return len(entries)


def summary(conn: sqlite3.Connection) -> dict:
    """Catalog-level numbers — the scale story in one query."""
    row = conn.execute(
        """SELECT COUNT(*) n,
                  COALESCE(AVG(completeness), 0) completeness,
                  COALESCE(AVG(mean_confidence), 0) confidence,
                  COALESCE(SUM(issue_count), 0) issues,
                  COALESCE(SUM(verdict = 'pass'), 0) passing
           FROM products"""
    ).fetchone()
    errors = conn.execute("SELECT COUNT(*) n FROM audit WHERE stage = 'error'").fetchone()["n"]
    return {
        "products": row["n"],
        "avg_completeness": round(row["completeness"], 3),
        "avg_confidence": round(row["confidence"], 3),
        "issues_found": row["issues"],
        "passing": row["passing"],
        "failed_records": errors,
    }

# ponytail: single-file SQLite with default locking, so concurrent writers will
# contend. Fine at demo scale (writes are serialised through one process).
# Upgrade path if parallel workers become real: WAL mode, then Postgres — the
# persistence layer is deliberately thin so the swap stays small.
