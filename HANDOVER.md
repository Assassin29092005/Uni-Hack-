# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-14 (steps 2 & 5, normalization, scorer)

---

## Where things stand

**Two real Delivery Format rows arrived, and the first comparison against them
found four exporter defects that 42 passing tests had missed.** All four are
fixed. The lesson is worth keeping: every test we had asserted our own
behaviour, so a self-consistent wrong format stayed green indefinitely.

- Input: their 6-column sheet ingests, all 1000 rows.
- Output: `src/delivery.py` writes their **252 columns byte-exactly**, plus a
  provenance sidecar that now covers **every** generated column (was 5 of 9).
- **17 real Unilog records enriched**, seedable with no API key
  (`data/demo_catalog.json`, now carrying the input row per record).
- **`src/truth.py` — field-level accuracy against their own sheet. Scored: 7%
  exact agreement** (3 match / 12 differs / 26 missing / 3 extra, 2 records).
  Read that number correctly before reacting to it: it measures **coverage, not
  correctness**. Their ground-truth rows were built from the manufacturer's own
  site — the URL is sitting in their own `MFR URL` column — and carry brand,
  series, voltage, amperage, mounting, dimensions and sound level that appear
  **nowhere** in the 6-column input. We abstained on every one of them, which is
  the pipeline working as designed. ~24 of the 41 non-matches are the missing
  manufacturer-source step, not a reasoning failure. See "Next up" item 1.
- Golden set: 0 hallucinations, 201/201 abstention, 135/140 grounding (96%).
- **62 tests pass** (was 42 at the start of the day).

Deadline: **23 August 2026, 11:59 PM IST.**

## Submission checklist (from the organisers' email)

| Item | State |
|---|---|
| GitHub repo link | ready |
| Prototype deck (PDF, mandatory template from dashboard, <=5 MB) | **not started** |
| Solution brief (text) | draft from README intro |
| Live prototype URL | **not deployed** — `src/app.py` runs locally only |
| Demo video | **not recorded** |

The three "not" rows are still the submission risk, not the code.

## Next up

1. **Feed manufacturer documents in — the sourcing machinery is built, it is
   starved.** `src/sources.py` handles policy, robots.txt, caching, local files
   and the `<document>` prompt block; `--sources` turns on web fetching. What it
   lacks is content: both sites their ground-truth rows cite refuse automated
   access, and modern manufacturer sites are JS-rendered (diablotools.com
   fetched clean and yielded 12 characters). **The unblocking action is a human
   one:** download a spec sheet in a browser, save it as
   `data/documents/<SKU>.html`, re-run with `--force`. Then `python -m src.truth`
   measures what a document is worth. Do this for PDSH4816AF first — it is one
   of the two scorable records.
   Background, still true: ~24 of 41 non-matching fields trace to this step.
   Their rows name their own sources (`MFR URL`, `Ref URL 1-5`) — row 1 cites
   `frigidaire.com/.../PDSH4816AF`, row 2 three Whirlpool documents including
   the owner's manual PDF.
2. **Deploy the UI** for the Live Prototype Link. Read-only FastAPI over a
   SQLite file, so a seeded `catalog.db` plus any host works — no key or quota
   at runtime. That property was designed in; use it.
3. **Deck + video.** Lead with the refusal surface, the provenance sidecar, and
   the ground-truth scorer — the brief explicitly calls a confidence /
   needs-review flag "a genuinely valuable feature", and asks submissions to
   show their evaluation. The 7% is a *strength* if framed honestly: every gap
   is an abstention with a written reason, not a wrong answer. Show the refusal.
4. **Re-enrich the 17 demo records:** they predate the MOBILE_DESC prompt fix
   and 16 of 17 still fail the 60-char minimum.
   `python -m src.pipeline data/unilog_demo.csv --force`
5. **PDF parsing** is the highest-value remaining piece of step 5 — their rows
   cite manufacturer PDFs and that is where real specs live. No parser is wired
   in; `sources.fetch` names the refusal explicitly rather than returning empty
   text.
6. If the remaining reference files arrive, wire the manufacturer/brand list and
   the LOV first: they convert "grounded" into "conformant", fix the Classpath
   leaf ("Dishwashers" vs their "Built-In Dishwashers", the other 2 wrong
   values), and are the only thing that can fix attribute-slot ordering
   (BUG-010 D3).

## Broken / known issues

- **BUG-010 D3 is unfixed and unfixable without the LOV.** Their sheet emits a
  category's full attribute sequence in fixed order, keeping the label with an
  empty value (`Model`, `Plug Type`, `Color` are present-and-blank on the
  dishwasher row). We pack only grounded specs, densely, in model order. Our
  values can be right and still not line up with their columns.
- **MOBILE_DESC compliance: 1/17 rows.** Cause found and fixed in the prompt
  (it asked the model to *count characters*); the 17 stored records predate the
  fix. Re-enrich when quota resets.
- **6 of 11 reference files still missing** — no approved manufacturer/brand
  list, no LOV, no UOM standard, no Decimal_Fraction table, and only 2 of 200
  ground-truth rows. Full table in README → Missing reference data.
- `MANUFACTURER_NAME` mirrors `BRAND_NAME` (no approved list). Their sheet
  proves these genuinely differ — `Rheem Manufacturing` vs `FRIGIDAIRE®`.
- **Manufacturer sourcing is built but starved.** `src/sources.py` works —
  policy, robots.txt, cache, local documents, `--sources`. It has nothing to
  read: the two sites their rows cite refuse automated access, manufacturer
  sites are JS-rendered, and PDFs (where the specs actually are) need a parser
  nobody has wired in. Coverage is whatever documents an operator supplies.
- **No digital assets.** Every image/SDS/spec-sheet column is blank, and
  nothing in the pipeline produces one. Guide step 8, untouched.
- **De-dup is exact-match only.** Near-duplicates ("1 Gang" vs "1G") go
  undetected; the threshold that would catch them needs the 200-row sheet to
  tune (DECISIONS 027).
- **Their sheet has an error we should report, not match.** Row 1 pairs
  `BRAND_NAME` `FRIGIDAIRE®` with `MANUFACTURER_NAME` `Rheem Manufacturing` —
  Rheem makes water heaters; Frigidaire is Electrolux. The guide predicted this
  ("at least one row where the manufacturer and brand look mismatched") and says
  noticing it is a strength. Do not tune the pipeline toward reproducing it.
- **Blank columns, measured — the old "~165" was invented and wrong.** Two
  different true numbers, and a deck should use the first:
  - **81 of 252 no code path can ever fill.** UPC/EAN/GTIN, UNSPSC, price,
    packaging, dimensions, warranty, country of origin, ITEM_FEATURES_1-20,
    every asset column, and the six URL columns. Counted, not estimated:
    reachable = 12 passthrough + 9 generated + 150 attribute slots = 171.
  - **217 of 252 are blank in the current 19-record export** (35 ever
    populated), because most records ground fewer than 50 attributes.
  Both are correct answers to different questions. Say which one you mean.
- Free-tier quota ~20 requests/day/model; 2 calls per record.
- `src/delivery.py` has no `--db` flag (use `CATALOG_DB=`); `pipeline` and
  `truth` both do.
- The local `catalog.db` holds 19 records (17 demo + the 2 ground-truth). On a
  fresh clone it does not exist at all — seed before demoing:
  `python -m src.pipeline --seed data/demo_catalog.json`
- **Anything that string-matches a canonical attribute name is a silent
  dependency of `normalize.ATTRIBUTE_ALIASES`.** Renaming one disarmed two
  instruments without failing a test (BUG-012). Grep `data/`, `golden.py`,
  `probe.py` and `checks.py` before touching that table.

## Avoid

- **Don't add a test that only asserts our own behaviour when their sheet could
  answer instead.** That is precisely how BUG-010 survived 42 green tests.
- Don't pool `truth.py`'s numbers with `golden.py`'s — they measure different
  things against differently-authored expectations (DECISIONS 025).
- Don't quote a field-level accuracy percentage from 2 ground-truth rows. The
  scorer exists to catch systematic format errors at this sample size.
- Don't derive Dept/Class/Fine from the Classpath. They are different
  taxonomies; this was BUG-010 D1.
- Don't add a delivery column to `to_row` without adding it to
  `GENERATED_COLUMNS` — provenance coverage is structural now, keep it that way.
- Don't add auth, multi-tenancy, microservices, or a plugin system.
- Don't hand-write `data/demo_catalog.json`. (Attaching `raw` by joining the
  committed input CSV is a key join, not authorship — DECISIONS 025.)
- Don't let `apply_report` raise confidence — deliberately one-way, test-guarded.
- Don't let `checks.merge` mutate the model's report; `probe.py` measures that
  report alone.
- Don't spend model calls on deterministic work; extend `normalize.py` or
  `checks.py` instead.
- Don't tune prompts without the golden set. You'll be guessing.
- Don't swap SQLite for Postgres until concurrent writes actually hurt.

## Open questions for the user

- **Can the remaining 6 reference files be obtained** — above all the full
  200-row Input-vs-Output sheet, the manufacturer/brand list, and the LOV? The
  2 rows we have paid for themselves within an hour; 200 would let us quote a
  real accuracy figure, and the LOV is the only path to attribute conformance.
- Team size — is anyone else committing to this repo?
- Where will the live prototype be hosted?
