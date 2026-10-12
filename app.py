import os
import re
import calendar
from datetime import datetime
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
    params = {
        "email": EMAIL,
        "quizSign": QUIZ_SIGN,
        "questionId": QUESTION_ID,
        "path": path,
    }
    params.update(extra)
    r = requests.get(API_URL, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def load_data():
    try:
        rates_obj = api_get("/rates")
        rates = rates_obj.get("usd_per_unit", {"USD": 1.0})
        first = api_get("/orders", page=1)
        pages = int(first.get("pages", 1))
        rows = list(first.get("orders", []))

        def fetch_page(page):
            result = api_get("/orders", page=page)
            return result.get("orders", [])

        # Fetch pages concurrently so startup finishes quickly.
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch_page, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())

        # The ledger can repeat IDs. Keep the row with the latest updated_at.
        current = {}
        def updated_key(row):
            raw = row.get("updated_at", "")
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                return dt.timestamp()
            except Exception:
                return 0.0
        for row in rows:
            oid = str(row.get("id", ""))
            old = current.get(oid)
            if old is None or updated_key(row) > updated_key(old):
                current[oid] = row

        DATA["orders"] = list(current.values())
        DATA["rates"] = rates
        DATA["ready"] = True
        DATA["error"] = None
    except Exception as exc:
        DATA["error"] = str(exc)
        DATA["ready"] = False

@app.on_event("startup")
def startup():
    load_data()

class Question(BaseModel):
    question: str

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def order_date(row):
    raw = row.get("created_at") or row.get("updated_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None

def usd_amount(row):
    amount = float(row.get("amount", 0) or 0)
    currency = str(row.get("currency", "USD")).upper()
    rate = float(DATA["rates"].get(currency, 0) or 0)
    return amount * rate

def filters_from_question(q):
    low = q.lower()
    year_match = re.search(r"\b(20\d{2})\b", low)
    year = int(year_match.group(1)) if year_match else None
    month = None
    for name, number in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            month = number
            break

    region = None
    for name in ("north", "south", "east", "west", "central"):
        if re.search(r"\b" + name + r"\b", low):
            region = name.title()
            break

    products = sorted({str(o.get("product", "")) for o in DATA["orders"] if o.get("product")}, key=len, reverse=True)
    product = next((p for p in products if p.lower() in low), None)

    customer_match = re.search(r"\bC\d{4}\b", q, re.I)
    customer = customer_match.group(0).upper() if customer_match else None

    def date_filter(row):
        dt = order_date(row)
        if not dt:
            return False
        if year is not None and dt.year != year:
            return False
        if month is not None and dt.month != month:
            return False
        return True

    filtered = []
    for row in DATA["orders"]:
        if region and str(row.get("region", "")).lower() != region.lower():
            continue
        if product and str(row.get("product", "")).lower() != product.lower():
            continue
        if customer and str(row.get("customer", "")).upper() != customer:
            continue
        if (year is not None or month is not None) and not date_filter(row):
            continue
        filtered.append(row)
    return filtered

def answer_question(question):
    if not DATA["ready"]:
        raise RuntimeError("Ledger data has not loaded: " + str(DATA["error"]))

    q = question.strip()
    low = q.lower()
    rows = filters_from_question(q)

    # Determine the population first. Revenue and purchases count paid orders only.
    refund_query = any(w in low for w in ("refund", "refunded", "refunds"))
    void_query = "void" in low
    if refund_query:
        chosen = [r for r in rows if str(r.get("status", "")).lower() == "refunded"]
    elif void_query:
        chosen = [r for r in rows if str(r.get("status", "")).lower() == "void"]
    elif any(w in low for w in ("all orders", "regardless of status", "including unpaid")):
        chosen = rows
    else:
        chosen = [r for r in rows if str(r.get("status", "")).lower() == "paid"]

    # Handle averages BEFORE any generic total/sum logic.
    # "On average, how many US dollars is a paid Central order worth?"
    # means average USD amount per qualifying order, not sum and not average per unit.
    if any(w in low for w in ("average", "on average", "mean")):
        amounts = [usd_amount(r) for r in chosen]
        if not amounts:
            return 0.0
        return money(sum(amounts) / len(amounts))

    if any(w in low for w in ("how many distinct customers", "distinct customers", "unique customers",
                              "how many customers", "number of customers", "customer count")):
        return len({r.get("customer") for r in chosen if r.get("customer")})

    if any(w in low for w in ("which product", "top product", "best-selling product",
                              "best selling product", "most popular product",
                              "product generated", "product had the highest",
                              "highest revenue product")):
        # Highest-revenue product uses paid order amounts converted to USD.
        totals = {}
        for r in chosen:
            name = r.get("product", "Unknown")
            totals[name] = totals.get(name, 0.0) + usd_amount(r)
        return max(totals.items(), key=lambda x: x[1])[0] if totals else "No matching data"

    if any(w in low for w in ("most ordered product", "most frequently ordered product",
                              "product with the most orders")):
        totals = {}
        for r in chosen:
            name = r.get("product", "Unknown")
            totals[name] = totals.get(name, 0) + 1
        return max(totals.items(), key=lambda x: x[1])[0] if totals else "No matching data"

    if any(w in low for w in ("highest-spending customer", "highest spending customer",
                              "customer with the most", "top customer", "which customer")):
        totals = {}
        for r in chosen:
            name = r.get("customer", "Unknown")
            totals[name] = totals.get(name, 0.0) + usd_amount(r)
        return max(totals.items(), key=lambda x: x[1])[0] if totals else "No matching data"

    if any(w in low for w in ("how many units", "total units", "units sold", "quantity")):
        return sum(int(r.get("qty", 0) or 0) for r in chosen)

    if any(w in low for w in ("how many", "number of", "count of", "count the")):
        if "order" in low or "transaction" in low:
            return len(chosen)
        if "product" in low:
            return len({r.get("product") for r in chosen if r.get("product")})
        if "customer" in low:
            return len({r.get("customer") for r in chosen if r.get("customer")})
        return len(chosen)

    # Monetary questions: paid orders are revenue; refunds use refunded rows.
    if any(w in low for w in ("revenue", "sales", "refund", "refunded",
                              "amount", "value", "total", "sum", "worth", "usd", "dollar")):
        return money(sum(usd_amount(r) for r in chosen))

    # Conservative default: total USD value of matching paid orders.
    return money(sum(usd_amount(r) for r in chosen))

@app.get("/")
def home():
    return {
        "service": "Acme Appliances Ledger Agent",
        "ready": DATA["ready"],
        "orders_loaded": len(DATA["orders"]),
        "health": "ok" if DATA["ready"] else "loading_or_error",
        "error": DATA["error"],
        "usage": "POST / with JSON {\"question\": \"...\"}"
    }

@app.post("/")
def ask(body: Question):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    try:
        return {"answer": answer_question(body.question)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

