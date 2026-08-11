# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-12 01:30 IST

---

## Where things stand

The pipeline is **built and self-tested, but has never made a real LLM call** —
no provider credentials on this machine. Everything up to the model boundary is
verified; everything past it is unproven. Treat "it works" as provisional until
someone runs it with a key.

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
test_pipeline.py   13 checks, all passing
data/sample_products.csv   5 deliberately messy rows
```

**The LLM provider is a config switch, not a code change** (DECISIONS 011).
Default is Google Gemini's free tier — no credit card. `LLM_PROVIDER=ollama`
runs fully local with no account at all; `LLM_PROVIDER=anthropic` is the paid
path. All three take the same prompts and the same pydantic schemas.

## Done

- `python test_pipeline.py` → **13 passed**. Covers normalization, ingest,
  confidence gating, completeness scoring, validator semantics, store
  idempotency, the failed-record path, provider dispatch, rate-limit detection,
  and that the schemas survive JSON-schema conversion.
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
   data are untracked.
2. **Run it live.** Get a free key at `https://aistudio.google.com/apikey`, set
   `GEMINI_API_KEY`, run `python -m src.pipeline data/sample_products.csv`.
   Until this happens the prompts are untested and enrichment quality — on any
   provider — is unknown.
3. Inspect one record with `python -m src.pipeline --show HDX-4025-200` — that
   provenance view is the demo's centrepiece.
4. Check the abstention path specifically: `XYZZY-99999` in the sample CSV is
   empty on purpose and **must** come back mostly null with reasons. If it comes
   back confidently populated, the core claim of the project is broken and that
   is the bug to fix before anything else.
5. Build the golden set (~10 hand-checked products). It now does double duty:
   catching prompt regressions, and answering whether the free model is good
   enough to demo on.
6. Milestone 7: minimal UI (table + detail panel with evidence and confidence).
7. Milestone 8: scale numbers.

## Broken / known issues

- **No live run yet** (above). Biggest unknown by far, and it now covers a
  second question: whether free-tier quality is sufficient. The prompts were
  written against Claude's behavior and may need re-tuning per provider.
- **Code is uncommitted.** Only the docs are on GitHub.
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
