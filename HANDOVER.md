# HANDOVER

**Read this first, every session.** State of the project right now — not history.
Rewritten (not appended) at the end of every chat.

Last updated: 2026-08-11 22:57 IST

---

## Where things stand

Project is **scaffolding only**. No source code has been written yet.

Repo contents:

- `CLAUDE.md` — project brief, stack defaults, conventions, session-end protocol.
- `HANDOVER.md` — this file.
- `DECISIONS.md` — why each choice was made.
- `FLOW.md` — execution path (currently PLANNED, not ACTUAL — nothing runs yet).
- `BUG.md` / `FEATURE.md` — work traces. Both empty.

Not a git repo yet. No `requirements.txt`, no source tree, no tests.

## Done

- Problem statement captured: AI-powered product intelligence for industrial commerce
  (sparse product input → structured, validated, explainable catalog records).
- Stack chosen as defaults: Python 3.11+, FastAPI, Pydantic v2, SQLite, Claude
  (`claude-sonnet-5` bulk / `claude-opus-5` hard cases).
- Core architectural rule locked: **every enriched field carries provenance +
  confidence; ungrounded fields return `null` with a reason rather than a guess.**
- Doc/continuity system set up (this file and its siblings).

## In progress

Nothing. Next session starts cold on implementation.

## Next up (suggested order)

1. `git init` — nothing is version-controlled yet, which is the single largest risk today.
2. Pydantic `Product` model with the provenance-wrapped field type. Everything else
   depends on this shape, so it lands first.
3. One product end-to-end through the pipeline, hardcoded input, no API, no UI.
4. CSV batch ingest.
5. Validation pass as a separate step.
6. Minimal UI: table + detail panel showing evidence and confidence.

## Broken / known issues

None — nothing runs yet.

## Avoid

- Don't build auth, multi-tenancy, microservices, or a plugin system. Explicitly out
  of scope; a hackathon judge will never see them.
- Don't spend LLM calls on deterministic work (unit casing, string normalization).
- Don't add a frontend framework until plain HTML demonstrably can't do the job.
- Don't let an enrichment path emit a value without evidence — that breaks the one
  rule the whole submission is built on.

## Open questions for the user

- Is there a provided dataset / sample catalog from the organizers, or do we source
  our own sample industrial products?
- Submission deadline and demo format (live demo vs recorded vs repo only)?
- Team size — are others writing code in this same repo?
