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

---

## 014 — Verification harnesses are separate from the test suite (2026-08-12)

**Context:** Two of the four judging criteria — AI validation, and accuracy —
cannot be checked offline. They need real model calls, which cost quota and take
minutes. But `test_pipeline.py` must stay free and instant or it stops being run.

**Decision:** Three tiers, not one. `test_pipeline.py` (offline, free, ~1s),
`src/probe.py` (~6 calls, does the validator object?), `src/golden.py` (~16
calls, does enrichment hallucinate?).

**Why:** Merging them produces a suite too slow and expensive to run on every
edit, which means it stops being run at all. Splitting also matches how the
questions differ: the unit tests guard logic that must never change, the probe
and golden set measure behaviour that will drift as prompts are tuned.

Both harnesses carry a design constraint learned from BUG-003: **an instrument
needs a control and an explicit error state.** The probe has `clean-control`
(must stay silent) and reports `NOT MEASURED` rather than a catch rate when the
control didn't run. Both call `is_unaudited()` to exclude API failures from
scoring instead of counting them as results.

**Cost accepted:** Two more entry points to maintain, and the expensive tiers
will be run rarely — so a prompt regression can survive for a while before
anyone notices. Mitigated by making them cheap to invoke on one case
(`python -m src.probe clean-control`, `python -m src.golden --quick 3`).

---

## 015 — The UI is read-only (2026-08-12)

**Context:** Milestone 7. The obvious design has an "enrich this" button.

**Decision:** `src/app.py` renders only what the pipeline already persisted. No
model calls, no writes, no enqueue endpoint.

**Why:** It makes the demo unbreakable by the thing most likely to break it. The
free tier allows roughly 10 records/day/model; a UI that enriches on demand is
one impatient click away from a 429 in front of judges. Read-only means the
demo's failure modes are limited to "SQLite file missing". Enrichment stays in
the CLI, where retries, pacing, and resumption already work and where a failure
is visible in a terminal rather than a spinner.

**Cost accepted:** No live "watch it enrich" moment in the browser — that has to
be demoed from the CLI. Given the quota, running it live on stage was never the
right plan anyway; pre-enriching and demoing the result is.

---

## 016 — Golden expectations must be verifiable from the input alone (2026-08-12)

**Context:** Expanding the golden set from 8 to 32 records. Records were drafted
with model assistance, which raises an obvious objection: if a model writes both
the test and its expected answer, the "golden" set is just one model's opinion
grading another's — the exact circularity this project exists to avoid.

**Decision:** Every expectation must be checkable by reading that record's own
`attributes` and `text` and asking *"is this stated here?"* — never by knowing
real-world facts about the part. Enforced mechanically by
`test_golden_set_is_structurally_valid`: every `values` string and every
`required_specs` entry must appear verbatim in the input, or the test fails.

**Why:** It makes correctness independent of who authored the record. A stranger
with no industrial-products knowledge can audit the entire set. It also means
the drafting process can be assisted without the result being model opinion —
the assertions survive because they are mechanically grounded, not because we
trust the drafter.

**Cost accepted:** We cannot test whether the system knows real product facts
(that a 6205 bearing has a 25mm bore). Fine: that is not what we are measuring.
We measure whether it invents values the input doesn't support.

---

## 017 — `deferred_specs` is a distinct trap from `forbidden_specs` (2026-08-12)

**Context:** `VLV-SOL-DS` says *"Torque: see datasheet"*. Torque was listed in
`forbidden_specs`, but the word plainly appears in the input — so the structural
test flagged it as a rule that would penalise the model for reading correctly.

**Decision:** Two separate keys, with opposite structural requirements:

| Key | The attribute is | Test asserts |
|---|---|---|
| `forbidden_specs` | never mentioned at all | string **absent** from input |
| `deferred_specs` | named but explicitly unvalued | string **present** in input |

Both score the same way — the spec must not come back grounded — but they test
different failure modes and are validated in opposite directions.

**Why:** Deferral is the subtler and more realistic trap: the attribute name is
sitting right there in the text ("Weight: TBD", "Kv: refer to catalogue"),
inviting the model to supply a plausible number for a field the supplier
explicitly declined to fill. Real supplier data is full of these. Conflating
them under one key meant either mis-encoding the deferral traps or, as the
verify pass did, quietly dropping them — leaving the sharpest cases untested.

**Cost accepted:** One more expectation key to explain. Cheap relative to a
whole trap category going unmeasured.

---

## 018 — Golden scores accumulate and rotate across models (2026-08-12)

**Context:** 32 records x 2 calls = 64 requests. The free tier allows ~20 per day
**per model**. A 32-record set is therefore unrunnable in one sitting on one
model — the set would be permanently unmeasurable as designed.

**Decision:** Three mechanisms in `src/golden.py`:
1. Scores cache to `golden_scores.json` after **each** record, so an interrupted
   run keeps its work and a later run continues where it stopped.
2. `--models a,b,c` rotates to the next model after two consecutive failures,
   since quota is per-model.
3. `--report` aggregates cached scores with **zero** API calls, and names every
   model that contributed.

**Why:** This is what made 28/32 records scoreable in a single session — one
model alone reached 2. Caching per record rather than per run matters because
quota death is the normal ending, not the exceptional one.

**Cost accepted, and it is a real one:** a headline number can blend models.
That is a genuine methodological weakness, mitigated only by reporting the mix
rather than hiding it. It bit immediately — all four hallucinating records landed
on one model, leaving "hard traps" and "weaker model" confounded (BUG-004). When
quota allows, prefer scoring the whole set on one model and treat rotation as
the fallback it is.

---

## 019 — Deterministic rules validate alongside the LLM, not instead of it (2026-08-12)

**Context:** Validation was one model call. That made the project's second
judging criterion entirely dependent on quota, and on a free tier "the audit ran"
is not something you can assume — `validate_one` already had a fallback for the
call simply failing. A record whose validation died got a retracted confidence
and no findings at all.

Separately, three of the failures we can actually observe are mechanical:
BUG-004's decoded part numbers, a unit from the wrong family, and two specs
naming one attribute with different values. None of those need a language model
to notice.

**Decision:** `src/checks.py` — four deterministic rules producing the same
`Issue` type the model produces, merged into the report by `checks.merge` inside
`process()`. It runs unconditionally, including when the validation call failed.

The rules are: provenance claimed as `input` for a value not in the input;
a spec decoded from the identifier (BUG-004); a numeric quantity with a
wrong-family or missing unit; two specs that canonicalise to one name and
disagree.

**Why not replace the LLM validator:** the probe shows it catching things no
table can — an 85 g relay reported as 45 kg is implausible only if you know what
a relay is, and "it is a Siemens relay because it is a Siemens relay" is circular
only if you can read. Deterministic rules are a floor, not a ceiling.

**Why not fold them into the prompt instead:** a prompt cannot be tested offline
and can regress silently. These rules are unit-tested, run in ~0 ms, cost
nothing, and behave identically every time.

**Rejected:** running the rules *before* the model and skipping the call on a
clean record. It would save quota and would also mean the cleanest records — the
ones a judge is most likely to inspect — were the ones we never audited properly.

**Cost accepted:** the rules can only be as good as their tables, and a rule that
fires wrongly damages good data. Mitigated by giving every rule a control case in
the test suite, including one that asserts the rules stay silent on the exact
fixture `probe.py` requires the LLM validator to call clean. If those two
instruments ever disagree, one of them is wrong and the test says so.

---

## 020 — Normalization runs on model output, not only on model input (2026-08-12)

**Context:** `normalize.py` cleaned records on the way in and nothing on the way
out. But the model names its own attributes, so the catalog stored whatever it
called them that minute — "Operating Voltage", "voltage", "VOLTAGE" — with units
spelled "VDC", "volts", or "V DC". "Normalized units, deduped attributes" is a
judging criterion, and it held only for the data we did not generate.

**Decision:** `normalize_specs` runs in `enrich_one` immediately after the model
replies and before validation, so both validators see one consistent spelling.

**The interesting sub-decision:** only *exact* duplicates are merged. Two specs
sharing a canonical name but disagreeing about the value are both kept, and
`checks.contradictory_specs` reports them. Merging them would have meant picking
a winner on the strength of a self-reported confidence score, and resolving a
contradiction by deleting one side of it is precisely the behaviour this project
exists to argue against.

That in turn forced a fix in `apply_report`, which mapped field name → one field
and would have flagged the contradiction while leaving the other claim published
at full confidence. It now maps name → every field answering to it.

**Cost accepted:** canonicalisation is only as good as the alias table, and
running it on model output means a bad alias now corrupts generated data as well
as ingested data. The tables are small and tested; the exposure is understood.

---

## 021 — The demo seeds from an exported catalog, never from a live run (2026-08-12)

**Context:** The UI has been read-only since DECISIONS 015 precisely so a rate
limit cannot break the demo. But it renders `catalog.db`, and `catalog.db` is
gitignored — so on any machine that has not personally run the pipeline, the
"read-only, unbreakable" UI shows an empty table. That is exactly the machine a
demo happens on.

**Decision:** `--export` dumps the enriched catalog to JSON; `--seed` loads it
back, making zero model calls. Enrich once where there is quota, export, commit
the file, seed anywhere.

**Why not commit `catalog.db`:** a binary blob nobody can diff or review, and it
would go stale invisibly. JSON shows up in a pull request.

**Why not generate a demo fixture by hand:** it was considered and rejected
outright. Hand-written records presented as model output are fabricated evidence,
in a project whose entire claim is that every value is traceable to where it
actually came from. The seed file must come from a real run.

**Why seeding preserves the original timestamps:** an imported record keeps its
real `enriched_at` and gains one extra audit row saying it arrived by import.
Stamping imported data as freshly enriched would make the audit trail lie about
when — and an audit trail that lies about when is barely better than none.
