"""The two LLM stages: enrich, then validate. Both use schema-constrained output.

Prompts live at the top of this module as named constants rather than as inline
f-strings scattered across call sites — they get tuned repeatedly and under time
pressure, and hunting them through the codebase mid-demo is how you lose an hour.

Enrich and validate are deliberately separate calls. Asking one call to both
produce and grade its own work gets you a rubber stamp: the model has already
committed to the answer. A second call with fresh context, shown the claim and
its evidence and asked only "does this hold up?", actually finds contradictions.
It is also the "AI validation" judging criterion made legible instead of buried
inside a generation prompt.

Which model answers is `src/llm.py`'s problem, not this file's. Nothing here
imports a vendor SDK — the prompts and the two-pass structure are the same
whether we run on Gemini's free tier, a local Ollama model, or Claude.
"""

from concurrent.futures import ThreadPoolExecutor

from . import checks, llm
from .models import Issue, Product, RawRecord, Sourced, ValidationReport
from .normalize import normalize_specs

ENRICH_SYSTEM = """You turn sparse industrial product records into structured, \
commerce-ready data.

The single rule: never state a value you cannot justify. For every field you fill, \
the `evidence` must be either a direct quote from the input or an explicit, checkable \
line of reasoning ("Model prefix 'HDX-' is Bosch Rexroth's hydraulic cylinder series").

When the input does not support a value, set `value` to null, put the reason in \
`evidence`, and move on. A null with a reason is a correct answer and is what we \
want. An invented torque rating or a guessed voltage is a failure, even if it \
sounds plausible — a buyer may order against it.

Confidence calibration, applied honestly:
- 0.9-1.0  stated verbatim in the input
- 0.7-0.9  strongly implied by the input (an explicit series name, a stated family)
- 0.5-0.7  reasonable inference from what the record itself says
- below 0.5  a guess. Prefer null.

A part number is an identifier, not a specification, and **not a source**. \
Resembling a known numbering scheme tells you what a part might be called; it \
tells you nothing about what this supplier's record says.

From a part number alone you must NOT emit:
- a brand or manufacturer. If the record does not name one anywhere — in a \
  column or in the text — `brand` is null. A SKU that looks like Allen-Bradley's \
  or SKF's scheme is not the record naming them; house brands and third-party \
  parts copy those schemes constantly, and a wrong manufacturer on a live \
  catalog page is the most expensive error in this whole job.
- dimensions, materials, clearances, tolerances, ratings, or seal/contact \
  configurations, however standard they are for that designation.

Brand is grounded only when the record itself contains it. "3M 775L Stikit Film" \
names 3M — use it. "Hersteller: Schaeffler" names Schaeffler — use it. \
"1756-IF16-XT" with an empty manufacturer column names nobody.

Equally: an attribute NAMED in marketing copy without a value is not a value. \
"Delivers exceptional power and a wide voltage range" states no wattage and no \
voltage. Extract nothing from it.

Set `source` to what actually happened: `input` for the record's own fields, \
`document` for attached text, `inference` for your own product knowledge.

Extract specs only where you have grounds. Four solid specs beat twenty padded ones.

OUTPUT FORMAT RULES (Unilog delivery standard). These are house style, not \
suggestions — a description in the wrong casing or length is a failed field even \
when every fact in it is right.

- `category` is a three-level Classpath joined by " > ", broad to narrow:
  "Appliances & Consumer Electronics > Kitchen Appliances > Built-In Dishwashers".
  If you cannot place all three levels, return null rather than a partial path.
- `name` is the bare item type — "Dishwasher", "Ball Bearing", "Sanding Belt".
  Not the brand, not the part number, not a sentence.
- Spec labels are Title Case nouns: "Voltage Rating", "Bore Diameter", not
  "voltage rating" or "VOLTAGE".
- Units go in the spec's `unit` field, never inside `value`. Write "25" + "mm",
  not "25mm". Use the standard abbreviation: in, mm, V, A, W, lb, kg, dBA.
- Imperial measurements use fractions, not decimals: 50-1/4 in, not 50.25 in.

The five descriptions, each written to its own rule:
- `invoice_desc`  <=40 characters, ALL CAPS, heavily abbreviated. Fits a till
  receipt: "DISHWASHER LEG 5 SST 120V 15A 50-1/4IN"
- `mobile_desc`   comma-separated, in this order, including EVERY part you have
  grounded: Manufacturer, Brand, Item Type, Series, then the part number.
  "Rheem Manufacturing FRIGIDAIRE, Dishwasher, Professional Series, PDSH4816AF".
  Include the part number — it is what makes this variant useful on a phone.
  (This lands at 60-80 characters when complete. Build it from the parts; do not
  count characters and do not pad to reach a length. If you only have two of the
  parts, a short line is correct and the omission is real.)
- `short_desc`    the product title: Brand + Series + MPN + Item Type + key
  attributes, in that order
- `retail_desc`   shopper-facing, NO brand and NO part number: "Professional
  Series Dishwasher, Leg Mounting, 5-Wash Cycle, Stainless Steel"
- `description`   the long form: full detail, sentence case, units spelled per
  the rules above

Every one of these follows the same grounding rule as any other field. If the \
record does not support enough content to write a variant honestly, return null \
for it. Do not pad a description to reach a character count, and do not repeat \
one variant's text in another.

Reply with JSON matching the schema. No prose, no markdown fences."""

ENRICH_USER = """Enrich this industrial product record.

<record>
{record}
</record>

Copy the SKU through unchanged. Fill what the record supports; leave the rest null \
with a reason."""

VALIDATE_SYSTEM = """You audit enriched product data. You did not write it, and \
your job is to find what is wrong with it, not to approve it.

You get the original raw input and the enriched record. For each field, check:

1. Contradiction — does the value conflict with the raw input?
2. Support — does the stated `evidence` actually establish the value? Evidence that \
   restates the claim ("It is a 24V relay because it is a 24V relay") supports nothing.
3. Plausibility — is the value physically sensible for this product class? A 400V \
   coil on a signal relay, a 30 kg handheld tool, a bore larger than the body.
4. Units — present, correct, and consistent with the quantity.
5. Calibration — is the confidence justified by the evidence quality? Over-confident \
   inference is the failure mode we most care about catching.

Report only real problems. An empty issues list is the right answer for a clean \
record, and inventing issues to look thorough is itself a failure.

Verdict: `pass` publishable as is; `revise` real problems but a usable core; \
`reject` the record is fundamentally ungrounded.

Reply with JSON matching the schema. No prose, no markdown fences."""

VALIDATE_USER = """<raw_input>
{record}
</raw_input>

<enriched>
{enriched}
</enriched>

Audit the enriched record against the raw input."""


def enrich_one(record: RawRecord) -> Product:
    """One sparse record -> one fully-provenanced Product."""
    product = llm.structured_call(
        ENRICH_SYSTEM,
        ENRICH_USER.format(record=record.as_prompt_block()),
        Product,
    )
    product.sku = record.sku  # the SKU is ours, not the model's, even if it echoes it back
    # The model names its own attributes, so canonicalise them the same way we
    # canonicalise input attributes. Runs BEFORE validation so the deterministic
    # rules and the LLM both audit one consistent spelling of each spec — and so
    # `checks.contradictory_specs` can see that "Voltage: 24" and "voltage: 240"
    # are two claims about one attribute rather than two unrelated specs.
    product.specs = normalize_specs(product.specs)
    return product


# Marker for "the audit never ran". Lives here so that anything reading a report
# can distinguish a validator that found problems from one that never executed —
# they are both non-empty issue lists, and conflating them turns an API failure
# into a false "we caught something".
UNAUDITED_MARKER = "Validation pass did not complete"


def is_unaudited(report: ValidationReport) -> bool:
    return any(i.field == "*" and UNAUDITED_MARKER in i.detail for i in report.issues)


def validate_one(record: RawRecord, product: Product) -> ValidationReport:
    """Second pass. Fresh context, adversarial framing, no memory of writing it."""
    try:
        return llm.structured_call(
            VALIDATE_SYSTEM,
            VALIDATE_USER.format(
                record=record.as_prompt_block(),
                enriched=product.model_dump_json(indent=2),
            ),
            ValidationReport,
        )
    except llm.ProviderError:
        raise  # setup problem: fail the run rather than silently shipping unaudited data
    except Exception as exc:  # noqa: BLE001
        # A failed audit must never read as a clean bill of health. Flag the whole
        # record so `apply_report` retracts it to zero confidence, and keep the
        # reason in the audit trail.
        return ValidationReport(
            issues=[Issue(
                field="*", severity="unsupported",
                detail=f"{UNAUDITED_MARKER} ({type(exc).__name__}); record is unaudited.",
                suggested_confidence=0.0,
            )],
            verdict="revise",
        )


def apply_report(product: Product, report: ValidationReport) -> Product:
    """Fold validation findings back into the record. Pure Python — the model
    finds problems, deterministic code applies consequences, so the same report
    always produces the same scores.

    Confidence only ever moves *down* here. A validator that could raise
    confidence would let a second opinion launder a bad first one.
    """
    # Field name -> every field answering to it. A LIST, not a single field: two
    # specs can legitimately share a canonical name when they contradict each
    # other, and a dict that kept only the last one would flag a contradiction
    # while quietly leaving the other claim published at full confidence.
    named: dict[str, list[Sourced]] = {
        "name": [product.name], "brand": [product.brand],
        "category": [product.category], "description": [product.description],
    }
    for spec in product.specs:
        named.setdefault(f"spec:{spec.name}", []).append(spec)

    for issue in report.issues:
        if issue.field == "*":
            targets = [field for group in named.values() for field in group]
        else:
            # A validator naming a field that doesn't exist is ignored, not fatal.
            targets = named.get(issue.field, [])
        for field in targets:
            field.confidence = min(field.confidence, max(0.0, issue.suggested_confidence))
            field.evidence = f"{field.evidence} [flagged: {issue.detail}]"
    return product


def process(record: RawRecord) -> tuple[Product, ValidationReport]:
    """Full per-record path: enrich -> validate -> check -> apply. Two calls.

    `checks.merge` adds the deterministic rules to whatever the model reported.
    It runs unconditionally, including when `validate_one` fell back to the
    unaudited marker: a record whose validation call died still gets the free
    half of the audit, which on a free tier is the difference between a degraded
    record and an unexamined one.
    """
    product = enrich_one(record)
    report = validate_one(record, product)
    report = checks.merge(record, product, report)
    return apply_report(product, report), report


def process_batch(
    records: list[RawRecord], workers: int | None = None
) -> list[tuple[RawRecord, Product | None, ValidationReport | None, str | None]]:
    """Run records concurrently.

    Returns a row per input record including failures, so a single bad record
    can never silently vanish from a catalog run — the caller decides whether a
    failure is fatal. Order matches the input.

    The worker default comes from the provider, because free tiers are limited
    per minute and this pipeline makes two calls per record: six workers against
    a 10-requests-per-minute quota spends the whole batch in backoff.
    """
    llm.check_ready()  # fail before spawning threads, not halfway through a quota
    workers = workers or llm.workers_default()

    def run(record: RawRecord):
        try:
            product, report = process(record)
            return record, product, report, None
        except llm.ProviderError:
            # Setup problems are not per-record failures — the same error would
            # repeat for every remaining row. Abort the run and say so once.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
            return record, None, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, records))

# ponytail: live per-record calls, so throughput is bounded by whatever the
# provider's quota is. For a real 100k-row catalog the batch endpoints are the
# right answer (roughly half price, huge job sizes) — skipped because their
# results can take up to an hour to land, which is useless during a live demo.
