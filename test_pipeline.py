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
