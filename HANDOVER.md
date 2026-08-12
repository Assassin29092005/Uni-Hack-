# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-12 23:55 IST

---

## Where things stand

**All 8 build-plan milestones are shipped and demonstrated.** The pipeline has
run end to end against the live Gemini free tier (5/5 records, `failed_records=0`),
the abstention case holds (`XYZZY-99999` → 0% complete, 0.00 confidence, four
gaps with per-field reasons), and re-running skips finished SKUs without spending
a call.

This session was an audit-and-repair pass with **no API key on the machine**, so
everything done is offline-verifiable. Four claims in `CLAUDE.md` that the code
did not actually back are now backed, and two real bugs were found — one of them
live since the first commit.

```
src/models.py      schema — Sourced/Spec/Product/Issue/ValidationReport
src/normalize.py   deterministic units, casing, aliases — input AND output
src/llm.py         provider layer: gemini (default) | ollama | anthropic
src/enrich.py      prompts + enrich pass + validate pass + apply_report
src/checks.py      deterministic validation rules (new this session)
src/store.py       SQLite persistence + append-only audit trail + export/seed
src/pipeline.py    ingest, orchestration, CLI
src/app.py         web UI (catalog + provenance + validation findings) and JSON API
src/probe.py       adversarial validator check (planted errors + control)
src/golden.py      accuracy harness vs data/golden.json
test_pipeline.py   32 checks, all passing
data/golden.json   32 hand-checked records, 24 of them traps
data/sample_products.csv   5 deliberately messy rows
```

## Done this session (all offline, no quota spent)

- **`src/checks.py`** — four deterministic rules, same `Issue` type as the model,
  merged in `process()`. Runs even when the validation API call failed, so a
  record whose audit died is degraded rather than unexamined. DECISIONS 019.
- **Output normalization** — `normalize_specs` canonicalises the model's own spec
  names and units. Exact duplicates merge; contradictions are deliberately kept
  for the rules to report. DECISIONS 020.
- **The UI now shows what the validator found.** Reports were persisted from the
  first commit and nothing ever read them back. Also distinguishes "no issues"
  from "never ran".
- **Pagination** on the catalog page and `/api/products`. Both were unbounded
  `SELECT`s.
- **`--export` / `--seed`** so a demo never needs live quota. DECISIONS 021.
- **BUG-005 fixed** — `normalize_key` deleted every non-Latin character. Three of
  four attributes on our own Chinese golden record were being dropped *before*
  enrichment; French names were mangled. Live since the first commit.
- **BUG-006 fixed** — the `?page=` route parameter shadowed the `page()` renderer
  and every catalog request raised `TypeError` while all 19 tests passed.
- **Tests 19 → 32.** Every new rule has a control case, including one asserting
  the deterministic rules stay silent on the exact fixture `probe.py` requires
  the LLM validator to call clean.

## In progress

Nothing mid-edit. The repo is in a consistent, runnable state.

## Next up

**Everything here needs an API key, which this machine does not have.** In
priority order once one is available:

1. **Re-run the golden set.** This is now the blocker on every accuracy claim.
   Three separate reasons the cached numbers are stale:
   - `golden_scores.json` **is gone from this working tree** (gitignored), so the
     28 accumulated scores no longer exist. `--report` currently prints "No
     scores cached yet."
   - BUG-005 means the two non-English records were scored on inputs we had
     silently deleted three quarters of.
   - The new deterministic rules change what gets published, so hallucination
     **and** grounding will both move.
   Prefer one model for the whole set if quota allows (DECISIONS 018).
2. **Settle the BUG-004 confound — 6 calls, cheap.**
   `python -m src.golden --only MISL-1756-IF16-XT,MISL-6205-2RS-C3-77,MISL-VLV-12-150-316 --force`
   Do not apply the drafted prompt fix before knowing whether it is a prompt
   problem or a model-choice problem.
3. **Create the demo seed.** Run the pipeline once, then
   `python -m src.pipeline --export data/demo_catalog.json` and commit it. After
   that the UI demos on any machine with `--seed` and zero API calls. Do **not**
   hand-write this file (DECISIONS 021).
4. **Re-run `python -m src.probe`** as one clean sweep — the README's table is
   correct but was assembled across two runs.
5. Then, and only then, update the README Results numbers.

## Broken / known issues

- **No accuracy number is currently defensible.** The README's figures (97%
  grounding, 96% abstention, 6 hallucinations) were true when measured, but the
  score cache is gone, two records were scored on corrupted input (BUG-005), and
  the pipeline has changed since. Treat them as historical until re-run.
- **No `catalog.db` and no `golden_scores.json` in this tree.** Both gitignored.
  The UI renders "No products yet" until something enriches or seeds.
- **BUG-004 open, now mitigated not fixed.** The rules stop identifier-decoded
  specs being *published*; the model still generates them, and a spec inferred
  without saying so in its evidence still escapes.
- **The rules' effect on grounding is unmeasured.** They may suppress legitimate
  inference along with the hallucinations. Only the golden set can tell.
- **Free-tier quota: 20 requests/day/model**, measured from the API's own 429.
  Two calls per record ≈ 10 records/day/model. Quota is per-model, so
  `GEMINI_MODEL=gemini-2.5-flash-lite` gets a fresh bucket.
- **Ingest is CSV-only** while `CLAUDE.md` scopes CSV/JSON/PDF/URL. Visible in
  the schema: `source: document` and `source: web` are declared and unreachable.
- 4 of 32 golden records were never scored even before the cache was lost.
- Headline accuracy blended 4 models (quota rotation). See DECISIONS 018.
- Compound dimension strings (`"25 MM x 200MM"`) pass through normalization
  untouched — deliberate, the model reads them. Pinned by a test.
- Single model per run; no per-record tiering (DECISIONS 007).

## Avoid

- Don't add auth, multi-tenancy, microservices, or a plugin system.
- Don't quote a new accuracy figure until the golden set has actually been re-run.
- Don't hand-write `data/demo_catalog.json`. Fabricated records in a project
  about provenance is the one unrecoverable own-goal.
- Don't let `apply_report` raise confidence — deliberately one-way, test-guarded.
- Don't let `checks.merge` mutate the model's report; `probe.py` measures that
  report alone and would start scoring our lookup tables as the model's judgement.
- Don't spend model calls on deterministic work; extend `normalize.py` or
  `checks.py` instead.
- Don't tune prompts without the golden set. You'll be guessing.
- Don't swap SQLite for Postgres until concurrent writes actually hurt.

## Open questions for the user

- Is there an organizer-provided dataset, or do we keep sourcing our own samples?
- Submission deadline and demo format (live / recorded / repo only)?
- Team size — is anyone else committing to this repo?
- Where did the working key go? The pipeline has run live before, so one existed;
  `.env` is absent now and it gates every remaining task.
