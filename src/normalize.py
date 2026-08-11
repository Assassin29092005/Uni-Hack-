"""Deterministic cleanup. Runs before enrichment; makes zero model calls.

Everything here is reproducible, free, and instant. Sending "MM" -> "mm" through
an LLM costs latency, money, and adds a hallucination surface for zero gain — so
anything that can be a lookup table is a lookup table, and the model only sees
already-consistent input.

Second benefit: it makes the AI's contribution measurable. Whatever the enriched
record contains beyond this file's output is what the model actually added.
"""

import re

# Attribute-name synonyms seen across supplier CSVs and scraped tables.
# Left side is lowercased and punctuation-stripped before lookup.
ATTRIBUTE_ALIASES = {
    "mfr": "brand",
    "mfg": "brand",
    "manufacturer": "brand",
    "make": "brand",
    "part no": "sku",
    "part number": "sku",
    "partno": "sku",
    "item code": "sku",
    "mpn": "sku",
    "product name": "name",
    "title": "name",
    "desc": "description",
    "long description": "description",
    "cat": "category",
    "product type": "category",
    "voltage": "operating voltage",
    "op voltage": "operating voltage",
    "temp range": "operating temperature",
    "wt": "weight",
    "dims": "dimensions",
}

# Canonical unit symbols. Keys are lowercased; the values are the SI-ish symbol
# we standardise on, so "MM", "mm.", and "millimeters" all end up as "mm".
UNIT_ALIASES = {
    "mm": "mm", "millimeter": "mm", "millimetre": "mm", "millimeters": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimetre": "cm",
    "m": "m", "meter": "m", "metre": "m", "meters": "m", "metres": "m",
    "in": "in", "inch": "in", "inches": "in", '"': "in",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "g": "g", "gram": "g", "grams": "g",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "v": "V", "volt": "V", "volts": "V", "vdc": "V DC", "vac": "V AC",
    "a": "A", "amp": "A", "amps": "A", "ampere": "A", "amperes": "A",
    "w": "W", "watt": "W", "watts": "W",
    "hz": "Hz", "hertz": "Hz",
    "bar": "bar", "psi": "psi",
    "nm": "N·m", "n-m": "N·m", "newton meter": "N·m", "newton metre": "N·m",
    "c": "°C", "°c": "°C", "celsius": "°C", "degc": "°C",
    "f": "°F", "°f": "°F", "fahrenheit": "°F",
    "rpm": "rpm",
}

# "24VDC", "1.5 mm", "10-30 V" — number(s) then a unit word.
_VALUE_UNIT = re.compile(r"^\s*([\d.,]+(?:\s*[-–x×]\s*[\d.,]+)?)\s*([a-zA-Z°\"·\-]+)\s*$")


def normalize_key(key: str) -> str:
    """Attribute name -> canonical name. Unknown names pass through cleaned."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", key.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return ATTRIBUTE_ALIASES.get(cleaned, cleaned)


def normalize_unit(unit: str) -> str:
    """Unit token -> canonical symbol. Unknown units pass through untouched
    rather than being dropped: an unrecognised unit is still information, and
    silently deleting it would be worse than showing it as-is."""
    return UNIT_ALIASES.get(unit.strip().lower().rstrip("."), unit.strip())


def normalize_value(value: str) -> str:
    """Collapse whitespace and canonicalise a trailing unit if there is one.

    Splits "24VDC" into "24 V DC" so downstream comparison and display don't
    have to re-parse supplier formatting quirks.
    """
    text = re.sub(r"\s+", " ", value.strip())
    if not text:
        return ""
    match = _VALUE_UNIT.match(text)
    if not match:
        return text
    number, unit = match.groups()
    number = re.sub(r"\s*[-–x×]\s*", "-", number)  # "10 - 30" -> "10-30"
    return f"{number} {normalize_unit(unit)}".strip()


def normalize_record(sku: str, attributes: dict[str, str], text: str = "") -> "RawRecord":
    """Clean one record. Drops empty values, canonicalises keys and values.

    On a key collision after aliasing (e.g. both "mfr" and "manufacturer"
    present) the first non-empty value wins — later duplicates are usually the
    blank column in a merged export.
    """
    from .models import RawRecord  # local import keeps this module import-light

    out: dict[str, str] = {}
    for key, value in attributes.items():
        if value is None or not str(value).strip():
            continue
        canonical = normalize_key(key)
        if canonical in ("sku", ""):
            continue  # SKU is passed separately; blank keys are export artifacts
        out.setdefault(canonical, normalize_value(str(value)))
    return RawRecord(sku=sku.strip(), attributes=out, text=re.sub(r"\s+", " ", text or "").strip())


# ponytail: hand-maintained alias tables, so coverage is whatever we thought of.
# Unknown attributes fall through to the LLM path, which is the correct failure
# mode — they get enriched, just without the free deterministic head start.
# Upgrade path if the catalog gets wide: mine the alias map from the actual
# column-name distribution across ingested files rather than guessing it.
