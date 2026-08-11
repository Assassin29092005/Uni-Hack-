"""Web UI — the explainability view.

    python -m src.app          then open http://127.0.0.1:8000

Two pages. The catalog table is table stakes; the record page is the demo — it
shows, per field, what the AI produced, where it came from, how sure it is, and
the evidence, plus the fields it refused to fill. That refusal surface is the
thing a normal product-data tool cannot show you.

Deliberately one file with inline HTML: no template directory, no CSS build, no
JS framework. A demo UI that needs a build step is a demo UI that breaks on
someone else's laptop.

Everything rendered here — descriptions, evidence, spec names — is model output,
so every interpolation goes through `esc()`. Untrusted text into HTML is how you
get a `<script>` tag in a product description.
"""

from html import escape as _escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import llm, store
from .models import Product, Sourced

app = FastAPI(title="Product Intelligence")


def esc(value) -> str:
    """Escape anything on its way into HTML. Model output is untrusted input."""
    return _escape(str(value if value is not None else ""))


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0f1115; color:#e6e8eb;
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
a { color:inherit; text-decoration:none; }
.wrap { max-width:1080px; margin:0 auto; padding:32px 20px 64px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:#8b93a1; font-size:13px; margin-bottom:24px; }
.stats { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:26px; }
.stat { background:#171a21; border:1px solid #232833; border-radius:10px;
        padding:10px 14px; min-width:120px; }
.stat b { display:block; font-size:20px; font-weight:600; }
.stat span { color:#8b93a1; font-size:11px; text-transform:uppercase;
             letter-spacing:.06em; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:#8b93a1; font-weight:500; padding:8px 10px; border-bottom:1px solid #232833; }
td { padding:11px 10px; border-bottom:1px solid #1b1f27; vertical-align:middle; }
tr:hover td { background:#151922; }
.sku { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
.bar { width:104px; height:6px; background:#232833; border-radius:3px; overflow:hidden; }
.bar i { display:block; height:100%; }
.pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:11px;
        font-weight:500; }
.pass { background:#10331f; color:#63d894; }
.revise { background:#3a2f10; color:#e0b34a; }
.reject { background:#3a1717; color:#e77; }
.gap { background:#2a2f3a; color:#98a2b3; }
.field { border:1px solid #232833; border-radius:12px; padding:16px 18px; margin-bottom:12px;
         background:#141821; }
.field.missing { border-style:dashed; border-color:#3a2f10; background:#16150f; }
.fname { font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:#8b93a1; }
.fval { font-size:19px; font-weight:600; margin:3px 0 10px; }
.fval.none { color:#e0b34a; font-weight:500; font-style:italic; font-size:16px; }
.meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:12px;
        color:#8b93a1; margin-bottom:8px; }
.src { background:#1d2430; color:#9fb3d1; padding:2px 8px; border-radius:5px;
       font-size:11px; }
.ev { font-size:13px; color:#b6bdc9; border-left:2px solid #2c3340; padding-left:11px; }
.back { color:#8b93a1; font-size:13px; display:inline-block; margin-bottom:16px; }
.empty { color:#8b93a1; padding:40px 0; text-align:center; }
"""


def confidence_color(confidence: float, has_value: bool) -> str:
    """Green = grounded and confident, amber = weak, grey = no value at all.
    The colour is the honest signal a buyer needs at a glance."""
    if not has_value:
        return "#5b6472"
    if confidence >= 0.85:
        return "#3fb27f"
    if confidence >= 0.45:
        return "#c99a2e"
    return "#c0554e"


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


def bar(fraction: float, color: str) -> str:
    pct = max(0.0, min(1.0, fraction)) * 100
    return f"<div class=bar><i style='width:{pct:.0f}%;background:{color}'></i></div>"


@app.get("/", response_class=HTMLResponse)
def catalog():
    conn = store.connect()
    rows = conn.execute(
        """SELECT sku, completeness, mean_confidence, verdict, issue_count, updated_at
           FROM products ORDER BY completeness ASC, sku"""
    ).fetchall()
    stats = store.summary(conn)
    conn.close()

    if not rows:
        return page("Catalog", "<h1>Product Intelligence</h1>"
                    "<div class=empty>No products yet.<br><br>"
                    "<code>python -m src.pipeline data/sample_products.csv</code></div>")

    cards = "".join(
        f"<div class=stat><b>{esc(v)}</b><span>{esc(k.replace('_', ' '))}</span></div>"
        for k, v in stats.items()
    )

    body_rows = ""
    for row in rows:
        color = confidence_color(row["mean_confidence"], row["completeness"] > 0)
        issues = (f"<span class='pill revise'>{row['issue_count']}</span>"
                  if row["issue_count"] else "<span style='color:#5b6472'>0</span>")
        body_rows += (
            f"<tr onclick=\"location='/product/{esc(row['sku'])}'\" style=cursor:pointer>"
            f"<td class=sku>{esc(row['sku'])}</td>"
            f"<td><span class='pill {esc(row['verdict'])}'>{esc(row['verdict'])}</span></td>"
            f"<td>{bar(row['completeness'], color)}</td>"
            f"<td>{row['completeness']:.0%}</td>"
            f"<td>{row['mean_confidence']:.2f}</td>"
            f"<td>{issues}</td></tr>"
        )

    return page("Catalog", f"""
      <h1>Product Intelligence</h1>
      <div class=sub>Enriched via {esc(llm.describe())}. Sorted least-complete first —
      the records needing attention are at the top.</div>
      <div class=stats>{cards}</div>
      <table><thead><tr><th>SKU</th><th>Verdict</th><th colspan=2>Completeness</th>
      <th>Confidence</th><th>Issues</th></tr></thead><tbody>{body_rows}</tbody></table>
    """)


def render_field(label: str, field: Sourced) -> str:
    """One field with its full provenance. A field with no grounded value is
    rendered as a visible, explained gap rather than being hidden — showing what
    the AI declined to invent is the point, not an omission to paper over."""
    grounded = field.is_grounded
    color = confidence_color(field.confidence, field.value is not None)

    if field.value is None:
        value_html = "<div class='fval none'>no grounded value</div>"
    else:
        shown = f"{field.value} {field.unit}" if field.unit else field.value
        cls = "fval" if grounded else "fval none"
        value_html = f"<div class='{cls}'>{esc(shown)}</div>"

    return f"""
      <div class="field{'' if grounded else ' missing'}">
        <div class=fname>{esc(label)}</div>
        {value_html}
        <div class=meta>
          <span class=src>{esc(field.source)}</span>
          {bar(field.confidence, color)}
          <span>confidence {field.confidence:.2f}</span>
          {'' if grounded else "<span class='pill gap'>below publish threshold</span>"}
        </div>
        <div class=ev>{esc(field.evidence)}</div>
      </div>"""


@app.get("/product/{sku}", response_class=HTMLResponse)
def product_detail(sku: str):
    conn = store.connect()
    product = store.load(conn, sku)
    audit = conn.execute(
        "SELECT stage, detail, created_at FROM audit WHERE sku=? ORDER BY id DESC LIMIT 4",
        (sku,),
    ).fetchall()
    conn.close()

    if product is None:
        return page("Not found", f"<a class=back href='/'>&larr; Catalog</a>"
                                 f"<h1>{esc(sku)} not found</h1>")

    fields = [("name", product.name), ("brand", product.brand),
              ("category", product.category), ("description", product.description)]
    fields += [(f"spec · {s.name}", s) for s in product.specs]
    rendered = "".join(render_field(label, field) for label, field in fields)

    gaps = (f"<div class=sub>Declined to fill: "
            f"<b>{esc(', '.join(product.gaps))}</b> — shown below with the reason.</div>"
            if product.gaps else
            "<div class=sub>Every field grounded above the publish threshold.</div>")

    trail = "".join(
        f"<tr><td>{esc(r['stage'])}</td><td class=sku>{esc(r['created_at'])}</td></tr>"
        for r in audit
    )

    return page(sku, f"""
      <a class=back href='/'>&larr; Catalog</a>
      <h1>{esc(product.sku)}</h1>
      <div class=sub>Completeness {product.completeness:.0%} ·
        mean confidence {product.mean_confidence:.2f}</div>
      {gaps}
      {rendered}
      <h1 style='font-size:15px;margin-top:28px'>Audit trail</h1>
      <table><thead><tr><th>Stage</th><th>When (UTC)</th></tr></thead>
      <tbody>{trail}</tbody></table>
    """)


@app.get("/api/products")
def api_products():
    """Commerce-ready JSON — the machine-readable half of the deliverable."""
    conn = store.connect()
    rows = conn.execute("SELECT data FROM products ORDER BY sku").fetchall()
    conn.close()
    return JSONResponse([Product.model_validate_json(r["data"]).model_dump() for r in rows])


@app.get("/api/product/{sku}")
def api_product(sku: str):
    conn = store.connect()
    product = store.load(conn, sku)
    conn.close()
    if product is None:
        return JSONResponse({"error": f"{sku} not found"}, status_code=404)
    return JSONResponse({
        **product.model_dump(),
        "completeness": product.completeness,
        "mean_confidence": product.mean_confidence,
        "gaps": product.gaps,
    })


if __name__ == "__main__":
    import uvicorn

    print("Catalog UI  ->  http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
