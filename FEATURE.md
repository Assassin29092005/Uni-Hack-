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

## FEAT-003 — Golden set widened 8 → 32 records   [DONE, 28/32 scored]

**Scoped:** Move the accuracy claim from "promising signal on 8 records" to
something defensible, by widening `data/golden.json` across trap categories the
original set never touched.

**Why:** Every accuracy number in the README rested on 8 records, 5 of them
traps. That is a smoke test. A judge pasting an unusual product was untested
territory, and we had no way to know it.

**Design:** 24 new records across 8 adversarial categories — non-English and
mixed-language catalogue text, self-contradictory rows, unfamiliar categories
(mining wear parts, lab glassware, HVAC dampers), unit edge cases (fractional
imperial, ranges, tolerances, bare unitless numbers), scraped HTML noise,
misleading part numbers, near-empty/placeholder rows, and rich-vs-deferred specs.

Records were drafted with model assistance, then subjected to a rule that makes
authorship irrelevant: **every expectation must be verifiable from that record's
own input alone** (DECISIONS 016), enforced mechanically by the test suite. Also
split `deferred_specs` out from `forbidden_specs` (DECISIONS 017) — the trap
where an attribute is *named but explicitly unvalued* was previously
inexpressible and had been silently dropped.

**Touches:** `data/golden.json`, `src/golden.py` (deferred_specs scoring, score
caching, model rotation, `--report`), `test_pipeline.py` (structural gate).

**Verified:** 28/32 scored. Grounding 127/131 (97%), abstention 163/169 (96%),
values 17/17, **6 hallucinations**. 19 tests pass.

**The result that matters:** widening 8 → 32 moved hallucinations from 0 to 6,
and 5 of those 6 are a single behaviour — decoding part numbers from memory
(BUG-004). The old set wasn't measuring the hard cases. Finding a specific,
reproducible, fixable failure mode is worth more than the clean scoreboard it
replaced.

**Left out:**
- The BUG-004 prompt fix is drafted but **not applied** — quota ran out, and
  shipping an unverified change to the enrichment prompt could suppress
  legitimate inference along with the hallucinations.
- 4 records unscored; the model-vs-difficulty confound unresolved (6 calls).
- Records are still English-schema'd JSON; no PDF or image inputs in the set.

---

## FEAT-002 — UI, and the two verification harnesses   [DONE]

**Scoped:** Milestones 7–8 plus the evidence needed to defend the two judging
criteria that were resting on assertion alone.

**Why:** Backend correctness that nobody can see doesn't score. And "the AI
validates its output" was, at that point, a claim supported by zero observations
of the validator ever objecting to anything.

**Design:**
- `src/app.py` — FastAPI, one file, inline HTML, no build step. **Read-only**:
  it renders what `pipeline` persisted and makes no model calls, so the demo
  cannot be broken by a rate limit. `render_field()` shows ungrounded fields as
  dashed boxes with their reason rather than hiding them — the refusal surface
  is the product, not an omission. Everything interpolated is model output, so
  all of it goes through `esc()`.
- `src/probe.py` — plants one fault per validator check, plus a **control** that
  must stay silent. Without the control a validator that flags everything scores
  perfectly.
- `src/golden.py` — scores against `data/golden.json`, weighted toward abstention
  traps. Headline metric is hallucination count.

**Progress / verified:** 0 hallucinations, 28/28 abstention, 24/25 grounding,
4/4 known values, 4.2s/record. Validator caught 5/5 planted faults; control
clean. Full tables in `README.md` → Results. UI routes smoke-tested; XSS
escaping covered by `test_ui_escapes_model_output`.

Two bugs surfaced and fixed while building this: BUG-002 (retries expired before
the quota window reopened) and BUG-003 (the probe scored API failures as
successful detections).

**Left out:**
- Automatic model rotation when a quota bucket empties — done manually via
  `GEMINI_MODEL`. Worth building if free-tier runs become routine.
- The probe's six cases have not yet run in one clean sweep (quota).
- No auth on the UI. It's a read-only local demo; adding auth is out of scope.

---

## FEAT-001 — Enrichment pipeline, ingest through persist   [DONE]

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
- **Live run: 5/5 records enriched on the Gemini free tier, 0 failures.**
  Abstention verified — the empty record returns 0% complete with per-field
  reasons rather than invented specs. Resumability verified: second run skips
  all 5 and makes no API calls.
- BUG-001 (schema dialect clash) found on that first live run and fixed
- `test_pipeline.py` → **15 checks passing**
- CSV ingest verified against `data/sample_products.csv` (5 rows, `MPN`
  auto-detected as the SKU column)
- Missing-credential and unknown-SKU paths → clean messages, no traceback
- **Live API run → not done.** No credentials on this machine.

**Verified:** `python test_pipeline.py` → `10 passed`. Covers unit/alias
normalization, ingest, confidence gating, completeness scoring (including that
padded specs don't inflate it), the one-way confidence rule, store idempotency,
and the failed-record path.

**Verified live** (Gemini free tier): 5/5 enriched, abstention correct,
resumability correct, provenance legible. Confidence tracks evidence quality —
verbatim input scored 1.00, a reasoned inference 0.80, with the reasoning stated.

**Still not verified: that the validator ever objects.** It reported 0 issues on
all 5 records. Either the enrichment was genuinely clean or the validator is
rubber-stamping, and nothing in the run distinguishes those. This is the one
judging criterion still resting on unit tests alone.

**Left out:**
- Model tiering (DECISIONS 007) — needs data on which records are hard
- Batch API (DECISIONS 008) — needed for the real scale story, not for the demo
- Golden set — the actual blocker on tuning prompts safely
- Field-level resume: `is_done` skips whole records, not individual empty fields.
  Fine while records are cheap; revisit if partial re-enrichment becomes common.
