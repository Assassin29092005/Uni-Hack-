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

from . import llm
from .models import Issue, Product, RawRecord, ValidationReport

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
- 0.7-0.9  strongly implied by the input (model number decoding, explicit series)
- 0.5-0.7  reasonable inference from well-known product families
- below 0.5  a guess. Prefer null.

Set `source` to what actually happened: `input` for the record's own fields, \
`document` for attached text, `inference` for your own product knowledge.

Extract specs only where you have grounds. Four solid specs beat twenty padded ones.

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
    return product


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
                detail=f"Validation pass did not complete ({type(exc).__name__}); record is unaudited.",
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
    named = {"name": product.name, "brand": product.brand,
             "category": product.category, "description": product.description}
    named.update({f"spec:{s.name}": s for s in product.specs})

    for issue in report.issues:
        targets = named.values() if issue.field == "*" else [named.get(issue.field)]
        for field in targets:
            if field is None:
                continue  # validator named a field that doesn't exist; ignore rather than crash
            field.confidence = min(field.confidence, max(0.0, issue.suggested_confidence))
            field.evidence = f"{field.evidence} [flagged: {issue.detail}]"
    return product


def process(record: RawRecord) -> tuple[Product, ValidationReport]:
    """Full per-record path: enrich -> validate -> apply. One record, two calls."""
    product = enrich_one(record)
    report = validate_one(record, product)
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
