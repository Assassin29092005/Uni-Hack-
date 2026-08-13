# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-14 03:05 IST

---

## Where things stand

**The project now speaks Unilog's actual format.** Everything before 2026-08-14
was built against a schema we invented; the organisers' real files arrived and
the pipeline could not even read their input.

- Input: their 6-column sheet ingests, all 1000 rows.
- Output: `src/delivery.py` writes their **252 columns byte-exactly**, plus a
  provenance sidecar. Guarded by a test against their file.
- **17 real Unilog records enriched and exported** — `build/delivery.csv`,
  `data/demo_catalog.json` (seedable with no API key).
- **Golden set re-validated on the current prompt: 0 hallucinations,
  201/201 abstention, 135/140 grounding (96%).**
- 42 tests pass.

Deadline: **23 August 2026, 11:59 PM IST.**

## Submission checklist (from the organisers' email)

| Item | State |
|---|---|
| GitHub repo link | ready |
| Prototype deck (PDF, mandatory template from dashboard, <=5 MB) | **not started** |
| Solution brief (text) | draft from README intro |
| Live prototype URL | **not deployed** — `src/app.py` runs locally only |
| Demo video | **not recorded** |

The three "not" rows are the submission risk now, not the code.

## Next up

1. **Deploy the UI** for the Live Prototype Link. It is a read-only FastAPI app
   over a SQLite file, so a seeded `catalog.db` plus any host works — no key or
   quota needed at runtime. That property was designed in; use it.
2. **Enrich a wider slice** of the 1000-row input and export a delivery CSV to
   show alongside the deck. Quota is ~20 requests/day/model, 2 calls/record —
   rotate models (`--models a,b,c`) and start early.
3. **Deck + video.** Lead with the refusal surface and the provenance sidecar;
   that is the differentiator, and the brief explicitly calls a confidence /
   needs-review flag "a genuinely valuable feature".
4. Re-run the golden set — accuracy figures predate the delivery-format prompt
   rewrite, so they describe a prompt that no longer exists.
5. If the missing reference files arrive, wire the manufacturer/brand list and
   LOV first: they convert "grounded" into "conformant" (README → Missing
   reference data).

## Broken / known issues

- **7 of 11 reference files were not in the pack.** No labelled ground truth, no
  approved manufacturer/brand list, no LOV, no UOM standard. We can show
  grounding; we cannot yet show conformance. Full table in README.
- **MOBILE_DESC compliance: 1/17 rows.** The 60-char minimum is the only
  format rule the demo catalog fails. Cause found and fixed — the prompt asked
  the model to *count characters*, which LLMs are unreliable at; it now specifies
  the components (Manufacturer, Brand, Item Type, Series, MPN) so the length
  follows from the content. Spot-checked 2/3 compliant after the change. **The
  17 stored records predate the fix and need re-enriching when quota resets** —
  `python -m src.pipeline data/unilog_demo.csv --force`.
- Free-tier quota for 2026-08-14 is **spent** across flash and most lite models,
  used by the golden re-validation and the demo enrichment. Resets ~12:30 IST.
- BUG-008: BUG-004's prompt fix was silently lost in a merge and restored. Now
  asserted by a test — a prompt is code, and it was the only code with no test.
- `MANUFACTURER_NAME` mirrors `BRAND_NAME` (no approved list shipped).
- ~170 of 252 columns stay blank. Correct — nothing in a 6-column input grounds
  UPC, UNSPSC, dimensions or asset filenames — but a judge will notice, so say
  it before they ask.
- Free-tier quota ~20 requests/day/model; 2 calls per record.

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

Three of the four earlier questions are now answered by the organisers' files:
the dataset is theirs (1000-row input + 252-column output sheet), the deadline is
23 Aug 2026 11:59 PM IST, and submission is deck + brief + live URL + repo +
video. What remains:

- **Can the other 7 reference files be obtained?** They are named in the Solution
  Guide but were not in the pack. The manufacturer/brand list and the LOV are the
  two that would most change the output.
- Team size — is anyone else committing to this repo? (One teammate's work is
  already merged.)
- Where will the live prototype be hosted?
