# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-12 02:40 IST

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

**All 8 build-plan milestones are done and measured.** Headline numbers, all on
the free tier: **0 hallucinations**, **28/28 abstention**, 96% grounding across 8
hand-checked records; validator caught 5/5 planted faults with a clean control.
Full write-up in `README.md` → Results.

```
src/models.py      schema — Sourced/Spec/Product/Issue/ValidationReport
src/normalize.py   deterministic units, casing, attribute aliases
src/llm.py         provider layer: gemini (default) | ollama | anthropic
src/enrich.py      prompts + enrich pass + validate pass + apply_report
src/store.py       SQLite persistence + append-only audit trail
src/pipeline.py    ingest, orchestration, CLI
src/app.py         web UI (catalog + provenance detail) and JSON API
src/probe.py       adversarial validator check (planted errors + control)
src/golden.py      accuracy harness vs data/golden.json
test_pipeline.py   18 checks, all passing
README.md          judge-facing: problem, design, measured results
data/sample_products.csv   5 deliberately messy rows
data/golden.json           8 hand-checked records, 5 of them abstention traps
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

Feature work is done. What remains is polish and preparation:

1. **Commit and push.** User is handling this.
2. **Pre-enrich the demo database before presenting.** The free tier is ~10
   records/day/model; do not run a cold batch on stage. The UI is read-only and
   makes zero API calls, so a pre-populated `catalog.db` demos perfectly with
   no quota risk at all.
3. **Re-run `python -m src.probe` when quota resets** to get all six cases in a
   single clean run. The results in README are correct but were assembled across
   two runs (planted errors from one, control verified separately after the
   fixture was fixed).
4. Optional: tune the one over-caution miss — `VLV-SOL-DS` declined to assign a
   category despite the input naming it a "Solenoid Valve".
5. Optional: widen `data/golden.json` beyond 8 records.

## Broken / known issues

- **Free-tier quota is the real constraint: 20 requests/day/model**, measured
  from the API's own 429. Two calls per record ≈ 10 records/day/model. Quota is
  per-model, so `GEMINI_MODEL=gemini-2.5-flash-lite` gets a fresh bucket when
  `gemini-2.5-flash` is spent — that is how the accuracy run completed at all.
  **`gemini-2.5-flash` quota was exhausted as of 2026-08-12 02:30 IST.**
- Accuracy numbers come from `gemini-2.5-flash-lite`, the weaker model. Treat
  them as a floor. Worth re-running on `gemini-2.5-flash` when quota resets.
- Probe results were assembled across two runs, not one clean sweep (see Next up).
- Evidence base is 8 golden records + 5 sample records. Good enough to make real
  claims, not enough to call it validated at catalog scale.
- One over-caution miss (`VLV-SOL-DS` category). Failing in the safe direction.
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
