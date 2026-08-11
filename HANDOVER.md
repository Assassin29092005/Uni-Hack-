# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-12 01:30 IST

---

## Where things stand

**The pipeline runs end to end against the live Gemini free tier.** First real
run: 5/5 records enriched, validated, scored, and persisted, `failed_records=0`.
Milestones 1–6 are done and *demonstrated*, not just written.

The abstention check passed, which was the make-or-break test: `XYZZY-99999`
(the deliberately empty row) came back 0% complete, 0.00 confidence, all four
core fields listed as gaps with per-field reasons — no invented specs. The core
claim of the project holds on a free model.

Repo is on GitHub: `github.com/Assassin29092005/Uni-Hack-`, branch `main`,
one commit (`21a765e`, the docs). The code below is **committed nowhere yet**.

Build plan milestones (table in `CLAUDE.md`): **1–6 done, 7–8 not started.**

```
src/models.py      schema — Sourced/Spec/Product/Issue/ValidationReport
src/normalize.py   deterministic units, casing, attribute aliases
src/llm.py         provider layer: gemini (default) | ollama | anthropic
src/enrich.py      prompts + enrich pass + validate pass + apply_report
src/store.py       SQLite persistence + append-only audit trail
src/pipeline.py    ingest, orchestration, CLI
test_pipeline.py   15 checks, all passing
data/sample_products.csv   5 deliberately messy rows
```

**The LLM provider is a config switch, not a code change** (DECISIONS 011).
Default is Google Gemini's free tier — no credit card. `LLM_PROVIDER=ollama`
runs fully local with no account at all; `LLM_PROVIDER=anthropic` is the paid
path. All three take the same prompts and the same pydantic schemas.

## Done

- **Live run, Gemini free tier, 5/5 records:**

  | SKU | verdict | complete | conf | gaps |
  |---|---|---|---|---|
  | HDX-4025-200 | pass | 100% | 0.94 | — |
  | RLY-24DC-4PDT | pass | 100% | 0.93 | — |
  | SKF-6205-2RS | pass | 100% | 0.97 | — |
  | FST-M8X30-A2 | pass | 80% | 0.89 | brand |
  | XYZZY-99999 | pass | 0% | 0.00 | name, brand, category, description |

  Evidence quality is good: the cylinder's stroke was inferred from
  `"25 MM x 200MM"` at 0.80 confidence *with the reasoning stated*, while its
  bore — quoted verbatim from the input — came back at 1.00. That gradient is
  exactly what the provenance model was for.
- Resumability confirmed: a second run prints `Skipping 5 already-enriched
  record(s)` and makes zero API calls.
- BUG-001 found and fixed on the first live run (schema dialect clash). See
  `BUG.md`; guarded by a test.
- `python test_pipeline.py` → **15 passed**. Covers normalization, ingest,
  confidence gating, completeness scoring, validator semantics, store
  idempotency, the failed-record path, provider dispatch, rate-limit detection,
  per-provider schema dialects, and permanent-vs-retryable error classification.
- CSV ingest verified end to end: 5 rows parsed, `MPN` auto-resolved to the SKU
  column, `Notes` folded into the free-text blob.
- Error paths all print one clean message rather than a traceback or per-record
  spam: missing Gemini key, Ollama not running, Ollama model not pulled, unknown
  provider, `--show` on an unknown SKU.
- Provenance is structural — `Product.name` is a `Sourced` object, not a `str`,
  so no code path can emit a value without evidence and confidence.
- Console output forced to UTF-8; Windows cp1252 was mangling `°`, `·`, `—`.

## In progress

Nothing mid-edit. The repo is in a consistent, runnable state.

## Next up

1. **Commit the code.** Only the docs are on GitHub; `src/`, tests, and sample
   data are untracked. This is the biggest risk in the repo right now.
2. **Probe the validator.** It returned `0 issues` on all 5 records. That is
   either a clean batch or a rubber stamp, and we cannot currently tell which —
   see Broken below. Feed it a deliberately wrong record and check it objects.
3. Build the golden set (~10 hand-checked products). Double duty: catching
   prompt regressions, and proving free-tier quality is demo-grade.
4. Milestone 7: minimal UI (table + detail panel with evidence and confidence).
   `pipeline.show()` already produces exactly this view in text — port it.
5. Milestone 8: scale numbers.

## Broken / known issues

- **The validator has never actually objected to anything.** 0 issues across 5
  records on its first outing. `apply_report` and the whole
  contradiction-catching story are therefore only proven by unit test, never by
  a real model finding a real problem. Until a deliberately-wrong record makes
  it complain, treat "AI validation" as unverified — it is one of the four
  judging criteria and the most likely thing to be quietly hollow.
- **Code is uncommitted.** Only the docs are on GitHub.
- Enrichment quality is proven on 5 records, all hand-picked by us. That is a
  smoke test, not evidence.
- Free tiers throttle per *minute*, so worker defaults are 2 and a batch of 10
  takes minutes. Plan the demo around a pre-enriched database rather than a cold
  run on stage — the store is resumable precisely so this works.
- Compound dimension strings (`"25 MM x 200MM"`) pass through normalization
  untouched — deliberate, the model reads them. Documented by a test so it
  can't change silently.
- Single model per run; no per-record tiering (DECISIONS 007).

## Avoid

- Don't add auth, multi-tenancy, microservices, or a plugin system.
- Don't let `apply_report` raise confidence — it is deliberately one-way, so a
  second opinion can't launder a bad first one. There's a test guarding this.
- Don't spend model calls on deterministic work; extend `normalize.py` instead.
- Don't tune prompts without the golden set. You'll be guessing.
- Don't swap SQLite for Postgres until concurrent writes actually hurt.

## Open questions for the user

- Is there an organizer-provided dataset, or do we keep sourcing our own samples?
- Submission deadline and demo format (live / recorded / repo only)?
- Team size — is anyone else committing to this repo?
