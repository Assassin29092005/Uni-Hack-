# FLOW

How execution actually travels — file to file, function to function, in order.

Last updated: 2026-08-14 (delivery export + ground-truth scorer)

---

## ACTUAL (verified against real code)

Entry point: `python -m src.pipeline <csv>` → [`pipeline.main`](src/pipeline.py:117)

```
main()                              src/pipeline.py
 ├─ --show SKU   ─► show()                              read-only provenance view
 ├─ --export PATH► export_catalog() ─► store.export_all()   catalog -> JSON
 ├─ --seed PATH  ─► seed_catalog()  ─► store.seed()         JSON -> catalog, 0 calls
 └─ run(csv, db, force, workers)
      ├─ store.connect(db)          src/store.py         creates schema if absent
      ├─ ingest_csv(csv)            src/pipeline.py
      │    └─ normalize_record()    src/normalize.py    ─► list[RawRecord]
      ├─ dedup.analyse(records)     src/dedup.py         ◄── before any model call
      │    ├─ unique         ─► enriched
      │    ├─ collisions     ─► store.save_error, NOT enriched
      │    └─ shared_content ─► enriched, flagged for review
      ├─ store.is_done(sku)         src/store.py         ◄── the resumability gate
      ├─ attach_sources(todo)       src/pipeline.py      only for records to enrich
      │    └─ sources.resolve()     src/sources.py       local doc > web, policy-checked
      ├─ process_batch(todo)        src/enrich.py
      │    ├─ llm.check_ready()     src/llm.py            fail fast on setup problems
      │    └─ ThreadPoolExecutor ─► process()                    per record:
      │         ├─ enrich_one()   ─► llm.structured_call()       model call 1
      │         │    └─ normalize_specs()                        pure Python
      │         ├─ validate_one() ─► llm.structured_call()       model call 2
      │         ├─ checks.merge() ─► run_checks()                pure Python
      │         └─ apply_report()                                pure Python
      ├─ store.save(product, report)      src/store.py     products + 2 audit rows
      │  └─ (on failure) store.save_error()               audit only, no product row
      └─ store.summary()
```

**Stage-to-code map** (the conceptual pipeline in `CLAUDE.md`, grounded):

| Stage | Where it lives | LLM? |
|---|---|---|
| ingest | `pipeline.ingest_csv` | no |
| de-duplicate | `dedup.analyse` | no |
| normalize (in) | `normalize.normalize_record` | no |
| source (manufacturer docs) | `pipeline.attach_sources` → `sources.resolve` | no |
| enrich | `enrich.enrich_one` → `llm.structured_call` | **yes** |
| normalize (out) | `normalize.normalize_specs`, inside `enrich_one` | no |
| validate | `enrich.validate_one` → `llm.structured_call` | **yes** |
| validate (rules) | `checks.run_checks` via `checks.merge` | no |
| score | `models.Product.completeness` / `.mean_confidence` / `.gaps` (properties) | no |
| persist | `store.save` | no |

Normalization appears twice on purpose (DECISIONS 020): input is cleaned before
the model sees it, and the model's own spec names and units are canonicalised
before anything validates or stores them.

### The provider seam

Every model call in the project goes through exactly one function:
`llm.structured_call(system, user, schema) -> BaseModel`. It picks a provider
from `LLM_PROVIDER` (`gemini` default / `ollama` / `anthropic`), retries on
rate limits, and validates the reply against the pydantic class before returning.

No other file imports a vendor SDK. If you find yourself adding
`import google.genai` or `import anthropic` outside `src/llm.py`, that's the
seam leaking — put it behind `structured_call` instead. This is what makes
switching providers a config change (DECISIONS 011).

### Validation has two halves that must not merge

`process()` calls the model validator, then `checks.merge`. They produce the same
`Issue` type and flow through the same `apply_report`, but they are different
instruments:

- **`validate_one`** — one API call, adversarial framing, catches what needs
  world knowledge (an implausible mass, circular evidence).
- **`checks.run_checks`** — pure Python, no network, catches what is mechanically
  checkable (provenance claimed as `input` for a value not in the input, a spec
  decoded from the part number, a unit from the wrong family, two specs
  contradicting each other). Runs **even when the API call failed**, so a record
  whose validation died is degraded rather than unexamined.

`checks.merge` returns a *new* report and never mutates the model's. That matters
because `probe.py` measures the LLM validator alone: an instrument that silently
received rule output would report our lookup tables as the model's judgement.
`probe.py` therefore calls `validate_one` directly and bypasses the rules;
`golden.py` calls `process()` and sees both, because it measures the system.

### The input block and the prompt block are not the same string

`RawRecord.as_input_block()` is the distributor's row. `as_prompt_block()` is
that plus any `<document>` retrieved for it. The model reads the second;
`checks.unsupported_input_claims` searches the **first**.

That split is load-bearing. The rule asks "did the record say this?", and once a
datasheet joins the prompt, a haystack built from the whole prompt would accept
a spec lifted off the manufacturer's page as though the distributor had stated
it — inverting the one claim the rule exists to falsify. Document-sourced values
are perfectly legitimate; they just have to say `source: document`, and the
prompt tells the model so explicitly.

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

### Second entry point: the UI

`python -m src.app` → uvicorn → FastAPI. **Read-only** — it never enriches, it
only renders what `pipeline` already persisted. That separation is why the demo
is safe: the UI cannot be slowed or broken by a rate limit, because it makes no
model calls at all.

```
GET /?page=N          catalog()        store.summary + one page of products
GET /product/{sku}    product_detail() store.load      -> render_field() per field
                                       store.load_report -> render_validation()
GET /api/products     api_products()   commerce-ready JSON, ?limit= &offset=
GET /api/product/{sku}
```

`render_field()` is where the honest-gaps story becomes visible: a field below
the publish threshold renders as a dashed box reading "no grounded value" with
its reason, rather than being omitted. Everything it interpolates is model
output, so it all goes through `esc()`.

`render_validation()` is the other half of that story and was missing until
2026-08-12: the reports were persisted from the first commit and nothing read
them back, so what the validator *found* existed only in the database. It also
distinguishes "no issues" from "never ran", for the same reason `is_unaudited`
exists (BUG-003).

Both list routes are paged. The catalog table was an unbounded `SELECT` that
built every row into one HTML string, which is not a 10k-row story.

The route parameter `page` is why the renderer is called `render_page` — see
BUG-006, where the two names collided and every catalog request died while the
whole test suite stayed green.

### Third entry point: the deliverable

`python -m src.delivery out.csv` → their 252 columns + a provenance sidecar.

```
export()                              src/delivery.py
 ├─ store.connect ─► SELECT sku, data, raw FROM products
 └─ per record:
      ├─ to_row(product, raw)
      │    ├─ passthrough    raw -> Mfg_Part_Num, Part_Desc, the 3 brand
      │    │                 columns, Part_Manuf, Dept/Class/Fine
      │    └─ generated_fields(product) -> GENERATED_COLUMNS, via _FORMATTERS
      ├─ provenance_rows()  ─► the SAME generated_fields list, + every spec
      └─ checks.delivery_checks() -> format compliance, printed per run
```

**`generated_fields` is the load-bearing part.** `to_row` and `provenance_rows`
iterate one list, so a column cannot ship without a provenance row explaining
it — they were two hand-maintained lists until BUG-010 D5, and the shorter one
covered five of the nine columns actually being emitted.

**Passthrough and generated are different categories, not a spectrum.**
Dept/Class/Fine are the distributor's taxonomy and are *copied*; Classpath is
ours and is *asserted*. Deriving the first from the second (which this exporter
did until BUG-010 D1) overwrites supplied data and emits columns nothing
explains. We owe provenance for what we assert, not for what we were handed.

### Verification harnesses (probe and golden cost real API calls)

```
python -m src.probe            probe.main()  -> validate_one() per planted error
python -m src.golden           golden.main() -> process() per golden record, then score()
python -m src.truth            truth.main()  -> to_row() vs their Delivery Format CSV
python -m src.truth --control  the same comparator, their rows against themselves
```

`probe` and `golden` call `enrich.is_unaudited()` on every report before scoring
it. A report that says "the audit never ran" and one that says "I found three
problems" are both non-empty issue lists, and an instrument that conflates them
scores its own API failures as successful detections.

`truth` makes **no** model calls — it reads the stored catalog — and carries the
same idea in two other forms. Its control (`--control`, also run by the test
suite) scores their rows against themselves and must come back a clean sweep: a
comparator that cannot grade a known-good row as correct says nothing about a
row it grades badly. And a run with no overlapping SKUs raises rather than
printing "100% accurate (0 records)".

The three answer different questions and their numbers must not be pooled:
probe asks *does the validator object?*, golden asks *does the model invent?*
against expectations we wrote, truth asks *does the output match the client's?*
against expectations they wrote. See DECISIONS 025.

---

## PLANNED (not yet code)

- **Batch API path** alongside the thread pool for a real 100k-row catalog
  (DECISIONS 008).
- **Per-field resume**: `is_done` currently skips whole records, not individual
  ungrounded fields.
- **Ingest beyond CSV.** `CLAUDE.md` scopes ingest as CSV/JSON/PDF/URL and only
  `ingest_csv` exists. The consequence is visible in the schema: `Source` allows
  `document` and `web`, and no code path can produce either, so two of the four
  provenance kinds are currently unreachable.

---

## Currently modifying

*(nothing in flight)*
