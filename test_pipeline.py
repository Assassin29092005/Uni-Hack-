"""Runnable checks for the parts that break silently.

    python test_pipeline.py

No framework, no fixtures — asserts and a main block. Nothing here calls the
Anthropic API: these cover the deterministic logic where a regression produces
wrong data rather than an error, which is exactly the kind that survives to the
demo. Prompt quality is checked against the golden set, not here.
"""

import tempfile
from pathlib import Path

from src import llm, store
from src.enrich import apply_report
from src.models import CONFIDENCE_FLOOR, Issue, Product, Sourced, Spec, ValidationReport
from src.normalize import normalize_key, normalize_record, normalize_value
from src.pipeline import ingest_csv


def sourced(value, confidence=0.9, unit=None) -> Sourced:
    return Sourced(value=value, unit=unit, source="input", evidence="test", confidence=confidence)


def demo_product(**overrides) -> Product:
    base = dict(
        sku="TEST-1",
        name=sourced("Widget"),
        brand=sourced("Acme"),
        category=sourced("Fasteners"),
        description=sourced("A widget."),
        specs=[Spec(name="thread", value="M8", source="input", evidence="test", confidence=0.9)],
    )
    return Product(**{**base, **overrides})


def test_normalize_keys():
    assert normalize_key("MPN") == "sku"
    assert normalize_key("Part No.") == "sku"
    assert normalize_key("Mfr") == "brand"
    assert normalize_key("  Product Name ") == "name"
    # Unknown keys survive, cleaned — dropping them would lose real data.
    assert normalize_key("Coating Type") == "coating type"


def test_normalize_values():
    assert normalize_value("24VDC") == "24 V DC"
    assert normalize_value("3.4 KG") == "3.4 kg"
    assert normalize_value("  25MM  ") == "25 mm"
    assert normalize_value("10 - 30 V") == "10-30 V"
    # Unrecognised unit is kept, not silently dropped.
    assert normalize_value("5 gallons") == "5 gallons"
    # Documents a known ceiling: compound dimension strings pass through for the
    # model to read. If this ever starts splitting, that is a behaviour change.
    assert normalize_value("25 MM x 200MM") == "25 MM x 200MM"


def test_normalize_record():
    record = normalize_record(
        " HDX-1 ",
        {"Mfr": "Bosch", "Voltage": "24VDC", "Empty": "", "MPN": "ignored", "None": None},
        text="  hydraulic   cylinder  ",
    )
    assert record.sku == "HDX-1"
    assert record.attributes == {"brand": "Bosch", "operating voltage": "24 V DC"}
    assert record.text == "hydraulic cylinder"
    assert "HDX-1" in record.as_prompt_block()


def test_ingest_csv():
    records = ingest_csv(Path("data/sample_products.csv"))
    assert len(records) == 5, "one row per non-blank SKU"
    first = records[0]
    assert first.sku == "HDX-4025-200"
    assert first.attributes["brand"] == "Bosch Rexroth"
    assert "210 bar" in first.text, "Notes column feeds the free-text blob"
    # The deliberately-empty record survives ingestion; abstaining is enrich's job.
    assert records[-1].sku == "XYZZY-99999" and not records[-1].attributes


def test_confidence_floor_gates_grounding():
    assert sourced("Acme", confidence=CONFIDENCE_FLOOR).is_grounded
    assert not sourced("Acme", confidence=CONFIDENCE_FLOOR - 0.01).is_grounded
    assert not sourced(None, confidence=1.0).is_grounded, "null is never grounded"


def test_completeness_and_gaps():
    assert demo_product().completeness == 1.0
    # A low-confidence value counts as a gap, not as a filled field.
    product = demo_product(brand=sourced("Guessed", confidence=0.2))
    assert product.completeness == 0.8
    assert product.gaps == ["brand"]
    # Specs are one slot however many there are — padding must not inflate score.
    many = demo_product(specs=[
        Spec(name=f"s{i}", value="x", source="input", evidence="t", confidence=0.9)
        for i in range(20)
    ])
    assert many.completeness == 1.0


def test_apply_report_only_lowers_confidence():
    product = demo_product()
    report = ValidationReport(
        issues=[Issue(field="brand", severity="unsupported",
                      detail="Evidence restates the claim.", suggested_confidence=0.1)],
        verdict="revise",
    )
    apply_report(product, report)
    assert product.brand.confidence == 0.1
    assert "flagged" in product.brand.evidence
    assert product.name.confidence == 0.9, "untouched fields keep their confidence"

    # A validator cannot launder a bad value by raising its confidence.
    apply_report(product, ValidationReport(
        issues=[Issue(field="brand", severity="unsupported", detail="ok now",
                      suggested_confidence=0.99)],
        verdict="pass"))
    assert product.brand.confidence == 0.1


def test_apply_report_edge_cases():
    product = demo_product()
    # '*' hits every named field; a nonexistent field name is ignored, not fatal.
    apply_report(product, ValidationReport(
        issues=[
            Issue(field="*", severity="unsupported", detail="unaudited", suggested_confidence=0.0),
            Issue(field="nope", severity="unit", detail="ghost", suggested_confidence=0.5),
        ],
        verdict="revise"))
    assert product.name.confidence == 0.0 and product.brand.confidence == 0.0
    assert product.specs[0].confidence == 0.0, "'*' covers specs too"
    assert product.completeness == 0.0, "an unaudited record is fully retracted"


def test_unaudited_report_is_distinguishable():
    """A validator that FOUND problems and one that NEVER RAN both produce a
    non-empty issue list. Conflating them let an API failure be scored as a
    successful detection in the first probe run — hence an explicit marker."""
    from src.enrich import UNAUDITED_MARKER, is_unaudited

    never_ran = ValidationReport(
        issues=[Issue(field="*", severity="unsupported",
                      detail=f"{UNAUDITED_MARKER} (RuntimeError); record is unaudited.",
                      suggested_confidence=0.0)],
        verdict="revise")
    real_finding = ValidationReport(
        issues=[Issue(field="brand", severity="contradiction",
                      detail="Input says Omron, record says Siemens.",
                      suggested_confidence=0.0)],
        verdict="revise")

    assert is_unaudited(never_ran)
    assert not is_unaudited(real_finding)
    assert not is_unaudited(ValidationReport(issues=[], verdict="pass"))
    # Both still retract confidence — failing safe is independent of the label.
    for report in (never_ran, real_finding):
        assert report.issues[0].suggested_confidence == 0.0


def test_golden_set_is_structurally_valid():
    """The golden set defines what 'correct' means, so a typo in it silently
    skews every accuracy number rather than raising. Checks the parts that would
    fail quietly: unknown core field names, duplicate SKUs, contradictory
    expectations, and `values` targets that were never asked to be grounded."""
    import json

    cases = json.loads(Path("data/golden.json").read_text(encoding="utf-8"))
    core = {"name", "brand", "category", "description"}
    known_keys = {"grounded", "absent_core", "values", "required_specs",
                  "forbidden_specs", "deferred_specs", "max_specs"}

    skus = [c["sku"] for c in cases]
    assert len(skus) == len(set(skus)), f"duplicate SKUs: {[s for s in skus if skus.count(s) > 1]}"

    for case in cases:
        sku, expect = case["sku"], case["expect"]
        assert case.get("note"), f"{sku}: every record needs a note saying what it tests"
        assert set(expect) <= known_keys, f"{sku}: unknown expectation key {set(expect) - known_keys}"

        grounded = set(expect.get("grounded", []))
        absent = set(expect.get("absent_core", []))
        assert grounded <= core, f"{sku}: 'grounded' has non-core field {grounded - core}"
        assert absent <= core, f"{sku}: 'absent_core' has non-core field {absent - core}"
        # A field cannot be required to be both filled and refused.
        assert not (grounded & absent), f"{sku}: {grounded & absent} both grounded and absent"

        for field in expect.get("values", {}):
            assert field in core, f"{sku}: 'values' targets non-core field {field!r}"
            assert field not in absent, f"{sku}: {field!r} expected absent but has an expected value"

        # The two hallucination traps are opposites and must not be confused:
        #   forbidden_specs — attribute NEVER mentioned. Must be absent from the input.
        #   deferred_specs  — attribute NAMED but explicitly unvalued ("Torque: see
        #                     datasheet"). Must be PRESENT in the input, or it is
        #                     really a forbidden spec and belongs in the other list.
        # Getting these backwards penalises the model for reading correctly, or
        # silently tests nothing.
        haystack = " ".join([*case.get("attributes", {}), *case.get("attributes", {}).values(),
                             case.get("text", "")]).lower()
        for banned in expect.get("forbidden_specs", []):
            assert banned.lower() not in haystack, (
                f"{sku}: forbidden spec {banned!r} appears in the input — the model "
                f"would be penalised for reading it correctly. Move it to deferred_specs.")
        for deferred in expect.get("deferred_specs", []):
            assert deferred.lower() in haystack, (
                f"{sku}: deferred spec {deferred!r} does NOT appear in the input, so "
                f"nothing defers it. Move it to forbidden_specs.")

        # The cardinal rule: an expectation must be checkable by reading the input
        # alone. If an expected value isn't in the record, verifying it needs
        # outside product knowledge — and the golden set stops being ground truth.
        for field, expected_value in expect.get("values", {}).items():
            assert expected_value.lower() in haystack, (
                f"{sku}: expected {field}={expected_value!r} but that string is not in "
                f"the input — this expectation cannot be verified from the record alone")
        for required in expect.get("required_specs", []):
            assert required.lower() in haystack, (
                f"{sku}: required spec {required!r} is not stated in the input, so "
                f"demanding it would reward guessing")


def test_ui_escapes_model_output():
    """Everything the UI renders — values, evidence, spec names — is LLM output.
    A product description containing a script tag must render as text, not run."""
    from src.app import render_field

    hostile = "<script>alert('xss')</script>"
    field = Sourced(value=hostile, source="input", evidence=hostile, confidence=0.9)
    html = render_field(hostile, field)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_store_roundtrip_and_idempotency():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        conn = store.connect(db)
        product, report = demo_product(), ValidationReport(issues=[], verdict="pass")

        assert not store.is_done(conn, "TEST-1")
        store.save(conn, product, report)
        assert store.is_done(conn, "TEST-1")

        loaded = store.load(conn, "TEST-1")
        assert loaded is not None and loaded.brand.value == "Acme"
        assert loaded.brand.evidence == "test", "provenance survives the round trip"

        # Re-saving updates in place rather than duplicating the catalog row...
        store.save(conn, product, report)
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        # ...while the audit trail keeps every attempt.
        assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 4

        assert store.summary(conn)["products"] == 1
        conn.close()  # Windows will not delete the temp dir while the file is open


def test_schemas_survive_json_schema_conversion():
    """Every provider is handed `model_json_schema()`. If a model change makes
    that unrepresentable (a recursive type, an unsupported constraint), it must
    fail here rather than on the first live call mid-demo."""
    for schema in (Product, ValidationReport):
        as_json = schema.model_json_schema()
        assert as_json["type"] == "object"
        assert "properties" in as_json
    product_schema = Product.model_json_schema()
    # The scores are @property, so they must never appear in what we ask for.
    requested = set(product_schema["properties"])
    assert not requested & {"completeness", "mean_confidence", "gaps"}
    assert {"sku", "name", "brand", "category", "description", "specs"} <= requested


def test_provider_dispatch():
    original = llm.PROVIDER
    try:
        llm.PROVIDER = "not-a-provider"
        for call in (llm.check_ready, lambda: llm.structured_call("s", "u", Product)):
            try:
                call()
                raise AssertionError("unknown provider should not be silently accepted")
            except llm.ProviderError:
                pass
    finally:
        llm.PROVIDER = original
    assert llm.workers_default() >= 1
    assert llm.describe()


def test_schema_dialects_differ_per_provider():
    """BUG-001. Anthropic requires `additionalProperties: false`; Gemini returns
    a 400 if it is present. The shared pydantic models must therefore emit it
    NOWHERE, and the Anthropic branch must add it back. Putting `extra: forbid`
    on a model would silently break every Gemini call."""
    for schema in (Product, ValidationReport):
        raw = schema.model_json_schema()
        assert "additionalProperties" not in str(raw), (
            f"{schema.__name__} emits additionalProperties — Gemini will 400. "
            "Do not add `extra: \"forbid\"` to the shared models."
        )
        closed = llm._close_objects(raw)
        assert closed["additionalProperties"] is False
        # Nested objects need it too, not just the root.
        assert str(closed).count("'additionalProperties': False") >= 2
        # Closing must not mutate the original.
        assert "additionalProperties" not in str(raw)


def test_permanent_errors_are_not_retried():
    """BUG-001 cost 4 retries x 5 records on an error that could never succeed,
    burying the real message under retry noise."""
    assert llm._is_permanent(Exception("400 INVALID_ARGUMENT Unknown name"))
    assert llm._is_permanent(Exception("401 unauthenticated"))
    assert llm._is_permanent(Exception("PermissionDenied: 403"))
    # 429 reads like a 4xx but is the one that must be retried.
    assert not llm._is_permanent(Exception("429 RESOURCE_EXHAUSTED"))
    assert not llm._is_permanent(Exception("503 service unavailable"))
    assert not llm._is_permanent(ConnectionError("connection reset"))


def test_retry_honours_server_stated_delay():
    """The provider tells you how long to wait. Ignoring it and using a blind
    4/8/16 backoff totals 28s — against a quota window that reopens at 42s,
    every retry failed and recoverable errors were reported as dead records."""
    # Real Gemini quota message, verbatim.
    quota = Exception("429 RESOURCE_EXHAUSTED ... Please retry in 41.812865242s.")
    assert abs(llm.retry_delay_from(quota, fallback=8) - 42.81) < 0.1
    # Alternate spellings providers actually use.
    assert abs(llm.retry_delay_from(Exception("retryDelay: '17s'"), 8) - 18.0) < 0.1
    assert abs(llm.retry_delay_from(Exception("Retry-After: 30"), 8) - 31.0) < 0.1
    # No stated delay -> caller's backoff, and never an unbounded sleep.
    assert llm.retry_delay_from(Exception("boom"), fallback=8) == 8
    assert llm.retry_delay_from(Exception("retry in 9999s"), 8) == llm.MAX_BACKOFF


def test_rate_limit_detection():
    """Free tiers throttle per minute; missing a 429 turns a retryable pause
    into a failed record, so this matcher is load-bearing."""
    assert llm._is_rate_limit(Exception("429 RESOURCE_EXHAUSTED"))
    assert llm._is_rate_limit(Exception("Quota exceeded for requests"))
    assert llm._is_rate_limit(Exception("Error code: 529 overloaded_error"))
    assert not llm._is_rate_limit(Exception("invalid api key"))
    assert not llm._is_rate_limit(ValueError("bad json"))


def test_failed_record_is_absent_but_audited():
    with tempfile.TemporaryDirectory() as tmp:
        conn = store.connect(Path(tmp) / "t.db")
        store.save_error(conn, "BAD-1", "RuntimeError: boom")
        assert not store.is_done(conn, "BAD-1"), "a failed record must not enter the catalog"
        assert store.summary(conn)["failed_records"] == 1
        conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
