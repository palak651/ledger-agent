"""Ledger Agent v2.
Run:  ROOT_URL="<full url>" LLM_KEY=... uvicorn app:app --host 0.0.0.0 --port 8000
Check http://localhost:8000/debug FIRST: confirm the detected field mapping and rates.
"""
import os, re, json, traceback, datetime as dt, collections, urllib.parse, threading
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI

ROOT = os.environ["ROOT_URL"]
BASE = os.environ.get("LLM_BASE", "https://api.openai.com/v1")
KEY = os.environ.get("LLM_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
IST = ZoneInfo("Asia/Kolkata")
http = httpx.Client(timeout=30, follow_redirects=True)

RAW, RATES, ROWS, MAP, NOTES = [], {}, [], {}, []


def setp(url, **kw):
    p = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(p.query)); q.update({k: str(v) for k, v in kw.items()})
    return p._replace(query=urllib.parse.urlencode(q)).geturl()


def items_of(d):
    if isinstance(d, list): return d
    for k in ("data", "items", "orders", "results", "records"):
        if isinstance(d.get(k), list): return d[k]
    return next((v for v in d.values() if isinstance(v, list)), [])


def load():
    global RATES
    root = http.get(ROOT).json()
    NOTES[:] = root.get("notes", [])
    links = root["links"]
    # orders, all pages
    page, seen_first = 1, None
    while page <= 500:
        d = http.get(setp(links["orders"], page=page)).json()
        its = items_of(d)
        if not its: break
        sig = json.dumps(its[0], sort_keys=True)
        if sig == seen_first: break          # server ignoring page param
        if page == 1: seen_first = sig
        RAW.extend(its)
        tp = d.get("total_pages") or d.get("pages") if isinstance(d, dict) else None
        if tp and page >= int(tp): break
        page += 1
    RATES = http.get(links["rates"]).json()
    build()


def first(keys, row):
    low = {k.lower(): k for k in row}
    for k in keys:
        if k in low: return low[k]
    return None


def parse_dt(v):
    if v is None: return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=dt.timezone.utc).astimezone(IST)
    s = str(v).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(IST)


def rate_for(cur, when):
    """Return multiplier converting 1 unit of `cur` into USD."""
    if not cur or cur.upper() == "USD": return 1.0
    cur = cur.upper()
    r = RATES
    base = (r.get("base") or r.get("base_currency") or "USD") if isinstance(r, dict) else "USD"
    table = None
    if isinstance(r, dict):
        for k in ("rates", "data", "usd_rates", "to_usd"):
            if isinstance(r.get(k), (dict, list)): table = r[k]; break
        if table is None: table = {k: v for k, v in r.items() if isinstance(v, (int, float))}
    else:
        table = r
    val = None
    if isinstance(table, dict):
        v = table.get(cur)
        if isinstance(v, dict):                 # dated: {"EUR": {"2026-03-01": 1.08}}
            ks = sorted(v); day = when.strftime("%Y-%m-%d") if when else ks[-1]
            val = v.get(day) or v[max([k for k in ks if k <= day] or [ks[0]])]
        else: val = v
    elif isinstance(table, list):
        rows = [x for x in table if str(x.get("currency", x.get("code", ""))).upper() == cur]
        dk = next((k for k in ("date", "effective_date", "as_of", "valid_from") if rows and k in rows[0]), None)
        if rows:
            if dk and when:
                day = when.strftime("%Y-%m-%d")
                ok = [x for x in rows if str(x[dk])[:10] <= day] or rows
                x = max(ok, key=lambda x: str(x[dk]))
            else: x = rows[-1]
            val = x.get("rate", x.get("usd_rate", x.get("to_usd")))
    if val is None: return 1.0
    val = float(val)
    rate_key_hint = json.dumps(r)[:600].lower()
    per_usd = base.upper() == "USD" and "to_usd" not in rate_key_hint and "usd_per" not in rate_key_hint
    return (1.0 / val) if per_usd else val


def build():
    if not RAW: return
    s = RAW[0]
    MAP.update({
        "id": first(["order_id", "id", "orderid"], s),
        "updated": first(["updated_at", "updatedat", "modified_at"], s),
        "created": first(["created_at", "order_date", "ordered_at", "placed_at", "date", "createdat", "timestamp"], s),
        "status": first(["status", "order_status", "state"], s),
        "amount": first(["amount_usd", "amount", "total", "total_amount", "price", "revenue", "amount_cents", "total_cents"], s),
        "currency": first(["currency", "currency_code"], s),
        "region": first(["region", "sales_region"], s),
        "product": first(["product", "product_name", "item", "sku_name"], s),
        "customer": first(["customer_id", "customer", "customer_email", "customer_name", "email"], s),
        "refund": next((k for k in s if "refund" in k.lower() and ("amount" in k.lower() or "cent" in k.lower())), None),
    })
    m = MAP
    latest = {}
    for r in RAW:
        k = r.get(m["id"])
        u = r.get(m["updated"]) if m["updated"] else ""
        if k not in latest or str(u) >= str(latest[k].get(m["updated"], "")):
            latest[k] = r
    ROWS.clear()
    for r in latest.values():
        when = parse_dt(r.get(m["created"]))
        def money(key):
            if not key or r.get(key) is None: return None
            v = float(r[key]) / (100 if "cent" in key.lower() else 1)
            if m["currency"] and "usd" not in key.lower():
                v *= rate_for(r.get(m["currency"]), when)
            return v
        norm = lambda x: re.sub(r"\s+", " ", str(x)).strip() if x is not None else None
        ROWS.append({
            "id": r.get(m["id"]), "status": (norm(r.get(m["status"])) or "").lower(),
            "region": norm(r.get(m["region"])), "product": norm(r.get(m["product"])),
            "customer": (norm(r.get(m["customer"])) or "").lower(),
            "usd": money(m["amount"]), "refund_usd": money(m["refund"]),
            "when": when, "year": when.year if when else None, "month": when.month if when else None,
            "raw": r,
        })


SYSTEM = """You answer questions about an order ledger by writing Python.
`rows` = list of CLEAN dicts (already deduplicated to the latest version per order, money converted to USD floats, dates in IST):
id, status (lowercase), region, product, customer, usd (order amount in USD), refund_usd (or None), when (tz-aware datetime in IST),
year, month, raw (original record). Rules: revenue/sales/spend counts ONLY status=='paid' unless the question is about refunds/other statuses.
Region/product/customer matching: case-insensitive. Product names in answers must be spelled exactly as in `rows`.
Write ONLY the source of `def solve(rows):` returning: a number (money rounded to 2 decimals, counts as int),
or a string (e.g. product name for 'top-selling product'), or a list/dict if explicitly asked for several things.
'Top product by revenue' -> return the product NAME. Averages: sum/count unrounded then round(,2). No prints, no markdown."""


def ctx():
    c = lambda f: dict(collections.Counter(r[f] for r in ROWS).most_common(30))
    return (f"NOTES: {NOTES}\nMAPPING: {MAP}\nSTATUSES: {c('status')}\nREGIONS: {c('region')}\nPRODUCTS: {c('product')}\n"
            f"N_ROWS: {len(ROWS)}\nSAMPLE: {[{k: str(v) for k, v in r.items() if k != 'raw'} for r in ROWS[:2]]}\n"
            f"RAW SAMPLE: {json.dumps(RAW[0])[:600]}")


def ask_llm(q, err=None):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"{ctx()}\n\nQUESTION: {q}"}]
    if err: msgs.append({"role": "user", "content": f"Your code failed:\n{err}\nFix it."})
    r = httpx.post(f"{BASE}/chat/completions", timeout=10, headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": MODEL, "messages": msgs, "temperature": 0})
    code = r.json()["choices"][0]["message"]["content"]
    return re.sub(r"^```(?:python)?\s*|```\s*$", "", code.strip(), flags=re.M).strip()


def answer(q):
    err = None
    for _ in range(2):
        try:
            env = {"datetime": dt, "collections": collections, "re": re}
            exec(ask_llm(q, err), env)
            a = env["solve"](ROWS)
            if isinstance(a, float):
                a = round(a, 2)
                if a == int(a) and abs(a) < 1e12 and "." not in str(a).rstrip("0"): pass
            return a
        except Exception:
            err = traceback.format_exc()[-600:]
    return None


app = FastAPI()
ready = threading.Event()


def boot():
    try: load()
    finally: ready.set()


threading.Thread(target=boot, daemon=True).start()


@app.post("/")
def ask(body: dict):
    ready.wait(12)
    return {"answer": answer(body.get("question", ""))}


@app.get("/debug")
def debug():
    return {"mapping": MAP, "notes": NOTES, "n_raw": len(RAW), "n_clean": len(ROWS),
            "rates": RATES, "sample": [{k: str(v) for k, v in r.items() if k != "raw"} for r in ROWS[:3]],
            "raw_sample": RAW[:2]}
