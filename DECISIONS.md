# DECISIONS

Append-only log of meaningful decisions and the reasoning behind them.
Newest at the bottom. **Never rewrite history here** — if a decision is reversed,
add a new entry that supersedes the old one and link back to it.

Format:

```
## NNN — <decision> (YYYY-MM-DD)
**Context:** what forced a choice
**Decision:** what we picked
**Why:** reasoning, and what we rejected
**Cost accepted:** what this makes harder
```

---

## 001 — Every enriched field carries provenance and confidence (2026-08-11)

**Context:** The challenge asks for "explainable outputs" as an explicit objective, and
the core failure mode of LLM enrichment is a confidently invented spec value.

**Decision:** No field ships bare. Every enriched attribute is
`{value, unit, source, evidence, confidence}`. If the model can't ground a value it
returns `null` with a reason instead of guessing.

**Why:** Explainability is the stated judging criterion, so it can't be a post-hoc
"add citations" layer — it has to be the data model, or it won't survive contact with
a deadline. Abstention also protects the demo: a judge pasting a garbage SKU sees
honest gaps rather than a fabricated torque rating, and honest gaps read as engineering
maturity. Rejected: plain flat `{field: value}` output with a separate explanations
blob, because the two drift apart the moment anything is edited.

**Cost accepted:** Every response is ~4x more verbose, prompts are longer, token cost
rises, and the UI has to render nested structure instead of a flat table.

---

## 002 — Python + FastAPI + Pydantic v2 + SQLite (2026-08-11)

**Context:** Empty repo, hackathon timeline, needs LLM structured output and a demo UI.

**Decision:** Python 3.11+, FastAPI, Pydantic v2, SQLite. One Pydantic model serves as
LLM schema, validation layer, and API response shape.

**Why:** Pydantic already does schema-constrained LLM output, input validation, and
JSON serialization — three jobs, one definition, no parallel type declarations to keep
in sync under time pressure. SQLite needs zero setup and zero running service, which
matters when the demo machine is a stranger's laptop. Rejected Postgres (ops cost with
no payoff at demo scale) and rejected hand-rolled dataclasses + manual JSON parsing.

**Cost accepted:** SQLite has weak concurrent-write behavior. If parallel enrichment
workers become real, this needs revisiting — swap is small because the persistence
layer is thin by design.

---

## 003 — Claude with tool-use structured output, tiered by difficulty (2026-08-11)

**Context:** Need reliable JSON out of an LLM across a large catalog, cheaply.

**Decision:** Claude via tool-use / JSON schema for all extraction. `claude-sonnet-5`
for bulk records, `claude-opus-5` reserved for ambiguous or low-confidence ones.

**Why:** Tool-use enforces the schema at the API layer, so we never regex a prose reply
— that parsing code is the classic source of 2am demo failures. Tiering by difficulty
keeps the "scalable catalog engine" claim credible on cost without giving up quality on
the hard rows that actually get inspected.

**Cost accepted:** Two model paths means two prompt variants to keep aligned, plus a
routing heuristic to decide which records are "hard."

---

## 004 — Deterministic work stays out of the LLM (2026-08-11)

**Context:** Tempting to let the model do normalization too, since it's already reading
the record.

**Decision:** Unit conversion, casing, and known-synonym attribute mapping are plain
Python, run before enrichment.

**Why:** `"MM" → "mm"` is reproducible, free, and instant in code; through a model it is
none of those and adds a hallucination surface for zero gain. Also makes the enrichment
step's contribution measurable — we can point at exactly what the AI added.

**Cost accepted:** A hand-maintained synonym/unit map that will always be incomplete.
Unknown attributes fall through to the LLM path.

---

## 005 — Documentation continuity system (2026-08-11)

**Context:** Every new AI session starts with amnesia; re-explaining the project each
time burns the scarcest hackathon resource.

**Decision:** Five artifacts — `HANDOVER.md` (volatile current state), `DECISIONS.md`
(append-only rationale), `FLOW.md` (execution path), `BUG.md` / `FEATURE.md` (per-item
traces) — updated at the end of every session per the protocol in `CLAUDE.md`.

**Why:** They're separated by time horizon on purpose. HANDOVER is rewritten because
stale status is worse than no status; DECISIONS is append-only because the value is
precisely the record of what was considered and rejected. Merging them produces a file
that is both a bad status board and a bad history.

**Cost accepted:** Real per-session overhead. Justified only if the files are actually
read at session start — a doc system nobody reads is pure cost.

---

## 006 — BUG.md / FEATURE.md as running logs, not one file per item (2026-08-11)

**Context:** The trace-per-item idea implies a folder of files.

**Decision:** Two files, one section per item, newest at top. Split an item into its own
file only when it grows unwieldy.

**Why:** At zero bugs and zero features, a folder of stub files is scaffolding for work
that hasn't happened. Two files stay greppable and readable in a single pass.

**Cost accepted:** These get long over a busy project. The split path is known and cheap.
