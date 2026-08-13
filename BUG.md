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

## BUG-009 — BUG-004's fix had a hole I wrote into it   [FIXED]

**Found:** 2026-08-14, re-scoring the golden set after the delivery-format prompt
rewrite. Two hallucinations were back that BUG-004's fix had eliminated.

**Symptom:** `MISL-1756-IF16-XT` again produced brand `Allen-Bradley`, decoded
from a SKU resembling the ControlLogix scheme, on a record whose manufacturer
column is empty.

**Root cause:** my own wording. BUG-004's fix said:

> "you may name the likely manufacturer or product family as an inference — but
> you must NOT emit dimensions, materials, clearances..."

That first clause **explicitly permits** exactly this failure. The fix verified
clean at the time because the run happened to land on models that declined
anyway; the permission was always there, waiting for a model that took it.

**Fix, two layers.** The prompt now says a part number is not a source, and
brand is grounded only when the record itself contains it — "3M 775L Stikit"
names 3M, "1756-IF16-XT" with an empty manufacturer column names nobody. And
`checks.brand_not_in_record` enforces it deterministically: an `inference` brand
whose text appears nowhere in the record is flagged regardless of prompt wording.

The deterministic rule is the load-bearing half. Prose fixes decay under editing
— that is BUG-008 — and this one decayed the moment a different model read it
literally.

**Verified:** `MISL-1756-IF16-XT` re-scored on the same model that failed it:
brand hallucination gone, abstention 10/10. `test_brand_must_appear_in_the_record`
covers the decoded case, the legitimately-stated case, and column-sourced brands.
Full set back to **0 hallucinations, 201/201 abstention**.

**Also corrected while here — a bad test, not a bad model.** `DATA-FLUFF-0001`
carried `max_specs: 0`, asserting the record stated nothing extractable. It
states `"ISO 9001 certified facility"`, and the model quoted it correctly. The
expectation violated the golden set's own rule (verifiable from the input alone),
so the fixture was wrong and is now `max_specs: 1`. The fluff trap it was built
for — copy that names power ratings and voltage classes without ever giving a
number — still passes via `forbidden_specs`.

---

## BUG-007 — Product codes split as if they were quantities   [FIXED]

**Found:** 2026-08-14, reading the first real Unilog delivery export. The
abrasive series `775L` had been rewritten as `775 L`.

**Symptom:** `ATTRIBUTE_VALUE` for a 3M sanding disc read `775 L` where the input
said `775L`. Silent: the value looks plausible, and a buyer searching the series
simply would not find it.

**Root cause:** `normalize.normalize_value` matched `^digits + letters$` and
treated any trailing letters as a unit. Right for `24VDC` and `25mm`, wrong for
anything shaped like a code — `775L`, `6205C3`, `P120`. The pattern had no notion
of whether the suffix was a unit it actually knew.

Worth noting how it surfaced: the enrichment was correct and the *normalizer*
corrupted it afterwards. Every validation instrument we have points at the model,
so nothing was watching this direction — the same blind spot as BUG-005.

**Fix:** split only when the trailing token resolves in `UNIT_ALIASES`. An
unrecognised suffix is far more likely to be part of a product code than a unit we
forgot to list, so the safe default is to leave the value untouched.

**Verified:** `test_unit_split_only_fires_on_known_units` — `775L`, `6205C3` and
`P120` survive intact while `24VDC` -> `24 V DC` and `25mm` -> `25 mm`.

---

## BUG-008 — BUG-004's prompt fix was lost in a merge   [FIXED]

**Found:** 2026-08-14, while renumbering docs after merging a teammate's work.
`grep "identifier, not a specification" src/enrich.py` returned nothing.

**Symptom:** `ENRICH_SYSTEM`'s confidence ladder had reverted to the original
wording — *"0.7-0.9 strongly implied by the input (model number decoding,
explicit series)"* — the exact sentence identified as BUG-004's root cause. The
fix had been verified (hallucinations 2 -> 0 on `MISL-6205-2RS-C3-77`, grounding
held at 14/14 on controls) and then silently reverted when the prompt section was
rewritten for other reasons.

Nothing failed. All 32 tests stayed green, because the fix's evidence lived in a
golden-set score, not in an assertion.

**Root cause:** a prompt is code, but it is the one kind of code with no unit
test asserting its content. A rewrite of an adjacent block took the fix with it
and no instrument noticed.

**Fix:** restored the paragraph. BUG-004's mitigation is now two independent
layers, which is the right shape: `checks.identifier_decoded_specs` catches a
decoded spec *after* generation, and the prompt discourages generating it. The
teammate's re-marking of BUG-004 as "OPEN — mitigated" was correct given the
code as it stood.

**Verified:** `test_enrich_prompt_forbids_identifier_decoding` asserts the
paragraph is present, so the next merge that drops it fails a test instead of a
golden run nobody re-ran.

---

## BUG-006 — Adding `?page=` killed every catalog request   [FIXED]

**Found:** 2026-08-12, first end-to-end render after adding pagination. The full
test suite was green at the time.

**Symptom:** `GET /` raised `TypeError: 'int' object is not callable` from inside
the f-string that builds the page.

**Root cause:** FastAPI takes a route's query parameters from its signature, so
the pager needed `def catalog(page: int = 1)`. `page` was already the name of the
module-level HTML renderer — `def page(title, body)` — and the parameter shadowed
it inside the one function that called it most.

**Fix:** renamed the renderer to `render_page`. Renaming the *parameter* was the
alternative and was rejected: it would have changed the public URL to something
other than `?page=`, which is the obvious spelling for the query string.

**Verified:** `test_ui_routes_render_against_a_real_database` calls the routes
against a temp database. Worth stating plainly, because it is the lesson:
**every existing test passed while the main page was dead.** The helpers were
unit-tested, the route was not, and a route can only be tested by calling it.
The first fix attempt missed the `page(sku, ...)` call site — the new test caught
that too, one minute later.

---

## BUG-005 — Non-Latin attribute names deleted before the model saw them   [FIXED]

**Found:** 2026-08-12, while checking whether canonicalising *output* spec names
would break any golden expectation. The check was defensive; the bug it turned up
was on the input side and had been live since the first commit.

**Symptom:** `normalize_record` on our own Chinese golden record:

```
raw attributes : {'型号': 'MULT-CN-PT100-2', '货号': 'MULT-CN-IT-7731',
                  'Categoria': 'Sensori di temperatura', '包装': '1 pz'}
after normalize: {'categoria': 'Sensori di temperatura'}
DROPPED        : 3 of 4
```

French fared better but not well: `débit` → `d bit`, `durée de vie` →
`dur e de vie`, `vida útil` → `vida til`.

**Root cause:** `normalize_key` cleaned with `re.sub(r"[^a-z0-9 ]+", " ", ...)`.
That class keeps ASCII letters and deletes every other script, so a CJK name
became the empty string — and `normalize_record` then dropped it, correctly, as
a blank key. Two reasonable-looking lines, each doing its job, combining into
silent data loss.

The damage was worst where it was least visible: `MULT-CN-IT-7731` is a *golden
record*. We were scoring the model's accuracy on a record whose input we had
quietly deleted three quarters of, and counting the resulting gaps against it.

**Fix:** `re.sub(r"[_\W]+", " ", ...)` with `re.UNICODE`. `\W` is unicode-aware,
so non-Latin letters survive; `_` is listed separately because it is a word
character but reads as a separator in supplier headers (`part_no` → `sku`).

**Verified:** `test_normalize_key_survives_non_latin_scripts` — asserts 型号,
débit, and `Réf. fournisseur` survive, that `part_no`/`Part No.` still resolve to
`sku`, and that a three-attribute Chinese record keeps all three. The existing
`test_normalize_keys` still passes unchanged, which is the point: the fix is
strictly additive to the behaviour we already relied on.

**Not yet known:** how much this moved the accuracy numbers. `MULT-CN-IT-7731`
and `MULT-FR-ES-0442` were both scored under the broken canonicaliser, so their
cached results measured a handicapped input. They need re-scoring, and the
hallucination count may move in either direction.

---

## BUG-004 — Part numbers get decoded from memory   [FIXED — two layers]

**Found:** 2026-08-12, first run of the 32-record golden set. Invisible to the
previous 8-record set, which scored 0 hallucinations.

**Symptom:** 5 of 6 hallucinations are the same behaviour — reading a SKU,
recognising the numbering scheme, and stating what that scheme *usually* means as
though the record had said it.

| Record | Invented | Decoded from |
|---|---|---|
| `MISL-1756-IF16-XT` | brand `Allen-Bradley` | SKU resembling the ControlLogix scheme |
| `MISL-6205-2RS-C3-77` | `width`, `internal clearance`, +2 specs | `6205-2RS-C3` bearing designation |
| `MISL-VLV-12-150-316` | `material` | digits reading as `1/2 in, Class 150, 316 SS` |
| `DATA-FLUFF-0001` | 2 specs | marketing copy naming attributes without values |

All 3/3 misleading-identifier records failed. Every other trap category passed.

**Scope:** `ENRICH_SYSTEM` in `src/enrich.py`. Not a code defect — the pipeline
behaved correctly and the provenance model recorded these as `inference`. The
prompt simply does not forbid the specific move of treating a recognised part
number as a source of fact.

**Root cause (probable):** the prompt tells the model to ground values in the
input and to prefer null, but explicitly *permits* inference from "model number
decoding, explicit series" at 0.7–0.9 confidence — which is precisely this
behaviour, described approvingly. The instruction and the trap are in conflict,
and the instruction wins.

**Confound, unresolved:** all four failing records were scored on
`gemini-3.5-flash` after a quota-forced model rotation, so "these traps are hard"
and "this model abstains less" cannot yet be separated. Re-scoring the three
`MISL-*` records on another model costs 6 API calls and settles it. Attempted
2026-08-12; every model's daily quota was already spent.

**Proposed fix (not yet applied — would ship unverified while quota is out):**
tighten the confidence ladder so that decoding an identifier is named as the
thing not to do:

> A part number that *resembles* a known series is not a statement of that
> series' properties. You may name the likely manufacturer or family as an
> inference, but never emit dimensions, materials, ratings, or tolerances that
> the record itself does not state — however standard they are for that
> designation.

**Verify by:** re-running `python -m src.golden --only MISL-1756-IF16-XT,
MISL-6205-2RS-C3-77,MISL-VLV-12-150-316,DATA-FLUFF-0001 --force` and checking
hallucinations drop to 0 **without** grounding falling — the risk is that a
blunter instruction also suppresses legitimate inference.

**Mitigation applied 2026-08-12 (deterministic, not a prompt change):**
`checks.identifier_decoded_specs` catches the failure downstream of the model
instead of trying to prevent it. A **spec** is flagged when three things hold at
once: its source is `inference`, its value does not appear in the record, and its
own evidence says it came from the identifier ("designation", "series", "part
number", "conventionally", ...). Confidence drops to 0.4 — just under
`CONFIDENCE_FLOOR` — so the value stops being published but stays visible with
the reason attached.

Why this and not the drafted prompt edit: the rule can be verified offline, right
now, with no quota, and it cannot silently regress the way a prompt can. It also
respects the same boundary the drafted wording draws — brand and family are left
alone, because naming the likely manufacturer from a series prefix is legitimate
inference and only the *properties* are off-limits.

**Still open, and why the status is not FIXED:**
- The model still produces these values; we now refuse to publish them. Suppressed
  at the output is not the same as not generated.
- The rule requires the evidence to admit where the value came from. A spec
  inferred silently escapes it.
- Effect on the golden numbers is **unmeasured**. It should lower hallucinations;
  it may also lower grounding if it catches legitimate inference, and the golden
  set is the only instrument that can tell those apart. Do not quote a new
  accuracy figure until it has been re-run.
- The model-vs-difficulty confound from the original run is untouched by this.

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
