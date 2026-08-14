"""Manufacturer-source enrichment — guide step 5, and the biggest score gap.

`src/truth.py` put a number on why this matters: of 41 fields where our output
differs from Unilog's, roughly 24 are things their rows took from the
manufacturer's own website. Their sheet even names the source in its own
`MFR URL` column. Nothing in a 6-column input can ground a sound rating or a
wash-cycle count; a document can.

**The sourcing hierarchy is a rule, not a preference.** The guidelines require
product data to come from the manufacturer's own site or documentation, and
exclude marketplaces and distributor sites explicitly. That is enforced here in
code (`classify`), before a fetch is attempted, and a refusal is recorded with
its reason rather than silently skipped — a buyer-facing spec sourced from a
marketplace listing is exactly the kind of laundered guess this project exists
to prevent.

**We do not defeat bot protection.** Standard user agent, `robots.txt`
respected, and a 403 or a timeout is taken as "no". Both manufacturer sources in
the ground-truth rows currently refuse automated fetches (frigidaire.com times
out, whirlpool.com returns 403), which is their right; the honest response is to
report the refusal and fall back to a document the operator supplies, not to
disguise the request. `SourceResult.reason` carries that story into the audit
trail.

Two ways a document reaches a record, in priority order:

1. **A local file** at `data/documents/<SKU>.{txt,html}` — a datasheet the
   operator downloaded. Always allowed: a human chose it.
2. **A URL** named in the input row (`MFR URL`, `Ref URL 1`..`5`), subject to
   `classify` and `robots.txt`.

Fetched pages are cached under `data/cache/` keyed by URL hash, so a re-run
costs nothing and the catalog stays resumable — the same property `is_done`
gives enrichment.
"""

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path

DOCUMENTS = Path("data/documents")
CACHE = Path("data/cache")

# Columns in their delivery format that name a source. Read in this order.
URL_COLUMNS = ("MFR URL", "Ref URL 1", "Ref URL 2", "Ref URL 3", "Ref URL 4", "Ref URL 5")

# Distributors, marketplaces and aggregators. The guidelines exclude these
# outright — not because the data there is always wrong, but because it is
# unattributable: a marketplace listing is written by a reseller, and a spec
# taken from one carries a manufacturer's authority it never had.
DENIED_HOSTS = {
    "amazon", "ebay", "walmart", "alibaba", "aliexpress", "etsy", "temu",
    "grainger", "mcmaster", "homedepot", "lowes", "fastenal", "zoro", "ferguson",
    "globalindustrial", "supplyhouse", "wayfair", "acehardware", "menards",
    "octopart", "findchips", "alldatasheet", "datasheetcatalog",
}

# Standard, honest identification. Not a disguise: a site that declines this is
# declining us, and that answer is respected.
USER_AGENT = "UnilogProductIntelligence/1.0 (hackathon prototype; contact via repo)"

# Dotted labels of alphanumerics and hyphens — a real hostname, not prose that
# happened to sit in a URL column.
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
TIMEOUT_SECONDS = 20
MAX_BYTES = 2_000_000


@dataclass
class SourceResult:
    """What sourcing produced for one record, including its refusals.

    `text` empty with a populated `reason` is a first-class outcome, not an
    error — "the manufacturer's site declined automated access" is information
    a reviewer needs, and it is the difference between a gap we understand and
    a gap we never looked at.
    """

    sku: str
    text: str = ""
    url: str = ""
    origin: str = ""       # "document" (local file) | "web" (fetched) | ""
    reason: str = ""       # why there is no text, or where the text came from

    @property
    def ok(self) -> bool:
        return bool(self.text)


def classify(url: str) -> tuple[bool, str]:
    """Is this URL an allowed source? Returns (allowed, reason).

    Deliberately a denylist of marketplaces rather than an allowlist of
    manufacturers: we cannot enumerate every manufacturer domain, and defaulting
    to "deny" would refuse the legitimate long tail this catalog is full of.
    The failure mode is bounded — an unknown domain is still checked against
    robots.txt, and whatever it yields is labelled `web`, which the validator
    and the confidence floor already treat as the weakest provenance.
    """
    candidate = url.strip()
    # A scheme, if present, must be a web one. Checked on the raw string
    # because `mailto:a@b.c` has no "//" — prepending https:// to it produced
    # the host "b.c" and a cheerful allow.
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", candidate)
    if scheme_match:
        if scheme_match.group(1).lower() not in ("http", "https"):
            return False, f"non-web scheme {scheme_match.group(1)!r}"
    else:
        candidate = f"https://{candidate}"

    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return False, f"unparseable URL: {url!r}"
    host = (parsed.hostname or "").lower()
    # Must actually look like a hostname. Without this, free text in a URL
    # column ("not a url", "see datasheet") parses to a "host" and is allowed
    # through to the fetcher — a URL column in a supplier export contains
    # prose far more often than it contains nothing.
    if not _HOSTNAME.match(host):
        return False, f"not a hostname: {url!r}"

    # Match on labels, not substrings: "myamazonbrand.com" is not Amazon, and
    # "grainger" appearing inside a longer word should not deny a real vendor.
    labels = set(host.split("."))
    hit = labels & DENIED_HOSTS
    if hit:
        return False, (f"{host} is a distributor/marketplace ({', '.join(sorted(hit))}); "
                       f"the guidelines require the manufacturer's own source")
    return True, host


def _robots_allows(url: str) -> tuple[bool, str]:
    """Respect robots.txt. A site that cannot be read is given the benefit of
    the doubt — an unreachable robots.txt is not a prohibition — but a rule
    that names us is obeyed."""
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception:  # noqa: BLE001 — unreachable robots.txt is not a refusal
        return True, ""
    if parser.can_fetch(USER_AGENT, url):
        return True, ""
    return False, f"robots.txt at {parsed.netloc} disallows this path"


def _cache_path(url: str) -> Path:
    return CACHE / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.txt"


def strip_html(html: str) -> str:
    """Crude tag stripping — enough to hand a model readable prose.

    Deliberately not a parser dependency. Script and style contents are removed
    first because their text is code, and a model shown minified JavaScript will
    find "specifications" in it.
    """
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, use_cache: bool = True) -> SourceResult:
    """Retrieve one URL, honouring the sourcing policy and robots.txt."""
    allowed, reason = classify(url)
    if not allowed:
        return SourceResult(sku="", url=url, reason=f"refused: {reason}")

    cached = _cache_path(url)
    if use_cache and cached.exists():
        return SourceResult(sku="", text=cached.read_text(encoding="utf-8"),
                            url=url, origin="web", reason=f"cached from {url}")

    permitted, robots_reason = _robots_allows(url)
    if not permitted:
        return SourceResult(sku="", url=url, reason=f"refused: {robots_reason}")

    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BYTES)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        # 403 is the site declining automated access. That is its decision, and
        # working around it is not something this pipeline does.
        return SourceResult(sku="", url=url,
                            reason=f"unavailable: HTTP {exc.code} from {url}")
    except Exception as exc:  # noqa: BLE001
        return SourceResult(sku="", url=url,
                            reason=f"unavailable: {type(exc).__name__} fetching {url}")

    if "pdf" in content_type.lower() or raw[:4] == b"%PDF":
        # ponytail: PDFs are the richest manufacturer source and need a parser
        # dependency we have not taken. Named explicitly rather than silently
        # producing empty text.
        return SourceResult(sku="", url=url,
                            reason=f"unsupported: PDF at {url} (no PDF parser wired in)")

    text = strip_html(raw.decode("utf-8", errors="replace"))
    if not text:
        return SourceResult(sku="", url=url, reason=f"empty document at {url}")

    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return SourceResult(sku="", text=text, url=url, origin="web",
                        reason=f"fetched from {url}")


def local_document(sku: str) -> SourceResult:
    """A datasheet the operator put on disk. Always allowed — a human chose it."""
    for suffix in (".txt", ".html", ".htm"):
        path = DOCUMENTS / f"{sku}{suffix}"
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = strip_html(raw) if suffix != ".txt" else re.sub(r"\s+", " ", raw).strip()
        if text:
            return SourceResult(sku=sku, text=text, url=str(path), origin="document",
                                reason=f"local document {path}")
    return SourceResult(sku=sku, reason="no local document")


def resolve(sku: str, raw: dict[str, str], allow_web: bool = True) -> SourceResult:
    """Find the best available source for one record.

    Local documents win over the web: an operator who downloaded a datasheet has
    made a sourcing decision we should not second-guess, and it costs no network.
    """
    local = local_document(sku)
    if local.ok:
        return local

    if not allow_web:
        return SourceResult(sku=sku, reason="web sourcing disabled (--sources to enable)")

    attempts: list[str] = []
    for column in URL_COLUMNS:
        url = (raw.get(column) or "").strip()
        if not url:
            continue
        result = fetch(url)
        if result.ok:
            result.sku = sku
            return result
        attempts.append(result.reason)

    return SourceResult(
        sku=sku,
        reason="; ".join(attempts) if attempts else "no manufacturer URL in the input row",
    )

# ponytail: no PDF parsing and no per-brand URL templates. Their ground-truth
# rows cite manufacturer PDFs (Whirlpool's owner's manual) and both cited sites
# refuse automated access, so the realistic path to a wide demo is an operator
# dropping files into data/documents/ rather than live crawling. Templates
# (brand -> product-page URL pattern) would widen coverage but need the approved
# manufacturer list to map a brand to a domain reliably.
