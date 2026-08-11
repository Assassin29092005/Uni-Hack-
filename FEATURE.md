# FEATURE

One section per feature, newest at top. Scoped → built → verified, traced end to end.

Split a feature into its own file only if its section outgrows this one.

Template:

```
## FEAT-NNN — <name>   [SCOPED | IN PROGRESS | DONE | DROPPED]
**Scoped:** what it must do, and explicitly what it must NOT do
**Why:** which judging outcome or user need it serves
**Design:** approach chosen; alternatives rejected (link DECISIONS.md if it warranted an entry)
**Touches:** files/stages (see FLOW.md)
**Progress:**
  - <what was built> → <state>
**Verified:** the check that proves it works
**Left out:** deliberate gaps, and when they'd be worth filling
```

The `Left out` line matters most under a deadline — it separates "not built yet" from
"decided against," and stops a future session from rebuilding something we cut on purpose.

---

*(no features logged yet — see HANDOVER.md "Next up" for the intended build order)*
