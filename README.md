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

No key to hand? An already-enriched catalog can be loaded with zero API calls:

```bash
python -m src.pipeline --seed data/demo_catalog.json
```

`--export` writes that file from a real run. The UI is read-only and makes no
model calls, so a seeded catalog demos identically to a live one — which is the
point, because a rate limit must never be able to break a demo.

## How it works

```
ingest → normalize → enrich → normalize → validate → score → persist
         (no LLM)    (LLM)     (no LLM)    (LLM + rules)  (no LLM)
```

Four decisions that shape everything else:

**Deterministic work never touches the model.** Unit canonicalisation, casing,
and attribute-name aliasing (`MPN`/`Part No`/`mpn` → `sku`) are lookup tables.
Sending `"MM" → "mm"` through an LLM costs latency and adds a hallucination
surface for zero gain. It also makes the AI's contribution measurable: whatever
appears beyond `normalize.py`'s output is what the model actually added.

Normalization runs **twice**, and the second pass is the one people forget: the
model names its own output attributes, so the same quantity comes back as
"Operating Voltage" on one record and "voltage" on the next. Canonicalising only
the input means deduped attributes hold for data you didn't generate. Exact
duplicates merge; two specs that disagree about one attribute are deliberately
*kept*, because that is a contradiction and resolving it by deleting one side is
the behaviour this project exists to argue against.

**Validation is a separate call, not a self-check.** One call that both writes
and grades its own work produces a rubber stamp — it has already committed to
the answer. A second call with fresh context, shown only the claim and its
evidence, actually finds contradictions. Verified: see Results.

**Validation has a free half.** `src/checks.py` applies deterministic rules
alongside the model: a field claiming `source: input` for a value not in the
input, a spec decoded from the part number, a numeric quantity carrying a unit
from the wrong family, two specs contradicting each other. They cost nothing,
behave identically every run, and — the part that matters on a free tier — still
run when the validation API call fails, so a record whose audit died comes out
degraded rather than unexamined. They are a floor, not a replacement: an 85 g
relay reported as 45 kg is implausible only if you know what a relay is.

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

## Delivery format

The organisers ship a 6-column input and require a **252-column** output sheet,
with the instruction *"Please do not change or modify the headers."*

```
INPUT  (6 cols)                          OUTPUT (252 cols)
Mfg_Part_Num                             PART_NUMBER, Dept/Class/Fine, Classpath,
Part_Desc          ── enrich ──▶         MANUFACTURER_NAME, BRAND_NAME,
E1_Brand    "-- Unbranded --"            5 descriptions at 5 lengths,
Unilog_Brand"-- No Unilog Brand --"      ATTRIBUTE_LABEL/VALUE/UOM x 50,
DIB_Brand   "-- No DIB Brand --"         UPC/EAN/GTIN, dimensions, assets...
Part_Manuf  (the dealer, not the maker)
```

```bash
python -m src.pipeline data/unilog_demo.csv        # 12 real rows, committed
python -m src.delivery build/delivery.csv
```

`data/unilog_demo.csv` is a 12-row extract of the organisers' 1000-row sheet, so
the pipeline is runnable straight from a clone. The full input and the Expected
Output sheet are **not committed** — they are Unilog's material, and the
accompanying briefing PDF carries a participant's personal email address. Drop
them in the repo root and the same commands take the full file:

```bash
python -m src.pipeline "Unihack_ Sample Dataset - Input.csv"
```

`data/delivery_headers.json` **is** committed — it is the 252-column contract
extracted from the Expected Output sheet, and `src/delivery.py` cannot run
without it. When the sheet is present, a test asserts the two still match.

Every run writes two files. `delivery.csv` is their 252 columns byte-exactly —
a test asserts our header list still equals their sheet, so drift fails loudly
rather than at judging time. `delivery.provenance.csv` carries the source,
evidence, confidence and a `needs_review` flag for every value **including the
refusals**, because their schema has nowhere to record why a cell says what it
says and we were told not to add columns (DECISIONS 022).

Three input traps the pipeline handles deterministically, before any model call:

| Trap | Why it bites | Handling |
|---|---|---|
| `-- Unbranded --` and friends | Left in, the model describes a product made by a company called Unbranded | `is_placeholder` strips ~20 sentinel forms |
| `Part_Manuf` is a **dealer** | The sample pairs `Part_Manuf` "Appliance Dealers Cooperative" with `MANUFACTURER_NAME` "Rheem Manufacturing" — aliasing it to brand feeds the model a confident falsehood as if it were input | maps to `supplier`, never `brand` |
| Codes that look like quantities | `775L` was being rewritten to `775 L` (BUG-007) | unit-splitting only fires on units we actually know |

**Live result on real Unilog rows — 5/5 enriched, 5/5 format-compliant.** From
the single input string `3M 775L Stikit Film P120 - Cubitron II 50 Disc/Box`:

| Column | Generated |
|---|---|
| `Classpath` | Industrial Supplies > Abrasives > Sanding Discs |
| `BRAND_NAME` | 3M |
| `INVOICE_DESC` | `3M 775L STIKIT FILM P120 DISC 50BX` (34 chars, ALL CAPS) |
| `MOBILE_DESC` | 70 chars, within the 60–80 rule |
| `RETAIL_DESC` | no brand, no MPN, as the rule requires |
| attributes | 6 triplets: Series, Grit Rating, Grain Type, Attachment Type, Backing Material, Quantity per Box |

Character limits, casing and Classpath depth are checked deterministically by
`checks.delivery_checks` and reported on every export — that is the
"character-limit compliance" metric the brief asks submissions to show. It is
kept off the confidence path on purpose: a formatting rule must not downgrade a
well-grounded value, or the accuracy numbers stop being comparable
(DECISIONS 024).

## Missing reference data

The Solution Guide describes **eleven** files. Four were in the pack we
received. The seven below were not, and each one bounds what the pipeline can
currently claim:

| Missing file | What it would enable |
|---|---|
| `Unilog-Sample_200_Items-Input-vs-Output.xlsx` | The labelled ground truth — the only place field-level accuracy can actually be scored |
| `UniCat_Manufacturer_and_Brand_List.xlsx` | 27k approved manufacturer/brand pairs with exact legal casing and ®/™. Until then `MANUFACTURER_NAME` mirrors `BRAND_NAME` |
| `Unicat_Lov_v1_0_Updated_With_Remarks.xlsx` | ~161k rows constraining attribute values per classpath. Our attributes are currently free text, not LOV-constrained |
| `Unilog_Master_UOM_Standards…xlsx` | ~500 approved unit abbreviations. We use a hand-built table of ~40 |
| `UNILOG_INTERNAL_CONTENT_GUIDELINES.docx` | The exact formulas and character limits per field. Ours are reconstructed from the guide's worked example |
| `Decimal_Fraction.xlsx` | 63 inch conversions (0.5 → 1/2, 50.25 in → 50-1/4 in) |
| `FAUCETS_LOV.xlsx`, `Fittings_LOV.xlsx` | The two categories specified end-to-end, which the guide recommends as the demo scope |

Consequence, stated plainly: we can show the pipeline is **grounded** (0
hallucinations across 32 adversarial records) but not yet that it is
**conformant** — matching approved vocabularies is unmeasurable without the
vocabularies. The architecture has the seams for it: `normalize.py` is already a
lookup layer, and `checks.py` already produces per-field findings.

## Results

All numbers below are from live runs on Google's **free** tier. Model is named
per run because it matters — the accuracy figures come from
`gemini-2.5-flash-lite`, the *weaker* of the two models used, so treat them as a
floor rather than a ceiling.

> **Status of these numbers, stated plainly:** they were true when measured and
> are now **stale**, for three reasons. The score cache they were aggregated from
> is a local file that no longer exists; BUG-005 (since fixed) means the two
> non-English records were scored on inputs the normalizer had silently deleted
> most of; and the deterministic rules described above change what gets
> published, so both hallucination and grounding will move. The set needs
> re-running. A project whose entire argument is that unverified numbers are
> worth less than admitted gaps does not get to make an exception for its own
> scoreboard.

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
bad record is recorded in the audit trail and never enters the catalog), rate
limits are **survivable** (retries honour the provider's own stated delay — see
BUG-002 for why that detail is not optional), and the read paths are **paged**
(the catalog table and the JSON API were unbounded `SELECT`s that materialised
the entire catalog per request — fine at 5 records, not at 10k).

Ingest is currently **CSV only**, against a brief that scopes CSV/JSON/PDF/URL.
The gap is visible in the schema rather than hidden: `source` allows `document`
and `web`, and no code path can produce either yet.

## Verification

Three layers, in increasing cost:

```bash
python test_pipeline.py       # offline, free, ~1s
python -m src.probe           # adversarial validator check, ~6 API calls
python -m src.golden          # accuracy vs hand-checked expectations, ~16 API calls
```

- **`test_pipeline.py`** — 32 checks over the deterministic logic where a
  regression produces *wrong data* rather than an error: unit normalization,
  confidence gating, scoring, store idempotency, per-provider schema dialects,
  HTML escaping, and every rule in `checks.py` **with a control case**. One test
  asserts the rules stay silent on the exact fixture the LLM validator is
  required to call clean — if the two instruments disagree, one is wrong and the
  suite says so. Another calls the web routes against a real database, after a
  query parameter shadowed a renderer and killed the catalog page while all 19
  tests of the day stayed green (BUG-006).
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
| `src/normalize.py` | Deterministic cleanup both directions, zero model calls |
| `src/llm.py` | Provider seam — the only file importing a vendor SDK |
| `src/enrich.py` | Prompts, enrich pass, validate pass |
| `src/checks.py` | Deterministic validation rules — the free half of the audit |
| `src/store.py` | SQLite + append-only audit trail + export/seed |
| `src/pipeline.py` | Ingest, orchestration, CLI |
| `src/app.py` | Web UI + JSON API |
| `src/probe.py` | Adversarial validator check |
| `src/golden.py` | Accuracy harness |

Project docs: `HANDOVER.md` (current state), `DECISIONS.md` (why, incl. rejected
alternatives), `FLOW.md` (execution path), `BUG.md` / `FEATURE.md` (traces).
