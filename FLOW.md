# FLOW

How execution actually travels — file to file, function to function, in order.

Last updated: 2026-08-12 01:20 IST

---

## ACTUAL (verified against real code)

Entry point: `python -m src.pipeline <csv>` → [`pipeline.main`](src/pipeline.py:117)

```
main()                              src/pipeline.py:117
 ├─ --show SKU ─► show()                           :97   read-only provenance view
 └─ run(csv, db, force, workers)                   :59
      ├─ store.connect(db)          src/store.py:49      creates schema if absent
      ├─ ingest_csv(csv)            src/pipeline.py:28
      │    └─ normalize_record()    src/normalize.py:95  ─► list[RawRecord]
      ├─ store.is_done(sku)         src/store.py:57      ◄── the resumability gate
      ├─ process_batch(todo)        src/enrich.py
      │    ├─ llm.check_ready()     src/llm.py            fail fast on setup problems
      │    └─ ThreadPoolExecutor ─► process()                    per record:
      │         ├─ enrich_one()   ─► llm.structured_call()       model call 1
      │         ├─ validate_one() ─► llm.structured_call()       model call 2
      │         └─ apply_report()                                pure Python
      ├─ store.save(product, report)      src/store.py:68   products + 2 audit rows
      │  └─ (on failure) store.save_error()           :96   audit only, no product row
      └─ store.summary()                             :115
```

**Stage-to-code map** (the conceptual pipeline in `CLAUDE.md`, grounded):

| Stage | Where it lives | LLM? |
|---|---|---|
| ingest | `pipeline.ingest_csv` | no |
| normalize | `normalize.normalize_record` | no |
| enrich | `enrich.enrich_one` → `llm.structured_call` | **yes** |
| validate | `enrich.validate_one` → `llm.structured_call` | **yes** |
| score | `models.Product.completeness` / `.mean_confidence` / `.gaps` (properties) | no |
| persist | `store.save` | no |

### The provider seam

Every model call in the project goes through exactly one function:
`llm.structured_call(system, user, schema) -> BaseModel`. It picks a provider
from `LLM_PROVIDER` (`gemini` default / `ollama` / `anthropic`), retries on
rate limits, and validates the reply against the pydantic class before returning.

No other file imports a vendor SDK. If you find yourself adding
`import google.genai` or `import anthropic` outside `src/llm.py`, that's the
seam leaking — put it behind `structured_call` instead. This is what makes
switching providers a config change (DECISIONS 011).

### Four invariants the flow depends on

**1. `is_done` is checked before any model call.** `run()` filters the record
list through `store.is_done` before `process_batch` is reached, so killing a
catalog run and restarting re-pays for nothing. Move that check downstream and
resumability silently disappears.

**2. Enrich and validate are separate API calls with separate prompts.** They
share a client, not a conversation — the validator has never seen the enrichment
happen. Collapsing them into one call would produce a self-approving record and
would remove the "AI validation" judging criterion from the demo.

**3. Failures leave the catalog untouched but the audit populated.**
`process_batch` catches per-record exceptions and returns them as a fourth tuple
element rather than raising, so one bad record can't kill a 10k-row run.
`run()` then calls `save_error`, which writes an audit row and **no** products
row — a half-enriched product is worse than a missing one.

**4. Setup failures abort; record failures don't.** `ProviderError` (missing key,
Ollama not running) is deliberately re-raised past that catch. It would recur
identically for every remaining record, so failing 10,000 rows one at a time
would bury the single line telling the user what to fix.

### Scoring is derived, never received

`completeness`, `mean_confidence`, and `gaps` are `@property` on `Product`, so
they are absent from the JSON schema sent to Claude and cannot be model-supplied.
See DECISIONS 010.

---

## PLANNED (not yet code)

- **Milestone 7 — UI** (`src/app.py`): FastAPI reading `store.load` / `store.summary`,
  rendering the table plus the same per-field provenance view `show()` prints.
- **Milestone 8 — scale**: Message Batches path alongside the thread pool
  (DECISIONS 008).
- **Golden set**: ~10 hand-checked products for regression-testing prompt edits.

---

## Currently modifying

*(nothing in flight)*
