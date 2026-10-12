import os
import re
import calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

API_URL = "https://exam.sanand.workers.dev/questionData"
EMAIL = os.getenv("EXAM_EMAIL", "24f2005619@ds.study.iitm.ac.in")
QUIZ_SIGN = os.getenv(
    "EXAM_QUIZ_SIGN",
    "ZRIwO+aVfPd8nxqco0fUps+Z1wEtWTKAdMjkoN1ZFipYfSsGTLYUYVer86zVtT7pS/Yz2G6eLDGk24ZDrH+xSUUH5sdstn2osRweL/nGFDe+gFkijDtkMhp+24iOW3XfVUr+FH3X1DYapA+KUKqPmXkj5KMzB9xtEQSrkUxFnFvM0aN11atV+eMXeO+hJMsy0VlQVkHE366Hbf121lt1+xA8Z2ZBCFje+3z68cdhFzmmChhzURuIZ8AaMLWfl251evL8UdaBEVAZdL2RkvhROb4ADXRDjdfzv+MQ4L9B3DAYdDHXz9xrc5ICYrriFMgj0cz3OGbtDb6ZlCVXlN5lIw=="
)
QUESTION_ID = "q-ledger-agent-server"
TZ = ZoneInfo("Asia/Kolkata")

DATA = {"orders": [], "rates": {"USD": 1.0}, "ready": False, "error": None}

def api_get(path, **extra):
    params = {"email": EMAIL, "quizSign": QUIZ_SIGN,
              "questionId": QUESTION_ID, "path": path}
    params.update(extra)
    r = requests.get(API_URL, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None

def updated_ts(row):
    dt = parse_dt(row.get("updated_at"))
    return dt.timestamp() if dt else 0

def load_data():
    try:
        rates_obj = api_get("/rates")
        rates = rates_obj.get("usd_per_unit") or rates_obj.get("rates") or {"USD": 1.0}
        first = api_get("/orders", page=1)
        pages = int(first.get("pages", 1))
        rows = list(first.get("orders", []))

        def fetch(p):
            return api_get("/orders", page=p).get("orders", [])

        with ThreadPoolExecutor(max_workers=20) as pool:
            fs = [pool.submit(fetch, p) for p in range(2, pages + 1)]
            for f in as_completed(fs):
                rows.extend(f.result())

        # Current order = latest updated_at for duplicate order IDs.
        current = {}
        for r in rows:
            oid = str(r.get("id", r.get("order_id", "")))
            if oid not in current or updated_ts(r) > updated_ts(current[oid]):
                current[oid] = r

        DATA["orders"] = list(current.values())
        DATA["rates"] = rates
        DATA["ready"] = True
        DATA["error"] = None
    except Exception as e:
        DATA["error"] = str(e)
        DATA["ready"] = False

@app.on_event("startup")
def startup():
    load_data()

class Question(BaseModel):
    question: str

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

def usd(r):
    amount = float(r.get("amount", r.get("total", 0)) or 0)
    cur = str(r.get("currency", "USD")).upper()
    rates = DATA["rates"]
    rate = rates.get(cur)
    if rate is None:
        # Some APIs return currency codes with lower-case keys.
        rate = next((v for k, v in rates.items() if str(k).upper() == cur), None)
    if rate is None:
        if cur == "USD":
            rate = 1
        else:
            raise ValueError("No USD exchange rate for " + cur)
    return amount * float(rate)

def all_values(field):
    return sorted({str(r.get(field)) for r in DATA["orders"] if r.get(field) is not None}, key=len, reverse=True)

def extract_filters(q):
    low = q.lower()
    year_m = re.search(r"\b(20\d{2})\b", low)
    year = int(year_m.group(1)) if year_m else None

    month = None
    for name, num in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            month = num
            break

    # If a year/month is mentioned, use the order's business date (created_at).
    regions = all_values("region")
    region = next((x for x in regions if re.search(r"\b"+re.escape(x.lower())+r"\b", low)), None)

    products = all_values("product")
    product = next((x for x in products if x.lower() in low), None)

    customers = all_values("customer")
    customer = next((x for x in customers if re.search(r"\b"+re.escape(x.lower())+r"\b", q, re.I)), None)

    return year, month, region, product, customer

def filtered_orders(q):
    low = q.lower()
    year, month, region, product, customer = extract_filters(q)

    # Status semantics.
    if any(x in low for x in ("refund", "refunded", "refunds")):
        statuses = {"refunded"}
    elif "void" in low:
        statuses = {"void"}
    elif any(x in low for x in ("unpaid", "pending")):
        statuses = {"unpaid", "pending"}
    else:
        # Revenue, sales, purchases and "bought" mean paid unless explicitly all orders.
        statuses = None if any(x in low for x in ("all orders", "regardless of status", "all statuses")) else {"paid"}

    out = []
    for r in DATA["orders"]:
        status = str(r.get("status", "")).lower()
        if statuses is not None and status not in statuses:
            continue
        if region and str(r.get("region", "")).lower() != region.lower():
            continue
        if product and str(r.get("product", "")).lower() != product.lower():
            continue
        if customer and str(r.get("customer", "")).lower() != customer.lower():
            continue

        if year is not None or month is not None:
            dt = parse_dt(r.get("created_at") or r.get("date") or r.get("updated_at"))
            if not dt:
                continue
            if year is not None and dt.year != year:
                continue
            if month is not None and dt.month != month:
                continue
        out.append(r)
    return out

def round2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def wants_count(low):
    return bool(re.search(r"\b(how many|number of|count|counted|distinct|unique)\b", low))

def is_average(low):
    return bool(re.search(r"\b(average|avg|mean|on average)\b", low))

def group_dimension(low):
    # Most-specific grouping requested by the question.
    if "customer" in low:
        return "customer"
    if "product" in low:
        return "product"
    if "region" in low:
        return "region"
    if "month" in low or "monthly" in low:
        return "month"
    return None

def answer_question(q):
    if not DATA["ready"]:
        raise RuntimeError("Ledger data is not ready: " + str(DATA["error"]))

    low = q.lower()
    rows = filtered_orders(q)

    # Explicit distinct customer count.
    if ("distinct customer" in low or "unique customer" in low or
        ("how many customers" in low and not ("per customer" in low))):
        return len({r.get("customer") for r in rows if r.get("customer")})

    # Explicit distinct product count.
    if ("distinct product" in low or "unique product" in low):
        return len({r.get("product") for r in rows if r.get("product")})

    # Average order value. Do this before totals.
    if is_average(low):
        if not rows:
            return 0.0
        return round2(sum(usd(r) for r in rows) / len(rows))

    # Quantity / units.
    if any(x in low for x in ("units sold", "total units", "how many units", "quantity")):
        return sum(int(r.get("qty", r.get("quantity", 0)) or 0) for r in rows)

    # Grouped "top/best/highest/lowest" questions.
    top = any(x in low for x in ("top ", "highest", "best-selling", "best selling",
                                 "most ", "largest", "greatest", "maximum"))
    bottom = any(x in low for x in ("lowest", "least", "smallest", "minimum"))
    group = group_dimension(low)

    if group and (top or bottom):
        metric_is_count = wants_count(low) and not any(x in low for x in ("revenue", "sales", "amount", "usd", "dollar", "value"))
        buckets = {}
        for r in rows:
            key = r.get(group)
            if key is None:
                continue
            if metric_is_count:
                val = 1
            elif "unit" in low or "quantity" in low:
                val = int(r.get("qty", r.get("quantity", 0)) or 0)
            else:
                val = usd(r)
            buckets[str(key)] = buckets.get(str(key), 0) + val
        if not buckets:
            return "No matching data"
        return (max if not bottom else min)(buckets.items(), key=lambda x: x[1])[0]

    # Plain count questions.
    if wants_count(low):
        if "order" in low or "transaction" in low:
            return len(rows)
        if "customer" in low:
            return len({r.get("customer") for r in rows if r.get("customer")})
        if "product" in low:
            return len({r.get("product") for r in rows if r.get("product")})
        return len(rows)

    # Total monetary value.
    if any(x in low for x in ("revenue", "sales", "refund", "refunded",
                              "amount", "value", "total", "sum", "usd", "dollar", "worth")):
        return round2(sum(usd(r) for r in rows))

    # If the question asks "which X" without top language, return the max revenue X.
    if low.startswith("which product") or low.startswith("what product"):
        buckets = {}
        for r in rows:
            k = str(r.get("product", ""))
            buckets[k] = buckets.get(k, 0) + usd(r)
        return max(buckets.items(), key=lambda x:x[1])[0] if buckets else "No matching data"

    return round2(sum(usd(r) for r in rows))

@app.get("/")
def root():
    return {"service":"Acme Appliances Ledger Agent",
            "ready":DATA["ready"],
            "orders_loaded":len(DATA["orders"]),
            "health":"ok" if DATA["ready"] else "error",
            "error":DATA["error"]}

@app.post("/")
def ask(body: Question):
    if not body.question.strip():
        raise HTTPException(400, "question must not be empty")
    try:
        return {"answer": answer_question(body.question)}
    except Exception as e:
        raise HTTPException(500, str(e))


