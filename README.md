# Product Intelligence for Industrial Commerce

Turns sparse, messy industrial product records into structured, validated,
commerce-ready data — and shows its work for every single field.

```
  INPUT                          OUTPUT
  MPN: HDX-4025-200              name        Bosch Rexroth HDX-4025-200 Hydraulic Cylinder
  Mfr: Bosch Rexroth             category    Hydraulic Cylinder
  Dims: 25 MM x 200MM            spec:bore   25 mm      [input, 1.00]  "Bore 25mm."
  WT: 3.4 KG                     spec:stroke 200 mm     [inference, 0.80]
  Notes: Hydraulic cylinder                              "Bore is stated as 25mm, so the
  assembly. Bore 25mm. Max                                second dimension conventionally
  operating pressure 210 bar.                             represents the stroke."
```

## The one rule

**No field ships without provenance.** Every enriched value carries where it came
from, the evidence for it, and a confidence score:

```python
{"value": "200", "unit": "mm", "source": "inference",
 "evidence": "Bore is explicitly 25mm, so the second dimension...",
 "confidence": 0.80}
```

If the input doesn't support a value, the system returns `null` **with a reason**
instead of guessing.

That constraint is enforced structurally, not by convention: `Product.name` is a
`Sourced` object, never a `str`, so no code path can emit a bare value. Ask it
about a SKU with no data and it tells you it doesn't know:

```
XYZZY-99999  —  complete 0%, confidence 0.00
        name: — (no grounded value)
              [confidence 0.00] No product name is provided in the input record.
       brand: — (no grounded value)
              [confidence 0.00] No brand information is provided in the input record.
```

A confident invented torque rating is worse than an admitted gap — a buyer can
work with a gap, and may order against a lie.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add a free Gemini key: aistudio.google.com/apikey
python -m src.pipeline data/sample_products.csv
python -m src.app             # http://127.0.0.1:8000
```

Runs on a **free** API tier. No credit card, no paid account.

## How it works

```
ingest → normalize → enrich → validate → score → persist
         (no LLM)    (LLM)     (LLM)     (no LLM)
```

Four decisions that shape everything else:

**Deterministic work never touches the model.** Unit canonicalisation, casing,
and attribute-name aliasing (`MPN`/`Part No`/`mpn` → `sku`) are lookup tables.
Sending `"MM" → "mm"` through an LLM costs latency and adds a hallucination
surface for zero gain. It also makes the AI's contribution measurable: whatever
appears beyond `normalize.py`'s output is what the model actually added.

**Validation is a separate call, not a self-check.** One call that both writes
and grades its own work produces a rubber stamp — it has already committed to
the answer. A second call with fresh context, shown only the claim and its
evidence, actually finds contradictions. Verified: see Results.

**Scores are computed in Python, never requested from the model.**
`completeness` and `mean_confidence` are `@property`, so they're absent from the
JSON schema the model sees. A model asked to grade itself grades generously.

**Validation can only lower confidence, never raise it.** Otherwise a second
opinion could launder a bad first one.

## Provider-agnostic

One env var switches the model. Prompts and schemas are identical across all
three — possible only because output is schema-constrained rather than parsed
from prose.

| `LLM_PROVIDER` | Cost | Needs |
|---|---|---|
| `gemini` *(default)* | free tier | key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `ollama` | free | local install, no account at all |
| `anthropic` | paid | API key |

## Results

All numbers below are from live runs on Google's **free** tier. Model is named
per run because it matters — the accuracy figures come from
`gemini-2.5-flash-lite`, the *weaker* of the two models used, so treat them as a
floor rather than a ceiling.

### Accuracy — `python -m src.golden`, 28 of 32 hand-checked records

**32 records, 24 of them deliberate traps**, covering non-English catalogue text,
self-contradictory rows, unfamiliar categories (mining wear parts, lab glassware,
HVAC dampers), imperial/fractional/range units, scraped HTML noise, misleading
part numbers, near-empty rows, and specs the supplier explicitly defers.

| Metric | Result |
|---|---|
| Grounding — filled what it should | 127/131 (97%) |
| Abstention — refused what it should | 163/169 (96%) |
| Values — matched known answers | 17/17 (100%) |
| **Hallucinations** | **6** |

**Widening the set from 8 records to 32 moved the hallucination count from 0 to
6.** The original 8 weren't measuring the hard cases. That is the finding, and it
is worth more than the clean scoreboard it replaced.

The failures are not scattered — **5 of 6 are the same behaviour: decoding a part
number from memory.**

| Record | What it invented | From |
|---|---|---|
| `MISL-1756-IF16-XT` | brand `Allen-Bradley` | SKU resembling the ControlLogix scheme |
| `MISL-6205-2RS-C3-77` | `width`, `internal clearance`, +2 more specs | `6205-2RS-C3` bearing designation |
| `MISL-VLV-12-150-316` | `material` | digits that look like `1/2 in, Class 150, 316 SS` |
| `DATA-FLUFF-0001` | 2 specs | marketing copy that names attributes without values |

Every one is real product knowledge correctly recalled — and wrong to state,
because *this record* never said it. All three misleading-identifier records
failed; every other trap category passed.

**Caveat we cannot yet resolve:** all four failing records were scored on
`gemini-3.5-flash`, because free-tier quota forced a mid-run model rotation. So
"these traps are hard" and "this model abstains less" are confounded. Re-scoring
those three records on another model costs 6 API calls and settles it — the next
thing to do when quota resets.

The 4 misses are all over-caution (a stated spec left unextracted), which is the
direction we would rather be wrong in.

### Validation — `python -m src.probe`, gemini-2.5-flash

| Planted fault | Caught | What the validator said |
|---|---|---|
| Coil voltage contradicted (24 V → 240 V) | yes | *"contradicts the raw input '24 V DC'"* |
| Circular evidence ("X because it is X") | yes | *"directly contradicts the raw input 'Omron'"* |
| Implausible mass (85 g → 45 kg) | yes | *"physically implausible for a miniature power relay"* |
| Wrong unit (current rated in mm) | yes | *"listed as 'mm', but the raw input indicates 'A'"* |
| Overconfident guess at 0.99 | yes | *"the evidence explicitly states that the category is a guess"* |
| **Control — a clean record** | **silent** | 0 issues, verdict `pass` |

The control matters more than the five catches: a validator that flags
everything would score 5/5 and be worthless.

It also caught something we didn't plant. The first control fixture used
`"Stated in the input record."` as evidence for a name that was actually
*derived* and a description that was *synthesized* — and the validator flagged
both as unsupported, at 0.7/0.6 rather than the 0.0 it gave real contradictions.
It was right, our fixture was sloppy, and the proportionality was correct.

### Scale — what we measured and what breaks

Measured: **4.2s per record**, two API calls each, on the free tier.

The binding constraint is not our code. The free tier allows **20 requests per
day per model** on this key (measured from the API's own 429: `limit: 20, model:
gemini-2.5-flash`) — about **10 records per day per model**, since each record
costs two calls. Quotas are per-model, so switching model gives a fresh bucket;
the accuracy run above happened only because `gemini-2.5-flash-lite` still had
headroom after `gemini-2.5-flash` was spent.

For a real 100k-row catalog, in the order these would actually bite:

1. **Quota / rate limits.** 200k calls. The fix is the Batch API — roughly half
   price, 100k requests per job — deliberately not used here because batch
   results can take an hour and that is useless in a live demo (DECISIONS 008).
2. **Cost.** Halved again by the Sonnet/Opus-style tiering we deferred until we
   can measure which records are actually hard (DECISIONS 007).
3. **SQLite write contention.** Single-writer. WAL mode first, Postgres if that
   isn't enough. The persistence layer is thin on purpose so the swap is small.
4. **Resume granularity.** `is_done` skips whole records, not individual
   ungrounded fields, so a partially-enriched record re-runs entirely.

What already works at scale: the run is **resumable** (kill it, restart, it skips
finished SKUs and makes zero API calls for them), failures are **isolated** (one
bad record is recorded in the audit trail and never enters the catalog), and
rate limits are **survivable** (retries honour the provider's own stated delay —
see BUG-002 for why that detail is not optional).

## Verification

Three layers, in increasing cost:

```bash
python test_pipeline.py       # offline, free, ~1s
python -m src.probe           # adversarial validator check, ~6 API calls
python -m src.golden          # accuracy vs hand-checked expectations, ~16 API calls
```

- **`test_pipeline.py`** — the deterministic logic where a regression produces
  *wrong data* rather than an error: unit normalization, confidence gating,
  scoring, store idempotency, per-provider schema dialects, HTML escaping.
- **`src/probe.py`** — plants known errors (a contradicted voltage, circular
  evidence, an implausible mass, a wrong unit, an overconfident guess) and checks
  the validator catches each. Includes a **control**: a clean record that must
  produce zero issues. Without the control, a validator that flags everything
  would score perfectly and be worthless.
- **`src/golden.py`** — scores enrichment against `data/golden.json` (32 records).
  Weighted toward abstention traps: cases where a well-known spec is
  *deliberately absent*, so the model plausibly "knows" it and must still refuse.
  Two distinct traps are tracked separately — `forbidden_specs` (never mentioned)
  and `deferred_specs` (named but explicitly unvalued: *"Torque: see datasheet"*).
  Headline metric is hallucination count.

  Every expectation must be verifiable from that record's own input alone —
  enforced by the test suite, which fails if an expected value isn't literally
  present in the record. That is what keeps the set ground truth rather than one
  model's opinion of another's output.

  The set needs 64 API calls against a ~20/day/model free-tier cap, so scores
  **accumulate** in `golden_scores.json` across runs, `--models a,b,c` rotates
  as each quota bucket empties, and `--report` aggregates without spending any
  calls. Each score records which model produced it.

## Layout

| Path | Role |
|---|---|
| `src/models.py` | Schema. `Sourced` makes provenance structural |
| `src/normalize.py` | Deterministic cleanup, zero model calls |
| `src/llm.py` | Provider seam — the only file importing a vendor SDK |
| `src/enrich.py` | Prompts, enrich pass, validate pass |
| `src/store.py` | SQLite + append-only audit trail |
| `src/pipeline.py` | Ingest, orchestration, CLI |
| `src/app.py` | Web UI + JSON API |
| `src/probe.py` | Adversarial validator check |
| `src/golden.py` | Accuracy harness |

Project docs: `HANDOVER.md` (current state), `DECISIONS.md` (why, incl. rejected
alternatives), `FLOW.md` (execution path), `BUG.md` / `FEATURE.md` (traces).
