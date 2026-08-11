# BUG

One section per bug, newest at top. Trace it start to finish — a cold reader (human or
AI) should be able to pick up mid-investigation without asking anyone anything.

Split a bug into its own file only if its section outgrows this one.

Template:

```
## BUG-NNN — <one-line symptom>   [OPEN | FIXED | WONTFIX]
**Found:** how it surfaced (demo, test, user report) + date
**Symptom:** exact observed behavior. Quote real error text.
**Scope:** files/stages on the suspect path (see FLOW.md)
**Tried:**
  - <attempt> → <result>
**Root cause:** the actual why, not the symptom
**Fix:** what changed, where
**Verified:** how we know it's fixed — the specific check that now passes
```

Record failed attempts too. "We tried X, it didn't work" saves the next session from
re-running the same dead end, and that is most of this file's value.

---

## BUG-003 — Probe scored API failures as successful detections   [FIXED]

**Found:** 2026-08-12, first run of `src/probe.py`. It reported "Planted errors
caught: 5/5", which was wrong.

**Symptom:** Two cases showed
`-> unsupported on *: Validation pass did not complete (RuntimeError)`.
The `overconfident-guess` case was scored **PASS** on the strength of that line.

**Root cause:** `validate_one` returns a degraded `ValidationReport` when the
audit fails, so the record fails safe. But "the validator found a problem" and
"the validator never ran" are then *both* non-empty issue lists. The probe scored
on `len(report.issues) > 0`, so an API failure was indistinguishable from a
detection — the measuring instrument reported success when it had measured
nothing.

Worth naming: this class of bug is the same one the whole project is about.
A number that looks plausible and isn't grounded in what actually happened.

**Fix:** `UNAUDITED_MARKER` + `enrich.is_unaudited(report)`. The probe and the
golden harness both check it and exclude those cases from scoring, counting them
as errors instead. Summary now prints `NOT MEASURED` when the control didn't run,
because a catch rate without a working control means nothing.

**Verified:** `test_unaudited_report_is_distinguishable`. Confirmed live — the
re-run reported `0/0` and `NOT MEASURED` under a quota outage rather than
inventing a pass.

---

## BUG-002 — Retries always gave up before the quota window reopened   [FIXED]

**Found:** 2026-08-12, while investigating why all 6 probe cases errored.

**Symptom:** Every call failed after 4 attempts. The provider's own message said:

```
429 RESOURCE_EXHAUSTED ... limit: 20, model: gemini-2.5-flash
Please retry in 41.812865242s.
```

**Root cause:** Backoff was a fixed 4s/8s/16s = **28 seconds total**. The server
was asking for **42**. Every retry fired inside the closed window and failed, so
a fully recoverable rate limit was reported as a dead record. The retry logic
looked reasonable and was, in this quota's shape, guaranteed to fail.

**Fix:** `llm.retry_delay_from()` parses the delay the provider actually states
(`retry in Ns`, `retryDelay: 'Ns'`, `Retry-After: N`), adds 1s of slack, and
caps at `MAX_BACKOFF`. Falls back to 8/16/32 when no delay is given.

**Verified:** `test_retry_honours_server_stated_delay`, using the verbatim
Gemini message plus the other two spellings, and asserting the cap holds against
an absurd stated delay.

---

## BUG-001 — Every Gemini call rejected with 400 INVALID_ARGUMENT   [FIXED]

**Found:** 2026-08-12, first live run of `python -m src.pipeline
data/sample_products.csv`. All 5 records failed.

**Symptom:** Each record failed after 4 attempts with:

```
400 INVALID_ARGUMENT ... Unknown name "additional_properties" at
'generation_config.response_schema': Cannot find field.
```

Secondary symptom, and the more annoying one during diagnosis: each record
retried 4 times before failing, so the terminal filled with
`[ClientError; retry N/3]` and 5 near-identical wall-of-JSON errors, burying the
one line that mattered.

**Scope:** `src/models.py` (schema generation) → `src/llm.py:_call_gemini`
(request construction) and `structured_call` (retry policy). See FLOW.md → the
provider seam.

**Tried:**
- Read the error rather than the retry noise → it names one field,
  `additional_properties`, repeated per nested object. Not a quota or key issue,
  despite the retry loop having framed it as transient.
- Inspected the generated schema directly:
  `'additionalProperties' in str(Product.model_json_schema())` → `True`.

**Root cause:** `model_config = {"extra": "forbid"}` on the shared pydantic
models. Pydantic emits `additionalProperties: false` for those, which the
Anthropic structured-output API **requires** — and which Gemini's
`response_schema` **rejects outright**. That comment was written while the
project was Anthropic-only (DECISIONS 002) and became wrong the moment a second
provider arrived (DECISIONS 011). Neither provider is misbehaving; the two
dialects genuinely disagree, and a single shared schema cannot satisfy both.

The existing `test_schemas_survive_json_schema_conversion` did not catch it: it
asserted the schema *generates*, never that a provider would *accept* it.

**Fix:**
1. Removed `extra: "forbid"` from all four models — the shared schema is now the
   permissive dialect, which Gemini accepts.
2. Added `llm._close_objects()`, which recursively injects
   `additionalProperties: false`, applied only in the Anthropic branch. One
   schema, two dialects, and the difference lives at the provider seam where it
   belongs rather than in the data model.
3. Separately: added `_is_permanent()` so 400/401/403/404 fail immediately
   instead of retrying. A malformed request fails identically forever; retrying
   it wasted quota and hid the message.

**Verified:**
- `test_schema_dialects_differ_per_provider` — asserts the shared models emit
  `additionalProperties` *nowhere* (with an error message telling the next person
  not to re-add `extra: forbid`), that `_close_objects` adds it at root *and*
  nested, and that it doesn't mutate its input.
- `test_permanent_errors_are_not_retried` — 400/401/403 permanent, 429/503/
  connection errors still retryable.
- Live: full 5-record run completes, `products=5, failed_records=0`.
