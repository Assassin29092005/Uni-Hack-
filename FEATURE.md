# FEATURE

One section per feature, newest at top. Scoped → built → verified, traced end to end.

Split a feature into its own file only if its section outgrows this one.

Template:

```
## FEAT-NNN — <name>   [SCOPED | IN PROGRESS | DONE | DROPPED]
**Scoped:** what it must do, and explicitly what it must NOT do
**Why:** which judging outcome or user need it serves
**Design:** approach chosen; alternatives rejected (link DECISIONS.md if it warranted an entry)
**Touches:** files/stages (see FLOW.md)
**Progress:**
  - <what was built> → <state>
**Verified:** the check that proves it works
**Left out:** deliberate gaps, and when they'd be worth filling
```

The `Left out` line matters most under a deadline — it separates "not built yet" from
"decided against," and stops a future session from rebuilding something we cut on purpose.

---

## FEAT-001 — Enrichment pipeline, ingest through persist   [IN PROGRESS]

**Scoped:** Take a CSV of sparse industrial product records and produce
structured, validated, provenance-carrying `Product` records in SQLite, resumable
across interruptions. Milestones 1–6 of the build plan in `CLAUDE.md`.

Explicitly **not** in scope for this feature: any UI (milestone 7), the scale
story (milestone 8), model tiering, or the Batch API.

**Why:** All four judging outcomes route through it — structured data generation,
accuracy/consistency, AI validation, and the catalog engine. Nothing else can be
demoed until this runs.

**Design:** Six-stage pipeline, uniform `list[record] → list[record]` per stage so
any stage is skippable and testable alone. Provenance made structural via the
`Sourced` type rather than enforced by convention. Enrich and validate are
separate Claude calls with separate prompts. See DECISIONS 007–010 for the
choices made while building; 001 for the provenance rule itself.

**Touches:** `src/models.py`, `src/normalize.py`, `src/enrich.py`, `src/store.py`,
`src/pipeline.py`, `test_pipeline.py`, `data/sample_products.csv`. Flow map and
verified line numbers in `FLOW.md`.

**Progress:**
- Schema, normalization, both LLM passes, store, CLI → written
- Provider layer added (`src/llm.py`) after the user asked why we were paying
  for an API: Gemini free tier is now the default, Ollama runs fully local, the
  Anthropic path is retained. No file outside `llm.py` imports a vendor SDK.
- Rate-limit retry + provider-aware worker defaults — free tiers throttle per
  minute, and without this the free option would not actually run
- `test_pipeline.py` → **13 checks passing**
- CSV ingest verified against `data/sample_products.csv` (5 rows, `MPN`
  auto-detected as the SKU column)
- Missing-credential and unknown-SKU paths → clean messages, no traceback
- **Live API run → not done.** No credentials on this machine.

**Verified:** `python test_pipeline.py` → `10 passed`. Covers unit/alias
normalization, ingest, confidence gating, completeness scoring (including that
padded specs don't inflate it), the one-way confidence rule, store idempotency,
and the failed-record path.

Not yet verified: enrichment *quality*, on any provider. The prompts have never
been executed. The specific check that matters is that `XYZZY-99999` — the
deliberately empty row in the sample CSV — comes back mostly null with reasons
rather than confidently populated. Second open question now that the default is
a free model: whether free-tier output is good enough, since the prompts were
written against Claude's behavior.

**Left out:**
- Model tiering (DECISIONS 007) — needs data on which records are hard
- Batch API (DECISIONS 008) — needed for the real scale story, not for the demo
- Golden set — the actual blocker on tuning prompts safely
- Field-level resume: `is_done` skips whole records, not individual empty fields.
  Fine while records are cheap; revisit if partial re-enrichment becomes common.
