# BUG

One section per bug, newest at top. Trace it start to finish — a cold reader (human or
AI) should be able to pick up mid-investigation without asking anyone anything.

Split a bug into its own file only if its section outgrows this one.

Template:

```
## BUG-NNN — <one-line symptom>   [OPEN | FIXED | WONTFIX]
**Found:** how it surfaced (demo, test, user report) + date
**Symptom:** exact observed behavior. Quote real error text.
**Scope:** files/stages on the suspect path (see FLOW.md)
**Tried:**
  - <attempt> → <result>
**Root cause:** the actual why, not the symptom
**Fix:** what changed, where
**Verified:** how we know it's fixed — the specific check that now passes
```

Record failed attempts too. "We tried X, it didn't work" saves the next session from
re-running the same dead end, and that is most of this file's value.

---

*(no bugs logged yet)*
