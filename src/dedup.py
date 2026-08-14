"""De-duplication — guide step 2. Deterministic, zero model calls.

Run against their 1000-row sheet, the naive assumption ("lots of duplicates,
collapse them") is wrong on both counts. There are exactly two collisions in
1000 rows, and neither should be merged:

    AVM6EV  "AVM6 EV Mini Snip Red"      <- same part number,
    AVM6EV  "AVM7 EV Mini Snip Green"       two different products

    52C3-5/8-UPC   "4x4 1G Box Cover"    <- three part numbers,
    52C14-5/8-UPC  "4x4 1G Box Cover"       one description
    52C3-UPC       "4x4 1G Box Cover"

So this module does not merge records. It **classifies** them, and the only
thing it collapses is a row that is duplicated in every field — where keeping
one is provably lossless.

**Why the SKU collision matters more than it looks.** `store.save` upserts on
SKU. Before this module existed, ingesting that sheet enriched both AVM6EV rows
— paying twice — and then the second silently overwrote the first. The catalog
ended up with one product, no warning, and a 50% chance of the wrong
description. A pipeline that loses a product without saying so is a worse
failure than one that refuses it, so colliding records are held back from
enrichment and reported.

**Why shared content is flagged, not merged.** Those three box covers are
genuinely different parts (different knockout sizes) whose descriptions are too
sparse to tell apart. Merging them would delete two products; ignoring them
would ship three identical catalog pages. Flagging them is the only honest
answer, and it is exactly the "needs human review" surface the brief calls a
genuinely valuable feature.

The three verdicts, and what each costs if you get it wrong:

| Verdict | Rule | Action | Cost of the alternative |
|---|---|---|---|
| `identical` | same SKU, every field equal | keep one | re-enriching a byte-identical row |
| `collision` | same SKU, fields differ | hold back, report | silent overwrite, one product lost |
| `shared_content` | different SKU, same content | enrich, flag | three identical catalog pages |
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .models import RawRecord


@dataclass
class DedupReport:
    """What ingest found, and what it decided to do about it."""

    # Records safe to enrich. Byte-identical repeats already collapsed.
    unique: list[RawRecord] = field(default_factory=list)
    # SKU -> the differing raw rows. Held back from enrichment on purpose.
    collisions: dict[str, list[RawRecord]] = field(default_factory=dict)
    # Normalised content -> the SKUs sharing it. Enriched, but flagged.
    shared_content: dict[str, list[str]] = field(default_factory=dict)
    collapsed: int = 0

    @property
    def flagged_skus(self) -> set[str]:
        """Every SKU a human should look at before this catalog is published."""
        shared = {sku for skus in self.shared_content.values() for sku in skus}
        return shared | set(self.collisions)

    def summary(self) -> str:
        parts = [f"{len(self.unique)} unique"]
        if self.collapsed:
            parts.append(f"{self.collapsed} identical row(s) collapsed")
        if self.collisions:
            parts.append(f"{len(self.collisions)} SKU collision(s) held back")
        if self.shared_content:
            groups = len(self.shared_content)
            skus = sum(len(v) for v in self.shared_content.values())
            parts.append(f"{skus} record(s) in {groups} shared-description group(s)")
        return ", ".join(parts)


def content_key(record: RawRecord) -> str:
    """What makes two records the same *product*, ignoring the part number.

    Punctuation and case are stripped because "4x4 1G Box Cover" and
    "4X4 1g box cover" are one description written twice, not two products. The
    SKU is deliberately excluded — including it would make every row unique and
    the whole check vacuous.
    """
    attributes = " ".join(f"{k}={v}" for k, v in sorted(record.attributes.items()))
    blob = f"{attributes} {record.text}".casefold()
    return re.sub(r"[^\w]+", " ", blob, flags=re.UNICODE).strip()


def _identity(record: RawRecord) -> tuple:
    """Full equality, including the SKU and the untouched source row."""
    return (record.sku, content_key(record), tuple(sorted(record.raw.items())))


def analyse(records: list[RawRecord]) -> DedupReport:
    """Classify a batch. Pure function — no I/O, no model calls, no mutation."""
    report = DedupReport()

    by_sku: dict[str, list[RawRecord]] = defaultdict(list)
    for record in records:
        by_sku[record.sku].append(record)

    for sku, group in by_sku.items():
        if len(group) == 1:
            report.unique.append(group[0])
            continue
        distinct = {_identity(r): r for r in group}
        if len(distinct) == 1:
            # Byte-identical repeat: keeping one loses nothing at all.
            report.unique.append(group[0])
            report.collapsed += len(group) - 1
        else:
            # Same identifier, different products. We cannot store both under
            # one primary key and have no grounds to pick, so neither is
            # enriched and both are reported.
            report.collisions[sku] = group

    # Shared content is computed over what survives, so a held-back collision
    # does not also generate a content-group finding about itself.
    by_content: dict[str, list[str]] = defaultdict(list)
    for record in report.unique:
        by_content[content_key(record)].append(record.sku)
    report.shared_content = {
        content: skus for content, skus in by_content.items() if len(skus) > 1
    }
    return report


def describe(report: DedupReport, limit: int = 5) -> list[str]:
    """Human-readable findings, for the CLI and the audit trail."""
    lines: list[str] = []
    for sku, group in list(report.collisions.items())[:limit]:
        variants = " | ".join(sorted({r.text or "(no description)" for r in group}))
        lines.append(
            f"  SKU COLLISION {sku}: {len(group)} rows describing different products "
            f"— {variants[:120]}. Held back: storing both under one part number "
            f"is impossible and choosing between them is a guess."
        )
    if len(report.collisions) > limit:
        lines.append(f"  ... and {len(report.collisions) - limit} more collision(s)")

    for skus in list(report.shared_content.values())[:limit]:
        lines.append(
            f"  SHARED DESCRIPTION: {', '.join(sorted(skus))} are different part "
            f"numbers with identical input text — enriched, but flagged for review."
        )
    if len(report.shared_content) > limit:
        lines.append(f"  ... and {len(report.shared_content) - limit} more group(s)")
    return lines

# ponytail: exact-match content comparison only. Near-duplicates ("4x4 1G Box
# Cover" vs "4x4 1 Gang Box Cover") are not detected, and the fuzzy matching
# that would catch them needs a similarity threshold that only the labelled
# 200-row sheet can tune — guessing one risks merging distinct parts, which is
# the expensive direction of this trade. Exact matching finds the real cases in
# the shipped data and never fires falsely.
