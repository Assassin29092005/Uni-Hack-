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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Product, ValidationReport

DB_PATH = Path("catalog.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    data            TEXT NOT NULL,   -- full Product JSON, provenance included
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
    stage      TEXT NOT NULL,       -- 'enrich' | 'validate' | 'error'
    detail     TEXT NOT NULL,       -- JSON payload for that stage
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_sku ON audit(sku);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) and ensure the schema exists."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def is_done(conn: sqlite3.Connection, sku: str) -> bool:
    """Has this SKU already been enriched?

    This is what makes a catalog run resumable: `pipeline` calls it before
    spending a model call, so killing and restarting a 10k-row job re-pays for
    nothing. Interruptions during a demo are a certainty, not a risk.
    """
    row = conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone()
    return row is not None


def save(conn: sqlite3.Connection, product: Product, report: ValidationReport) -> None:
    """Upsert the record and append its audit rows. One transaction, so a record
    can never end up persisted without the trail that explains it."""
    with conn:  # commits on success, rolls back on exception
        conn.execute(
            """INSERT INTO products
                   (sku, data, completeness, mean_confidence, verdict, issue_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET
                   data=excluded.data,
                   completeness=excluded.completeness,
                   mean_confidence=excluded.mean_confidence,
                   verdict=excluded.verdict,
                   issue_count=excluded.issue_count,
                   updated_at=excluded.updated_at""",
            (product.sku, product.model_dump_json(), product.completeness,
             product.mean_confidence, report.verdict, len(report.issues), _now()),
        )
        conn.executemany(
            "INSERT INTO audit (sku, stage, detail, created_at) VALUES (?, ?, ?, ?)",
            [
                (product.sku, "enrich",
                 json.dumps({"completeness": product.completeness, "gaps": product.gaps}), _now()),
                (product.sku, "validate", report.model_dump_json(), _now()),
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
