# CLAUDE.md

## Session protocol — do this without being asked

**At the start of every session:** read `HANDOVER.md` first, then `FLOW.md`. Check
`DECISIONS.md` before proposing an architectural change — it may already be settled.

**At the end of every session, update:**

| File | How | Contains |
|---|---|---|
| `HANDOVER.md` | **rewrite** | Current state only: done / in progress / broken / next / avoid. Stale status is worse than none. |
| `DECISIONS.md` | **append** | Any meaningful choice made this session + why, incl. what was rejected. Never edit past entries; supersede them. |
| `FLOW.md` | update | Real execution path as it changes. Move entries PLANNED → ACTUAL only once the code exists. |
| `BUG.md` / `FEATURE.md` | append/update | One traced section per bug or feature, including failed attempts. |

Skip a file only when genuinely nothing changed for it, and say so. Not every session
produces a decision — don't manufacture entries to look busy.

## Comments

Comment non-obvious logic **while writing it**, not after. Explain the flow, never
restate the code:

- What this block is for and what invariant it protects.
- What calls into it, and what it assumes already exists.
- Why it's done this way, when an obvious simpler way was rejected.

`# increment counter` above `i += 1` is noise. `# runs before enrich() so the LLM never
sees mixed units — enrich() assumes normalized input` is the whole point.
Mark deliberate shortcuts with `# ponytail:` naming the ceiling and the upgrade path.

## Project

Hackathon build: **AI-Powered Product Intelligence for Industrial Commerce**.

Industrial companies hold product info scattered across websites, PDF catalogs, and
technical datasheets. We turn sparse/messy input (a SKU, a name, a spec blob, a PDF page)
into structured, validated, commerce-ready product records — with an explanation for
every field we generated.

**Judged on four outcomes:**

1. **Structured data generation** — rich product records from limited inputs.
2. **Accuracy & consistency** — normalized units, deduped attributes, no contradictions.
3. **AI validation & enrichment** — the AI checks its own output, not just produces it.
4. **Scalable catalog engine** — works on 10k rows, not just the demo product.

## The one non-negotiable rule

**No field ships without provenance.** Every enriched attribute carries:

```python
{"value": ..., "unit": ..., "source": "input|document|inference|web",
 "evidence": "<quote or doc ref>", "confidence": 0.0-1.0}
```

If the model can't ground a value, it returns `null` with a reason. **Abstaining is a
correct answer; a confident hallucinated torque rating is a failed demo.** Judges will
paste a garbage SKU to see what happens.

## Stack (defaults — change if a reason appears, not on taste)

- **Python 3.11+**, FastAPI backend, Pydantic v2 for the schema.
- **LLM: provider-agnostic**, selected by the `LLM_PROVIDER` env var in `src/llm.py`.
  Default `gemini` (free tier, no card). Also `ollama` (free, local, no account) and
  `anthropic` (paid). All three take a schema-constrained JSON call — we never
  regex a prose reply, and the prompts are identical across providers.
- **SQLite** for storage. Postgres only if we actually hit a wall.
- **Frontend**: whatever renders a table and a detail panel fastest. No SPA framework
  unless the demo needs interactivity that plain HTML can't do.

Pydantic model **is** the LLM schema **is** the API response. One definition, three uses.
Do not maintain parallel type definitions.

## Pipeline

```
ingest → normalize → enrich (LLM) → validate → score → persist
```

- **ingest**: CSV/JSON/PDF/URL → raw records.
- **normalize**: units, casing, known-synonym attribute names. Deterministic code, no LLM.
  Cheap and reproducible — don't spend a model call on `"MM" → "mm"`.
- **enrich**: LLM fills gaps, writes description, assigns category, extracts specs from
  attached docs. Batched.
- **validate**: second pass — cross-check units/ranges, flag contradictions against source
  evidence. This is a distinct step and must stay distinct; it's an explicit judging criterion.
- **score**: per-field confidence + record completeness %.
- **persist**: record + full audit trail of what changed and why.

## Conventions

- Deterministic work stays in Python. Reach for the LLM only for genuine language/inference
  tasks. Every model call costs latency and adds a hallucination surface.
- Enrichment is **idempotent and resumable** — re-running must not duplicate rows or
  re-pay for already-enriched fields. Catalog runs will get interrupted during the demo.
- Batch and cache by default. A demo that processes one product per API call does not read
  as a "scalable catalog engine."
- Prompts live in one module as named constants, not inline f-strings scattered across
  files. They will be tuned repeatedly and under time pressure.
- Mark deliberate shortcuts with `# ponytail:` naming the ceiling and the upgrade path.

## Testing

One `test_pipeline.py` with assert-based checks on the parts that silently break:
unit normalization, schema validation, confidence thresholds, idempotent re-runs.
No fixtures, no framework ceremony. Keep a **golden set of ~10 real products** with
hand-checked expected output — it is the only defense against a prompt tweak quietly
regressing accuracy.

## Build plan

Working end-to-end thin slice beats a half-built impressive architecture. Each
milestone is demoable on its own — never leave the repo in a state where nothing runs.

| # | Milestone | Files | Done when |
|---|---|---|---|
| 1 | Schema | `src/models.py` | `Product` validates; provenance is unavoidable by construction |
| 2 | Deterministic normalize | `src/normalize.py` | units/casing/synonyms fixed with zero model calls |
| 3 | Enrich | `src/llm.py`, `src/enrich.py` | one sparse record → full `Product` with evidence per field |
| 4 | Validate | `src/enrich.py` | second pass flags contradictions and downgrades confidence |
| 5 | Persist | `src/store.py` | SQLite upsert by SKU + audit row per run; re-run is idempotent |
| 6 | Batch CLI | `src/pipeline.py` | a CSV of N products runs end to end and resumes after Ctrl-C |
| 7 | UI | `src/app.py` | table + detail panel showing value, evidence, confidence |
| 8 | Scale story | — | numbers on throughput/cost, plus what breaks at 100k |

Current position lives in `HANDOVER.md`, not here — this table is the route, that
file is the odometer.

Not now (say so out loud rather than silently building it): auth, multi-tenancy,
user accounts, plugin systems, microservices, custom vector DB.
