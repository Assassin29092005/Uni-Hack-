"""Runnable checks for the parts that break silently.

    python test_pipeline.py

No framework, no fixtures — asserts and a main block. Nothing here calls the
Anthropic API: these cover the deterministic logic where a regression produces
wrong data rather than an error, which is exactly the kind that survives to the
demo. Prompt quality is checked against the golden set, not here.
"""

import json
import tempfile
from pathlib import Path

from src import checks, llm, store
from src.enrich import apply_report
from src.models import CONFIDENCE_FLOOR, Issue, Product, RawRecord, Sourced, Spec, ValidationReport
from src.normalize import normalize_key, normalize_record, normalize_specs, normalize_value
from src.pipeline import ingest_csv


def sourced(value, confidence=0.9, unit=None) -> Sourced:
    return Sourced(value=value, unit=unit, source="input", evidence="test", confidence=confidence)


def spec(name, value, unit=None, confidence=0.9, source="input", evidence="test") -> Spec:
    return Spec(name=name, value=value, unit=unit, source=source,
                evidence=evidence, confidence=confidence)


# The record the README's flagship example comes from. Used as the control for
# the deterministic rules: everything the model legitimately derives from it must
# pass through them silently.
CYLINDER = RawRecord(
    sku="HDX-4025-200",
    attributes={"brand": "Bosch Rexroth", "dimensions": "25 MM x 200MM", "weight": "3.4 kg"},
    text="Hydraulic cylinder assembly. Bore 25mm. Max operating pressure 210 bar.",
)


def cylinder_product(**overrides) -> Product:
    base = dict(
        sku="HDX-4025-200",
        name=sourced("Bosch Rexroth HDX-4025-200 Hydraulic Cylinder"),
        brand=sourced("Bosch Rexroth"),
        category=sourced("Hydraulic Cylinder"),
        description=sourced("A hydraulic cylinder assembly with a 25 mm bore."),
        specs=[spec("bore", "25", "mm")],
    )
    return Product(**{**base, **overrides})


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
    # "voltage rating", not our old invented "operating voltage" — the canon is
    # Unilog's label, so a correct value lands in the column they grade.
    assert record.attributes == {"brand": "Bosch", "voltage rating": "24 V DC"}
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


def test_brand_must_appear_in_the_record():
    """BUG-009. The first BUG-004 fix explicitly permitted naming a manufacturer
    from a part number, so `1756-IF16-XT` still produced 'Allen-Bradley'. House
    brands copy famous numbering schemes constantly; the SKU resembling one is
    evidence of nothing."""
    from src.models import RawRecord, Sourced

    record = RawRecord(sku="MISL-1756-IF16-XT", attributes={},
                       text="16-channel analog input module, 24 V DC.")

    invented = demo_product(brand=Sourced(
        value="Allen-Bradley", source="inference", confidence=0.8,
        evidence="The 1756-IF16 pattern matches the ControlLogix scheme."))
    assert checks.brand_not_in_record(record, invented), "decoded brand must be flagged"

    # A brand the record actually contains is grounded, even loosely quoted.
    stated = RawRecord(sku="X", attributes={}, text="3M 775L Stikit Film P120")
    ok = demo_product(brand=Sourced(value="3M", source="inference", confidence=0.9,
                                    evidence="Named in the description."))
    assert not checks.brand_not_in_record(stated, ok), "a brand in the text is grounded"

    # Column-sourced brands are somebody else's rule to police.
    from_column = demo_product(brand=Sourced(
        value="Acme", source="input", confidence=1.0, evidence="Mfr: Acme"))
    assert not checks.brand_not_in_record(record, from_column)


def test_enrich_prompt_forbids_identifier_decoding():
    """BUG-008. BUG-004's fix lived only in prompt prose, so a rewrite of an
    adjacent block silently reverted it and all 32 tests stayed green — its
    evidence was a golden-set score, not an assertion. A prompt is code; this is
    the assertion it was missing."""
    from src.enrich import ENRICH_SYSTEM

    assert "identifier, not a specification" in ENRICH_SYSTEM, (
        "BUG-004's prompt fix is missing from ENRICH_SYSTEM — see BUG-008"
    )
    # The original wording actively invited the failure. It must not come back.
    assert "model number decoding" not in ENRICH_SYSTEM, (
        "the confidence ladder again endorses decoding part numbers (BUG-004 root cause)"
    )


def test_delivery_headers_match_the_supplied_sheet_exactly():
    """The brief says: "Contains the static headers your solution must populate.
    Please do not change or modify the headers." A reordered or renamed column
    could fail the whole submission on format, so the contract is asserted
    against their file directly whenever it is present."""
    import csv as _csv

    from src import delivery

    ours = delivery.headers()
    assert len(ours) == 252, f"expected 252 delivery columns, have {len(ours)}"

    supplied = Path("Unihack_ Expected Output - Delivery Format.csv")
    if supplied.exists():  # absent on a fresh clone; the JSON is the checked-in copy
        with open(supplied, encoding="utf-8-sig", newline="") as fh:
            theirs = next(_csv.reader(fh))
        assert ours == theirs, "delivery headers have drifted from the supplied sheet"


def test_delivery_row_is_complete_and_refuses_ungrounded_values():
    from src import delivery

    product = demo_product(
        brand=sourced("Acme", confidence=0.2),        # below the floor
        category=sourced("Tools > Power Tools > Drills"),
    )
    row = delivery.to_row(product, {"Mfg_Part_Num": "ACME-1", "Part_Desc": "raw text"})

    # Every column present, every time — a short row would misalign the CSV.
    assert list(row) == delivery.headers()

    assert row["Mfg_Part_Num"] == "ACME-1"
    assert row["Part_Desc"] == "raw text", "the distributor's own text passes through"
    # A low-confidence brand is a blank cell, never a hedged guess.
    assert row["BRAND_NAME"] == ""
    # Spec -> LABEL/VALUE/UOM triplet, units kept out of the value. Labels are
    # stored lowercase for case-insensitive dedup and Title Cased on the way out
    # to match Unilog house style.
    assert row["ATTRIBUTE_LABEL 1"] == "Thread" and row["ATTRIBUTE_VALUE 1"] == "M8"


def test_dept_class_fine_pass_through_and_are_never_derived():
    """Their sample pairs Dept/Class/Fine "Appliances / Large Appliances /
    Dishwashers" with Classpath "Appliances & Consumer Electronics>Kitchen
    Appliances>Built-In Dishwashers" — a different, coarser taxonomy that
    travels with the input. The exporter used to split the Classpath into these
    three columns, which overwrites a supplied value with a plausible wrong one
    and emits three columns no provenance row covers."""
    from src import delivery

    product = demo_product(category=sourced("Tools > Power Tools > Drills"))

    supplied = delivery.to_row(product, {
        "Mfg_Part_Num": "ACME-1",
        "Dept": "Appliances", "Class": "Large Appliances", "Fine": "Dishwashers",
    })
    assert supplied["Dept"] == "Appliances", "the distributor's taxonomy must survive"
    assert supplied["Class"] == "Large Appliances"
    assert supplied["Fine"] == "Dishwashers"
    assert supplied["Dept"] != "Tools", "Classpath must not overwrite a supplied Dept"

    # The 6-column input carries no such columns. Blank is the honest answer;
    # inventing them from a differently-worded hierarchy is not.
    absent = delivery.to_row(product, {"Mfg_Part_Num": "ACME-1"})
    assert absent["Dept"] == absent["Class"] == absent["Fine"] == ""
    assert absent["Classpath"], "Classpath itself is still emitted"


def test_classpath_uses_their_separator():
    """We prompt for ' > ' and store that; their sheet writes a bare '>'."""
    from src import delivery

    row = delivery.to_row(
        demo_product(category=sourced("Appliances & Consumer Electronics > "
                                      "Kitchen Appliances > Built-In Dishwashers")),
        {"Mfg_Part_Num": "ACME-1"},
    )
    assert row["Classpath"] == ("Appliances & Consumer Electronics>"
                                "Kitchen Appliances>Built-In Dishwashers")


def test_every_generated_column_carries_provenance():
    """The one non-negotiable rule, asserted structurally. The sidecar once
    covered five of the ten columns being shipped: the four description
    variants and MANUFACTURER_NAME went out with no recorded evidence."""
    from src import delivery

    product = demo_product(
        invoice_desc=sourced("ACME DRILL M8"),
        mobile_desc=sourced("Acme, Drill, ACME-1"),
        short_desc=sourced("Acme ACME-1 Drill"),
        retail_desc=sourced("Cordless Drill, M8 Thread"),
    )
    raw = {"Mfg_Part_Num": "ACME-1", "Part_Desc": "raw text", "Dept": "Tools"}

    row = delivery.to_row(product, raw)
    explained = {p["column"] for p in delivery.provenance_rows(product, raw)}

    for column, _ in delivery.generated_fields(product):
        assert row[column], f"{column} should be populated by this fixture"
        assert column in explained, f"{column} ships with no provenance row"

    # Passthrough is the deliberate exception: we owe evidence for what we
    # assert, not for what the distributor handed us.
    assert "Part_Desc" not in explained and "Dept" not in explained

    # The declared column list and the field mapping must stay in step. A
    # column in the tuple with no source already raises KeyError; this catches
    # the reverse, where a mapped field is silently never emitted or scored.
    assert tuple(c for c, _ in delivery.generated_fields(product)) == delivery.GENERATED_COLUMNS


def test_document_provenance_requires_an_actual_document():
    """Auditing the stored catalog found 12 fields claiming `source: document`
    with evidence like "Free text: 'PDSH4816AF Dishwasher SS'". No document was
    ever retrieved — the model used `document` to mean the description column.

    Harmless until `sources.py` made real documents possible; now the label is
    the only thing separating a genuine datasheet citation from the model
    renaming the distributor's blurb. A provenance system whose strongest label
    can be self-awarded is not a provenance system."""
    record = RawRecord(sku="P-1", attributes={"description": "Dishwasher SS"})
    assert not record.document, "precondition: nothing was retrieved"

    # Value IS in the record: a mislabel of real data. Flagged, not retracted —
    # the value is grounded and only its paperwork is wrong.
    mislabelled = demo_product(name=Sourced(
        value="Dishwasher", source="document", confidence=0.95,
        evidence="Free text: 'PDSH4816AF Dishwasher SS'"))
    found = checks.undocumented_provenance(record, mislabelled)
    assert [i.field for i in found] == ["name"]
    assert "mislabel" in found[0].detail
    assert found[0].suggested_confidence == 0.95, "a correct value is not retracted"

    # Value is NOT in the record: an invented citation. This is the worst case
    # in the taxonomy — a fabrication wearing the most authoritative label — so
    # it is retracted outright.
    invented = demo_product(specs=[Spec(
        name="sound level", value="47", unit="dBA", source="document",
        confidence=0.9, evidence="Datasheet states 47 dBA sound level.")])
    found = checks.undocumented_provenance(record, invented)
    assert found and found[0].suggested_confidence == 0.0
    assert "invented" in found[0].detail

    # With a document attached, citing it is simply legitimate.
    record.document = "Sound Level: 47 dBA"
    record.document_source = "https://www.frigidaire.com/x"
    assert not checks.undocumented_provenance(record, invented)
    assert not checks.undocumented_provenance(record, mislabelled)

    # And the rule is wired into the deterministic pass, not just importable.
    assert any(i.field == "name"
               for i in checks.run_checks(
                   RawRecord(sku="P-1", attributes={"description": "Dishwasher SS"}),
                   mislabelled))


def test_sourcing_policy_excludes_distributors_and_marketplaces():
    """The guidelines require the manufacturer's own site or documentation and
    exclude marketplaces and distributor sites explicitly. A spec lifted from a
    marketplace listing carries a manufacturer's authority it never had, so the
    rule is enforced in code before a fetch is attempted."""
    from src.sources import classify

    for denied in ("https://www.amazon.com/dp/B01", "https://www.grainger.com/product/1",
                   "https://www.homedepot.com/p/2", "https://octopart.com/search"):
        allowed, reason = classify(denied)
        assert not allowed, f"{denied} must be refused"
        assert "distributor/marketplace" in reason

    for allowed_url in ("https://www.frigidaire.com/en/p/PDSH4816AF",
                        "https://www.whirlpool.com/manuals/x.html",
                        "https://www.3m.com/product/775L"):
        ok, _ = classify(allowed_url)
        assert ok, f"{allowed_url} is a manufacturer source and must be allowed"

    # Label matching, not substring: a brand whose name contains a denied word
    # is not that company.
    assert classify("https://www.myamazonbrand.com/p/1")[0] is True
    # Junk in, refusal out — never a silent allow.
    for junk in ("", "not a url", "ftp://files.example.com/x", "mailto:a@b.c"):
        assert classify(junk)[0] is False


def test_document_text_cannot_masquerade_as_the_input():
    """Attaching manufacturer documentation puts text in the prompt that the
    distributor's record never contained. `unsupported_input_claims` asks "did
    THE RECORD say this?" — if its haystack included the document, a spec lifted
    off a datasheet could claim `source: input` and pass, inverting the one
    thing that rule checks."""
    record = RawRecord(sku="P-1", attributes={"description": "Dishwasher SS"})
    record.document = "Sound Level: 47 dBA. Wash cycles: 5. Voltage: 120 V."
    record.document_source = "https://www.frigidaire.com/x"

    assert "47 dBA" in record.as_prompt_block(), "the model must see the document"
    assert "47 dBA" not in record.as_input_block(), "the record did not say it"
    assert 'source="https://www.frigidaire.com/x"' in record.as_prompt_block()

    lying = demo_product(specs=[
        Spec(name="sound level", value="47", unit="dBA", source="input",
             evidence="stated in the record", confidence=0.9)])
    issues = checks.unsupported_input_claims(record, lying)
    assert any(i.field == "spec:sound level" for i in issues), (
        "a document-sourced value claiming source 'input' must still be caught"
    )

    # The same value, honestly labelled, is not an issue — the check polices
    # the provenance claim, not the value.
    honest = demo_product(specs=[
        Spec(name="sound level", value="47", unit="dBA", source="document",
             evidence="document: 'Sound Level: 47 dBA'", confidence=0.9)])
    assert not any(i.field == "spec:sound level"
                   for i in checks.unsupported_input_claims(record, honest))


def test_strip_html_drops_script_and_style_content():
    """A model shown minified JavaScript will find 'specifications' in it."""
    from src.sources import strip_html

    html = ("<html><head><style>.a{color:red}</style>"
            "<script>var specs={voltage:'999V'};</script></head>"
            "<body><h1>Model X</h1><p>Sound&nbsp;Level: 47 dBA</p></body></html>")
    text = strip_html(html)
    assert "999V" not in text and "color:red" not in text
    assert "Model X" in text and "Sound Level: 47 dBA" in text


def test_golden_expectations_survive_an_attribute_rename():
    """The golden expectations are hand-written in the record's own wording;
    the specs they score have been through `normalize_specs`. The day
    "current rating" started canonicalising to "amperage rating", the substring
    match stopped firing — and a hallucinated current rating scored as a clean
    abstention. An instrument that a rename can silently disarm is BUG-003
    wearing a different hat."""
    from src.golden import score

    invented = demo_product(specs=[
        Spec(name=normalize_key("Current Rating"), value="5", unit="A",
             source="inference", evidence="typical for this part", confidence=0.9),
    ])
    assert invented.specs[0].name == "amperage rating", "precondition: the rename happened"

    # The expectation is still written the pre-rename way, as a human would.
    result = score(invented, {"forbidden_specs": ["current rating"]})
    assert result["hallucinations"], (
        "a forbidden spec went undetected because the canonical name changed"
    )
    assert result["abstained_ok"] == 0

    # And the honest case still scores as an abstention rather than a false hit.
    clean = demo_product(specs=[])
    assert not score(clean, {"forbidden_specs": ["current rating"]})["hallucinations"]
    assert score(clean, {"forbidden_specs": ["current rating"]})["abstained_ok"] == 1


def test_dedup_holds_back_sku_collisions_instead_of_overwriting():
    """The real failure in their 1000-row sheet: AVM6EV appears twice
    describing two different products. `store.save` upserts on SKU, so both
    were enriched — paying twice — and the second silently overwrote the first.
    A pipeline that loses a product without saying so is worse than one that
    refuses it."""
    from src import dedup

    records = [
        normalize_record("AVM6EV", {"Part_Desc": "AVM6 EV Mini Snip Red"}),
        normalize_record("AVM6EV", {"Part_Desc": "AVM7 EV Mini Snip Green"}),
        normalize_record("SAFE-1", {"Part_Desc": "Sanding Belt"}),
    ]
    report = dedup.analyse(records)

    assert [r.sku for r in report.unique] == ["SAFE-1"], "a collision must not be enriched"
    assert set(report.collisions) == {"AVM6EV"}
    assert len(report.collisions["AVM6EV"]) == 2
    assert "AVM6EV" in report.flagged_skus
    assert any("COLLISION" in line for line in dedup.describe(report))


def test_dedup_collapses_only_provably_identical_rows():
    from src import dedup

    same = [normalize_record("D-1", {"Part_Desc": "Box Cover"}),
            normalize_record("D-1", {"Part_Desc": "Box Cover"})]
    report = dedup.analyse(same)
    assert len(report.unique) == 1 and report.collapsed == 1
    assert not report.collisions, "identical rows are not a collision"


def test_dedup_flags_shared_descriptions_without_merging_them():
    """Three different part numbers, one description — their box covers. These
    are distinct parts whose input text is too sparse to tell apart. Merging
    would delete two products; ignoring would ship three identical pages."""
    from src import dedup

    records = [normalize_record(sku, {"Part_Desc": "4x4 1G Box Cover"})
               for sku in ("52C3-5/8-UPC", "52C14-5/8-UPC", "52C3-UPC")]
    report = dedup.analyse(records)

    assert len(report.unique) == 3, "distinct parts must all still be enriched"
    assert not report.collisions
    assert len(report.shared_content) == 1
    assert report.flagged_skus == {"52C3-5/8-UPC", "52C14-5/8-UPC", "52C3-UPC"}

    # Case and punctuation are not a difference in product.
    mixed = [normalize_record("A-1", {"Part_Desc": "4x4 1G Box Cover"}),
             normalize_record("A-2", {"Part_Desc": "4X4  1g  box cover!"})]
    assert len(dedup.analyse(mixed).shared_content) == 1

    # Genuinely different products must not be grouped.
    distinct = [normalize_record("B-1", {"Part_Desc": "Sanding Belt"}),
                normalize_record("B-2", {"Part_Desc": "Cut-Off Disc"})]
    assert not dedup.analyse(distinct).shared_content


def test_dedup_finds_exactly_the_known_cases_in_the_shipped_sheet():
    """Against the real 1000-row input, not a fixture. Guards the claim the
    README makes: two collisions, and both are reported rather than merged."""
    from src import dedup

    sheet = Path("Unihack_ Sample Dataset - Input.csv")
    if not sheet.exists():
        return  # the full sheet is not in every clone

    report = dedup.analyse(ingest_csv(sheet))
    assert set(report.collisions) == {"AVM6EV"}, "the known part-number collision"
    assert report.collapsed == 0, "no byte-identical rows in this sheet"
    groups = {tuple(sorted(skus)) for skus in report.shared_content.values()}
    assert ("52C14-5/8-UPC", "52C3-5/8-UPC", "52C3-UPC") in groups
    # 1000 rows minus BOTH sides of the one collision — neither is enriched,
    # because the ambiguity is about which product owns the part number.
    assert len(report.unique) == 998


def test_ground_truth_scorer_control():
    """The instrument's control: their own rows, scored against themselves,
    must come back a clean sweep. A comparator that cannot score a known-good
    row as correct tells you nothing about a row it grades badly — the same
    reason `probe.py` carries a clean control record.

    This is the cheap half of `python -m src.truth --control`, run every time
    the suite runs rather than when somebody remembers the flag."""
    from src import truth

    if not truth.GROUND_TRUTH.exists():
        return  # their sheet is not in every clone; skip rather than fail

    result = truth.score(None, truth.GROUND_TRUTH, control=True)
    assert result["records"] == result["ground_truth_records"] > 0
    for verdict in ("differs", "missing", "extra"):
        assert result["tally"][verdict] == 0, (
            f"comparator reports {result['tally'][verdict]} {verdict} on a row "
            f"scored against itself; every accuracy figure it prints is unsafe"
        )
    assert result["tally"]["match"] > 0, "a control that compares nothing is not a control"


def test_scorer_refuses_an_empty_comparison():
    """"100% accurate (0 records)" is the failure mode this guards. An
    instrument with no overlap must refuse, not average over an empty set."""
    from src import truth

    if not truth.GROUND_TRUTH.exists():
        return
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.db"
        store.connect(empty).close()
        try:
            truth.score(empty, truth.GROUND_TRUTH)
        except SystemExit as exc:
            assert "nothing to score" in str(exc)
        else:
            raise AssertionError("scored an empty catalog instead of refusing")


def test_scorer_separates_wrong_values_from_honest_blanks():
    """A blank where the truth has a value is a gap; a different value is an
    error that reaches a buyer. Collapsing them into one accuracy number hides
    the only distinction this project is built around."""
    from src import truth

    theirs = {"BRAND_NAME": "FRIGIDAIRE®", "Product Name": "Dishwasher",
              "Classpath": "A>B>C", "LONG_DESC1": "Long copy."}
    ours = {"BRAND_NAME": "FRIGIDAIRE",     # symbol missing -> differs, near
            "Product Name": "Dryer",        # wrong -> differs
            "Classpath": "",                # abstained -> missing
            "LONG_DESC1": "Long copy.",     # -> match
            "MOBILE_DESC": "Acme, Dryer"}   # they left it blank -> extra
    columns = ["BRAND_NAME", "Product Name", "Classpath", "LONG_DESC1", "MOBILE_DESC"]

    by_column = {v["column"]: v for v in truth.compare_row(ours, theirs, columns)}
    assert by_column["LONG_DESC1"]["verdict"] == "match"
    assert by_column["Product Name"]["verdict"] == "differs"
    assert by_column["Classpath"]["verdict"] == "missing", "a blank is not a wrong answer"
    assert by_column["MOBILE_DESC"]["verdict"] == "extra"

    # A missing ® is still wrong — the guide requires the approved name "symbols
    # and all" — but it is a different repair from naming the wrong company, so
    # it is labelled rather than forgiven.
    assert by_column["BRAND_NAME"]["verdict"] == "differs"
    assert by_column["BRAND_NAME"]["near"] is True
    assert by_column["Product Name"]["near"] is False


def test_scorer_compares_attributes_by_label_not_by_slot():
    """Their slot 3 and our slot 3 holding different attributes is an ordering
    difference, not two wrong values."""
    from src import truth

    theirs = {"ATTRIBUTE_LABEL 1": "Voltage Rating", "ATTRIBUTE_VALUE 1": "120",
              "ATTRIBUTE_UOM 1": "V",
              "ATTRIBUTE_LABEL 2": "Sound Level", "ATTRIBUTE_VALUE 2": "47",
              "ATTRIBUTE_UOM 2": "dBA",
              # A label their category defines but nobody filled in. It asserts
              # nothing, so it must not be scored as an expectation.
              "ATTRIBUTE_LABEL 3": "Color", "ATTRIBUTE_VALUE 3": "", "ATTRIBUTE_UOM 3": ""}
    ours = {"ATTRIBUTE_LABEL 1": "Sound Level", "ATTRIBUTE_VALUE 1": "47",
            "ATTRIBUTE_UOM 1": "dBA",
            "ATTRIBUTE_LABEL 2": "Voltage Rating", "ATTRIBUTE_VALUE 2": "240",
            "ATTRIBUTE_UOM 2": "V"}

    by_column = {v["column"]: v for v in truth.compare_row(ours, theirs, [])}
    assert by_column["ATTRIBUTE[sound level]"]["verdict"] == "match", "order is not an error"
    assert by_column["ATTRIBUTE[voltage rating]"]["verdict"] == "differs"
    assert "ATTRIBUTE[color]" not in by_column, "an empty label is not an expectation"


def test_unit_split_only_fires_on_known_units():
    """`digits + letters` is not enough to call something a quantity. The
    abrasive series "775L" was being rewritten to "775 L", and a bearing code
    like "6205C3" would split the same way — an unrecognised suffix is far more
    likely to be part of a product code than a unit we forgot to list."""
    assert normalize_value("775L") == "775L", "series code must survive intact"
    assert normalize_value("6205C3") == "6205C3"
    assert normalize_value("P120") == "P120"
    # Real units still split, with a space, as Unilog requires.
    assert normalize_value("24VDC") == "24 V DC"
    assert normalize_value("25mm") == "25 mm"


def test_decimal_inches_become_fractions():
    """"Manufacturers publish decimals; trade buyers search fractions." Their
    Decimal_Fraction table is every exact 64th, so it is generated rather than
    transcribed — same numbers, no copying errors, no file we were not sent."""
    from src.normalize import to_fraction

    assert to_fraction("50.25") == "50-1/4"
    assert to_fraction("0.5") == "1/2", "no leading zero — '1/2', never '0-1/2'"
    assert to_fraction("2.75") == "2-3/4"
    assert to_fraction("0.015625") == "1/64", "the first row of their table"
    assert to_fraction("0.984375") == "63/64", "the last row of their table"
    assert to_fraction("12") == "12", "a whole number has no fraction part"

    # The refusals matter more than the conversions. A third of an inch is not
    # a 64th; rounding it to 21/64 would invent precision the source never had.
    assert to_fraction("0.33") is None
    assert to_fraction("abc") is None
    assert to_fraction("-1") is None

    # End to end, in both shapes: inline with the unit, and as the delivery
    # format's separate VALUE/UOM pair.
    assert normalize_value("50.25 in") == "50-1/4 in"
    assert normalize_value("0.5 inch") == "1/2 in"
    # Feet are left decimal: their table is inches, and extending it is ours to
    # invent. But feet must still normalise as a unit — they used to fall
    # through the unknown-unit branch entirely.
    assert normalize_value("16 feet") == "16 ft"
    assert normalize_value("1.5 ft") == "1.5 ft"
    # A range stays a range. "1/2-3/4 in" is ambiguous between a range and a
    # fraction and guessing is worse than leaving the decimals visible.
    assert normalize_value("10-30 V") == "10-30 V"

    spec = normalize_specs([Spec(name="Depth With Door Open", value="50.25", unit="in",
                                 source="input", evidence="e", confidence=0.9)])[0]
    assert (spec.value, spec.unit) == ("50-1/4", "in"), "their VALUE/UOM shape"


def test_attribute_labels_normalise_to_unilogs_vocabulary():
    """Canonical form is THEIR label, not one we invented. `truth.py` caught us
    emitting "Finish Material" on one record and "Material" on the other for
    the same value, missing on both."""
    assert normalize_key("Finish Material") == "material"
    assert normalize_key("Material Construction") == "material"
    assert normalize_key("Colour") == "color"
    # Our old canon was "operating voltage" — defensible, and unmatchable
    # against a sheet that says "Voltage Rating".
    assert normalize_key("Voltage") == "voltage rating"
    assert normalize_key("Operating Voltage") == "voltage rating"
    assert normalize_key("Current Rating") == "amperage rating"


def test_unit_check_still_fires_after_the_rename():
    """`checks.QUANTITY_UNITS` matched current specs on the substring
    "current". Renaming the canonical attribute to Unilog's "amperage rating"
    removed that substring, and a rule that silently matches nothing is
    indistinguishable from a rule that finds no problems."""
    product = demo_product(specs=[
        Spec(name=normalize_key("Current Rating"), value="15", unit="mm",
             source="input", evidence="e", confidence=0.9),
    ])
    issues = checks.unit_problems(product)
    assert any(i.severity == "unit" for i in issues), (
        "a current rated in mm must still be caught after the rename"
    )


def test_attribute_labels_use_unilog_title_case():
    from src.delivery import _title_case

    assert _title_case("grit rating") == "Grit Rating"
    assert _title_case("number of wash cycles") == "Number of Wash Cycles"
    assert _title_case("ip rating") == "IP Rating", "initialisms stay upper"
    assert _title_case("od") == "OD"


def test_placeholders_are_not_data():
    """The brief: "-- Unbranded --", "-- No Unilog Brand --" and "-- No DIB
    Brand --" mean the field is empty. Left in, the model describes a product
    made by a manufacturer called Unbranded."""
    from src.normalize import is_placeholder

    for sentinel in ("-- Unbranded --", "--UNBRANDED--", "-- No Unilog Brand --",
                     "-- No DIB Brand --", "N/A", "TBD", "-", "  ", "none"):
        assert is_placeholder(sentinel), f"{sentinel!r} should read as empty"
    for real in ("SKF", "Bosch Rexroth", "3M", "Unbranded Tools Ltd"):
        assert not is_placeholder(real), f"{real!r} is a real value"

    record = normalize_record("P-1", {"E1_Brand": "-- Unbranded --", "Mfr": "SKF"})
    assert record.attributes.get("brand") == "SKF", "sentinel dropped, real brand kept"


def test_unilog_input_schema_ingests():
    """The shipped input is 6 columns whose names none of our original aliases
    covered — `Mfg_Part_Num` did not resolve to a SKU, so ingest refused the
    file outright."""
    from src.normalize import normalize_key

    assert normalize_key("Mfg_Part_Num") == "sku"
    assert normalize_key("Part_Desc") == "description"
    # Part_Manuf is the DEALER. The sample delivery row pairs it with a
    # different MANUFACTURER_NAME, so mapping it to brand would feed the model
    # a confident wrong answer.
    assert normalize_key("Part_Manuf") == "supplier"
    assert normalize_key("Part_Manuf") != "brand"


def test_delivery_format_checks_are_separate_from_content_checks():
    """Format compliance must not flow through `apply_report`: downgrading a
    well-grounded category because its Classpath has two levels would move the
    grounding and abstention numbers the golden baseline is measured on."""
    product = demo_product(category=sourced("Tools"))  # 1 level, not 3

    assert any("Classpath" in i.detail for i in checks.delivery_checks(product))
    assert not any("Classpath" in i.detail for i in checks.run_checks(CYLINDER, product)), (
        "a formatting rule leaked into the content-validation path"
    )


def test_invoice_desc_character_rules():
    from src.models import Sourced

    too_long = demo_product(invoice_desc=Sourced(
        value="DISHWASHER WITH A GREATLY OVERLONG MARKETING SENTENCE ATTACHED",
        source="inference", evidence="t", confidence=0.9))
    assert any("40" in i.detail for i in checks.delivery_checks(too_long))

    lowercase = demo_product(invoice_desc=Sourced(
        value="dishwasher leg 5 sst", source="inference", evidence="t", confidence=0.9))
    assert any("ALL CAPS" in i.detail for i in checks.delivery_checks(lowercase))

    fine = demo_product(invoice_desc=Sourced(
        value="DISHWASHER LEG 5 SST 120V 15A", source="inference", evidence="t", confidence=0.9))
    assert not any("INVOICE_DESC" in i.field for i in checks.delivery_checks(fine))


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


def test_export_seed_round_trip_keeps_the_input_row():
    """`export -> seed` is the no-API-key demo path, and it used to drop `raw`.
    The delivery export then blanked Part_Desc, the three brand columns,
    Part_Manuf and Dept/Class/Fine — data we were *given*, lost on the exact
    path a machine with no key runs."""
    from src import delivery

    raw = {"Mfg_Part_Num": "TEST-1", "Part_Desc": "3/8 CPLG BRS 150#",
           "Part_Manuf": "Freud Inc (2435)", "Dept": "Abrasives"}
    with tempfile.TemporaryDirectory() as tmp:
        source = store.connect(Path(tmp) / "a.db")
        store.save(source, demo_product(), ValidationReport(issues=[], verdict="pass"), raw=raw)
        entries = store.export_all(source)
        source.close()

        assert entries[0]["raw"] == raw, "the input row must survive the export"

        target = store.connect(Path(tmp) / "b.db")
        store.seed(target, entries, source="test")
        row = target.execute("SELECT raw FROM products WHERE sku = 'TEST-1'").fetchone()
        assert json.loads(row["raw"]) == raw, "and must survive the seed"

        # The whole point: the passthrough columns arrive in the deliverable.
        product = store.load(target, "TEST-1")
        exported = delivery.to_row(product, json.loads(row["raw"]))
        assert exported["Part_Desc"] == "3/8 CPLG BRS 150#"
        assert exported["Part_Manuf"] == "Freud Inc (2435)"
        assert exported["Dept"] == "Abrasives"

        # An older dump has no `raw` key at all. Seeding it must not erase a
        # good input row that is already stored, and must not crash.
        legacy = [{k: v for k, v in entries[0].items() if k != "raw"}]
        store.seed(target, legacy, source="legacy")
        kept = target.execute("SELECT raw FROM products WHERE sku = 'TEST-1'").fetchone()
        assert json.loads(kept["raw"]) == raw, "re-seeding an old dump erased the input row"
        target.close()


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


def test_normalize_key_survives_non_latin_scripts():
    """BUG-005. The ASCII-only character class deleted every non-Latin letter,
    so a Chinese attribute name became the empty string and `normalize_record`
    dropped it as a blank key — three of four attributes vanished from our own
    Chinese golden record before the model ever saw them."""
    assert normalize_key("型号") == "型号"
    assert normalize_key("débit") == "débit"
    assert normalize_key("Réf. fournisseur") == "réf fournisseur"
    # Separators must still separate, including the underscore `\W` would keep.
    assert normalize_key("part_no") == "sku"
    assert normalize_key("Part No.") == "sku"
    assert normalize_key("Artikel-Nr.") == "artikel nr"

    record = normalize_record("MULT-CN-IT-7731",
                              {"型号": "PT100-2", "货号": "7731", "包装": "1 pz"})
    assert len(record.attributes) == 3, "non-Latin attribute names must reach the model"
    assert record.attributes["型号"] == "PT100-2"


def test_normalize_specs_canonicalises_and_dedupes():
    """The model names its own output attributes, so the same quantity comes
    back spelled three ways. Input has always been normalized; output was not."""
    out = normalize_specs([
        spec("Operating Voltage", "24", "VDC"),
        spec("voltage", "24", "vdc", confidence=0.95),   # same thing, louder
        spec("WT", "3.4", "KG"),
    ])
    names = [s.name for s in out]
    assert names == ["voltage rating", "weight"], "aliases and casing collapse"
    assert out[0].unit == "V DC" and out[1].unit == "kg", "units canonicalised"
    assert out[0].confidence == 0.95, "the higher-confidence copy of a duplicate wins"


def test_normalize_specs_keeps_contradictions_for_the_validator():
    """Two specs that share a name and disagree are a contradiction, not a
    duplicate. Merging them would resolve the more interesting failure by
    picking a winner on the strength of a self-reported confidence score."""
    out = normalize_specs([spec("Voltage", "24", "V"), spec("voltage", "240", "V")])
    assert len(out) == 2, "conflicting claims both survive normalization"
    assert checks.contradictory_specs(Product(
        sku="X", name=sourced("n"), brand=sourced("b"), category=sourced("c"),
        description=sourced("d"), specs=out)), "and the check reports them"


def test_checks_flag_provenance_the_input_does_not_support():
    """A field claiming `source: input` whose value is not in the input is
    mechanically wrong, whatever the value's merits."""
    fake = cylinder_product(brand=sourced("Allen-Bradley"))
    issues = checks.unsupported_input_claims(CYLINDER, fake)
    assert [i.field for i in issues] == ["brand"]
    assert issues[0].suggested_confidence < CONFIDENCE_FLOOR, "stops being published"

    # CONTROL: a name assembled entirely from tokens the record contains is the
    # normal, correct case and must stay silent, or the rule flags good work.
    assert not checks.unsupported_input_claims(CYLINDER, cylinder_product())


def test_checks_flag_specs_decoded_from_the_part_number():
    """BUG-004: the model recognises a numbering scheme and reports what it
    conventionally means as though the record had said so."""
    decoded = cylinder_product(specs=[
        spec("width", "15", "mm", source="inference",
             evidence="The 6205-2RS designation indicates a 15 mm width."),
    ])
    issues = checks.identifier_decoded_specs(CYLINDER, decoded)
    assert len(issues) == 1 and issues[0].field == "spec:width"
    assert issues[0].suggested_confidence < CONFIDENCE_FLOOR

    # CONTROL: the README's flagship inference. The value IS in the record
    # ("25 MM x 200MM"), so joining it up is legitimate work, not decoding —
    # even though the evidence uses the word "conventionally".
    legitimate = cylinder_product(specs=[
        spec("stroke", "200", "mm", source="inference",
             evidence="Bore is stated as 25mm, so the second dimension "
                      "conventionally represents the stroke."),
    ])
    assert not checks.identifier_decoded_specs(CYLINDER, legitimate)


def test_checks_flag_unit_family_mismatches():
    wrong = cylinder_product(specs=[spec("current rating", "5", "mm")])
    assert [i.severity for i in checks.unit_problems(wrong)] == ["unit"]

    missing = cylinder_product(specs=[spec("weight", "3.4")])
    assert "no unit" in checks.unit_problems(missing)[0].detail

    # CONTROLS: values that are complete without a unit, and correct units.
    # A rule that demands units for "IP67" or counts trains everyone to ignore it.
    fine = cylinder_product(specs=[
        spec("operating voltage", "24", "V DC"),
        spec("number of poles", "4"),
        spec("ingress protection", "IP67"),
        spec("bore", "25", "mm"),
    ])
    assert not checks.unit_problems(fine)


def test_checks_stay_silent_on_the_probe_control_record():
    """The strongest available control: the fixture the LLM validator is
    required to find clean must also survive every deterministic rule. If these
    disagree, one of the two instruments is wrong and the probe's control case
    stops meaning what it claims."""
    from src.probe import _cases

    name, _, _, raw, product, expect_issues = next(
        c for c in _cases() if c[0] == "clean-control")
    assert expect_issues is False, "fixture drift: clean-control must expect silence"
    assert not checks.run_checks(raw, product), (
        "deterministic rules flag the record the LLM validator must call clean")


def test_checks_merge_is_additive_and_never_silently_upgrades():
    clean_report = ValidationReport(issues=[], verdict="pass")
    dirty = cylinder_product(brand=sourced("Allen-Bradley"))

    merged = checks.merge(CYLINDER, dirty, clean_report)
    assert len(merged.issues) == 1, "rule findings join the model's findings"
    assert merged.verdict == "revise", "a rule disagreeing with `pass` downgrades it"
    assert clean_report.issues == [] and clean_report.verdict == "pass", (
        "the model's own report must not be mutated — probe.py measures it alone")

    # No findings -> the report passes through untouched, not rebuilt.
    assert checks.merge(CYLINDER, cylinder_product(), clean_report) is clean_report


def test_checks_still_run_when_validation_never_happened():
    """The free half of the audit is exactly what a record needs when the API
    call died. It must add findings without disguising the API failure."""
    from src.enrich import UNAUDITED_MARKER, is_unaudited

    unaudited = ValidationReport(
        issues=[Issue(field="*", severity="unsupported",
                      detail=f"{UNAUDITED_MARKER} (RuntimeError); record is unaudited.",
                      suggested_confidence=0.0)],
        verdict="revise")
    merged = checks.merge(CYLINDER, cylinder_product(brand=sourced("Allen-Bradley")),
                          unaudited)
    assert len(merged.issues) == 2
    assert is_unaudited(merged), "adding findings must not hide that the audit failed"


def test_apply_report_reaches_every_spec_sharing_a_name():
    """Contradictory specs canonicalise to one name. A lookup that kept only the
    last would flag the contradiction while leaving the other claim published."""
    product = cylinder_product(specs=[
        spec("operating voltage", "24", "V"), spec("operating voltage", "240", "V")])
    apply_report(product, ValidationReport(
        issues=[Issue(field="spec:operating voltage", severity="contradiction",
                      detail="two values", suggested_confidence=0.1)],
        verdict="revise"))
    assert [s.confidence for s in product.specs] == [0.1, 0.1]


def test_page_window_clamps_instead_of_erroring():
    """A hand-typed ?page=999 during a demo shows the last page, not a stack
    trace. Pure arithmetic, so it is testable without a database or a client."""
    from src.app import page_window

    assert page_window(0, 1) == (0, 1, 1), "an empty catalog still has one page"
    assert page_window(250, 3, per_page=100) == (200, 3, 3)
    assert page_window(250, 999, per_page=100) == (200, 3, 3)
    assert page_window(250, 0, per_page=100) == (0, 1, 3)


def test_requirements_cover_what_the_app_imports():
    """The live prototype is a submission item, and `fastapi`/`uvicorn` were
    imported by src/app.py but absent from requirements.txt — it only ran here
    because this machine happened to have them. A clean host would install from
    requirements and then fail at import."""
    required = Path("requirements.txt").read_text(encoding="utf-8").lower()
    for package in ("fastapi", "uvicorn", "pydantic"):
        assert package in required, f"{package} is imported but not in requirements.txt"


def test_app_binds_the_host_port_when_deployed():
    """A PaaS injects $PORT and requires a bind on 0.0.0.0. Binding loopback
    makes the container unreachable, so the health check fails and the instance
    is killed — which surfaces as a crash loop with nothing in the logs."""
    source = Path("src/app.py").read_text(encoding="utf-8")
    assert 'os.getenv("PORT"' in source, "app must read $PORT"
    assert '"0.0.0.0"' in source, "app must bind all interfaces when PORT is set"
    assert 'uvicorn.run(app, host=host, port=port' in source, (
        "uvicorn must use the resolved host/port, not hardcoded values")


def test_seed_on_boot_fills_an_empty_catalog_and_never_overwrites():
    """catalog.db is gitignored, so a fresh deploy has no data and the UI would
    render 'No products yet' in the one place it is being judged."""
    import json as _json

    from src import app as app_module

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fresh.db"
        original = store.DB_PATH
        store.DB_PATH = db
        try:
            export = _json.loads(Path("data/demo_catalog.json").read_text(encoding="utf-8"))
            assert export, "data/demo_catalog.json must ship with records"

            app_module.seed_if_empty()
            conn = store.connect()
            seeded = conn.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
            conn.close()
            assert seeded == len(export), "boot seed must populate an empty catalog"

            # Second call must be a no-op: never clobber a real enrichment.
            app_module.seed_if_empty()
            conn = store.connect()
            again = conn.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
            conn.close()
            assert again == seeded, "seeding twice must not duplicate or overwrite"
        finally:
            store.DB_PATH = original


def test_ui_routes_render_against_a_real_database():
    """The routes were only ever smoke-tested by hand, and a unit test of their
    helpers cannot catch a route that doesn't run at all: adding the `?page=`
    query parameter shadowed the module-level `page()` renderer inside
    `catalog()`, so every catalog request raised `'int' object is not callable`
    while every test still passed. Call the routes."""
    from src import app as webapp

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ui.db"
        conn = store.connect(db)
        store.save(conn, cylinder_product(), ValidationReport(
            issues=[Issue(field="brand", severity="unsupported",
                          detail="not in the record", suggested_confidence=0.3)],
            verdict="revise"))
        conn.close()

        original = store.DB_PATH
        store.DB_PATH = db  # resolved per call, not frozen as a default argument
        try:
            listing = webapp.catalog().body.decode()
            assert "HDX-4025-200" in listing
            assert webapp.catalog(page=999).body.decode(), "out-of-range page must clamp"

            detail = webapp.product_detail("HDX-4025-200").body.decode()
            assert "Validation" in detail, "the findings panel must render"
            assert "not in the record" in detail, "the validator's actual finding"
            assert "confidence capped at 0.30" in detail

            import json as _json
            payload = _json.loads(webapp.api_products().body.decode())
            assert payload["total"] == 1 and len(payload["products"]) == 1
            assert payload["products"][0]["brand"]["evidence"], "provenance reaches the API"
            assert webapp.product_detail("NOPE").status_code == 200, "unknown SKU is a page"
        finally:
            store.DB_PATH = original


def test_export_seed_roundtrip_makes_no_model_calls():
    """The demo-safety path: enrich once where there is quota, export, and seed
    it on a machine with no API key at all."""
    with tempfile.TemporaryDirectory() as tmp:
        source_db, target_db = Path(tmp) / "a.db", Path(tmp) / "b.db"
        report = ValidationReport(
            issues=[Issue(field="brand", severity="unsupported",
                          detail="not in the record", suggested_confidence=0.3)],
            verdict="revise")

        conn = store.connect(source_db)
        store.save(conn, cylinder_product(), report, at="2026-08-11T09:00:00+00:00")
        exported = store.export_all(conn)
        conn.close()

        assert len(exported) == 1 and exported[0]["enriched_at"] == "2026-08-11T09:00:00+00:00"

        conn = store.connect(target_db)
        assert store.seed(conn, exported, source="demo.json") == 1
        loaded = store.load(conn, "HDX-4025-200")
        assert loaded is not None and loaded.brand.value == "Bosch Rexroth"
        assert loaded.specs[0].evidence == "test", "provenance survives the export"

        # The findings survive the round trip, or the UI panel would go blank.
        restored = store.load_report(conn, "HDX-4025-200")
        assert restored is not None and restored.issues[0].detail == "not in the record"

        # The trail says both when it was enriched and when this DB imported it.
        stages = {r["stage"] for r in
                  conn.execute("SELECT stage FROM audit WHERE sku=?", ("HDX-4025-200",))}
        assert stages == {"enrich", "validate", "seed"}
        when = conn.execute(
            "SELECT created_at FROM audit WHERE sku=? AND stage='enrich'",
            ("HDX-4025-200",)).fetchone()["created_at"]
        assert when == "2026-08-11T09:00:00+00:00", "imported data must not claim to be fresh"
        conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
