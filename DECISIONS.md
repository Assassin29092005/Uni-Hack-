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

---

## 007 — One model for every record; tiering deferred (2026-08-12)

**Supersedes the tiering half of [003].** The structured-output and tool-use
decisions in 003 stand unchanged.

**Context:** 003 specified `claude-sonnet-5` for bulk records and `claude-opus-5`
for hard ones. Implementing that needs a routing heuristic plus a second prompt
variant kept aligned with the first.

**Decision:** `MODEL = "claude-opus-5"` for both passes, single prompt pair.

**Why:** We can't yet tell which records are "hard" — no data on where quality
drops, so any routing rule would be invented rather than measured, and two
prompts would need to stay in sync during the exact hours we'll be tuning them
hardest. Routing on a guess also risks sending the genuinely ambiguous records to
the weaker model, which is the opposite of the intent.

**Cost accepted:** Every record pays Opus pricing. Revisit once we can measure
which records are hard — likely routing on post-normalize field count plus input
text length.

---

## 008 — Threads over the Batch API (2026-08-12)

**Context:** "Scalable catalog engine" is a judging criterion. The Message
Batches API is 50% cheaper and takes up to 100k requests per batch.

**Decision:** `ThreadPoolExecutor`, 6 workers, live API calls.

**Why:** Batch results can take up to an hour to land. That's correct for a
production catalog and useless for a live demo where a judge wants to watch
their own input become a record. Rejected: batching now and adding a "live mode"
later — two code paths to debug under time pressure.

**Cost accepted:** Full per-token price, throughput bounded by rate limits. The
batch path is the honest answer for the scale story and is recorded as the
upgrade path in a `ponytail:` comment in `enrich.py`.

---

## 009 — Validation can only lower confidence, never raise it (2026-08-12)

**Context:** `apply_report` folds validator findings back into the record; the
validator returns a `suggested_confidence` per issue.

**Decision:** `field.confidence = min(field.confidence, suggested)`. One-way.

**Why:** A validator that can raise confidence lets a second opinion launder a
bad first one — exactly the failure the separate validation pass exists to
prevent. Monotonic means the audit can only ever make us more cautious, which is
the direction we want to be wrong in. Guarded by a test.

**Cost accepted:** A genuinely under-confident correct value stays under-confident
and may be shown as a gap. We prefer that error to its opposite.

---

## 010 — Scoring is Python, not model output (2026-08-12)

**Context:** `Product` needs completeness and confidence scores. The model could
simply return them as fields.

**Decision:** `completeness`, `mean_confidence`, and `gaps` are Python
`@property` on `Product`, so they never appear in the JSON schema sent to Claude.

**Why:** A model asked to grade its own output grades it generously. Properties
are invisible to pydantic's schema generation, so this is enforced by the type
system rather than by prompt wording, and the same record scores identically on
every run. Related: specs count as **one** slot in completeness however many
there are — otherwise a record with 40 padded specs outscores one with 4 good
ones, and the metric starts rewarding padding.

**Cost accepted:** The scoring formula is a hand-tuned heuristic
(`CONFIDENCE_FLOOR = 0.45`), not anything calibrated. Revisit against the golden
set once it exists.

---

## 011 — Provider-agnostic LLM layer; free tier by default (2026-08-12)

**Supersedes the model choice in [003] and [007].** The structured-output
principle from 003 stands and is now enforced across all three providers.

**Context:** The user asked why we were on a paid API and whether a free model
could do the job. Correct question — this is a hackathon, not production, and
paying per record to iterate on prompts is a bad trade.

**Decision:** `src/llm.py` exposes one function, `structured_call(system, user,
schema) -> BaseModel`, dispatching on `LLM_PROVIDER`:

| Provider | Cost | Needs |
|---|---|---|
| `gemini` (default) | free tier | `GEMINI_API_KEY`, no credit card |
| `ollama` | free | local install + `ollama serve`, no account at all |
| `anthropic` | paid | `ANTHROPIC_API_KEY` |

**Why:** Every one of these supports schema-constrained decoding, so the pipeline
above the call site is unchanged — same prompts, same pydantic classes, same
two-pass structure. That is what makes swapping providers a config change rather
than a rewrite, and it is only possible because we never parsed prose.

This is a deliberate exception to the project's no-abstractions rule: there are
three real implementations, not one, and a free tier that rate-limits mid-demo is
a live failure mode a fallback actually solves. Rejected: committing to a single
free provider, which would leave no path when its quota runs out on demo day.

**Cost accepted:** Quality on the free tier is unmeasured — the prompts were
written against Claude's behavior and have never been run anywhere. The golden
set is now doing double duty: catching prompt regressions *and* telling us
whether the free model is actually good enough. Also a third code path to keep
working, mitigated by keeping the layer to one function.

---

## 012 — Rate-limit retry and provider-aware worker defaults (2026-08-12)

**Context:** Free tiers throttle **per minute**, not just per day. The pipeline
makes two calls per record; six parallel workers exhausts a 10-request/minute
quota before the first record finishes.

**Decision:** `structured_call` retries on rate-limit errors with exponential
backoff (4s, 8s, 16s), and each provider declares its own worker default —
2 for the free tiers, 6 for paid.

**Why:** Without this, choosing the free provider means the pipeline visibly
doesn't work, which would make the free option a false promise rather than a
real one. Detection matches on the exception message because the three SDKs
share no error hierarchy; it over-matches at worst, which is the harmless
direction.

**Cost accepted:** A 10-record batch on the free tier takes minutes, not
seconds. That is the actual shape of the free tier and the demo should be
planned around it — enrich the catalog ahead of time and let the resumable
store serve the demo, rather than running a cold batch on stage.

---

## 013 — The shared schema is permissive; providers add their own strictness (2026-08-12)

**Context:** BUG-001. Anthropic's structured outputs *require*
`additionalProperties: false` on every object; Gemini's `response_schema`
*rejects* it with a 400. One pydantic model has to serve both.

**Decision:** The shared models in `models.py` emit the **permissive** dialect —
no `extra: "forbid"` anywhere. `llm._close_objects()` injects the closed-object
marker, and only the Anthropic branch calls it.

**Why:** Between the two, permissive is the correct shared base: adding
constraints at the seam is easy, stripping them back out is fiddly and
error-prone (you have to walk nested `$defs` and not break `anyOf`). It also
keeps provider quirks in the one file whose job is provider quirks, instead of
letting a vendor requirement dictate the shape of the core data model. Rejected:
keeping `forbid` and stripping for Gemini — same effect, harder direction, and it
would leave the data model shaped by whichever vendor was integrated first.

Enforced by `test_schema_dialects_differ_per_provider`, whose failure message
tells the next person not to re-add `extra: forbid`. Without that guard this bug
returns the moment someone adds a model and copies the pattern from an old file.

**Cost accepted:** Pydantic no longer rejects unexpected keys in LLM output — it
ignores them. For model output that is arguably the better failure mode anyway,
but it does mean a provider inventing a field goes unnoticed rather than raising.
