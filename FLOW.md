# FLOW

How execution actually travels — file to file, function to function, in order.

> **STATUS: PLANNED, NOT ACTUAL.** No source code exists yet. Every path and function
> name below is intended design, not something you can open. Do not trust a path in this
> file until it moves to the ACTUAL section. As code lands, replace planned entries with
> real `file.py:function()` references and delete the guesses.

Last updated: 2026-08-11 22:57 IST

---

## ACTUAL (verified against real code)

*(empty — nothing implemented)*

---

## PLANNED

### Top-level pipeline

```
input (CSV / JSON / PDF / URL)
  │
  ├─► ingest      → raw records                  [deterministic]
  ├─► normalize   → units, casing, synonyms      [deterministic]
  ├─► enrich      → LLM fills gaps               [Claude, batched]
  ├─► validate    → LLM + rules cross-check      [Claude + deterministic]
  ├─► score       → per-field confidence, completeness %
  └─► persist     → record + audit trail
```

Each stage takes a list of records and returns a list of records. Same shape in, same
shape out — so any stage can be skipped, re-run, or tested in isolation. That uniformity
is what makes the pipeline resumable.

### Stage contracts

| Stage | Input | Output | LLM? | Notes |
|---|---|---|---|---|
| ingest | file path / URL | `list[RawRecord]` | no | PDF path is the risky one |
| normalize | `list[RawRecord]` | `list[RawRecord]` | no | pure function, easy to test |
| enrich | `list[RawRecord]` | `list[Product]` | yes | batched; skips already-filled fields |
| validate | `list[Product]` | `list[Product]` | yes | separate pass — a judging criterion |
| score | `list[Product]` | `list[Product]` | no | derives from field confidences |
| persist | `list[Product]` | ids | no | upsert by SKU, writes audit rows |

### Why validate is its own pass

Asking one call to both generate and check its own work in a single breath gets a model
that rubber-stamps itself. A separate pass gets fresh context containing the claim and
its evidence, and asks only "does this hold up?" It is also directly legible to a judge
as the "AI validation" criterion, rather than being buried inside a generation prompt.

### Resumability

`enrich` must be idempotent: re-running over a record with populated fields skips them
rather than regenerating. Enables killing and restarting a catalog run — which will
happen during the demo — without duplicate rows or duplicate spend.

---

## Currently modifying

*(nothing in flight)*

When work is in progress, name the exact stage and function being changed here, so the
next session knows which part of the path is half-built.
