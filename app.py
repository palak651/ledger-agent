import os
import re
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

API_URL = "https://exam.sanand.workers.dev/questionData"
EMAIL = os.getenv("EXAM_EMAIL", "24f2005619@ds.study.iitm.ac.in")
QUIZ_SIGN = os.getenv("EXAM_QUIZ_SIGN", "ZRIwO+aVfPd8nxqco0fUps+Z1wEtWTKAdMjkoN1ZFipYfSsGTLYUYVer86zVtT7pS/Yz2G6eLDGk24ZDrH+xSUUH5sdstn2osRweL/nGFDe+gFkijDtkMhp+24iOW3XfVUr+FH3X1DYapA+KUKqPmXkj5KMzB9xtEQSrkUxFnFvM0aN11atV+eMXeO+hJMsy0VlQVkHE366Hbf121lt1+xA8Z2ZBCFje+3z68cdhFzmmChhzURuIZ8AaMLWfl251evL8UdaBEVAZdL2RkvhROb4ADXRDjdfzv+MQ4L9B3DAYdDHXz9xrc5ICYrriFMgj0cz3OGbtDb6ZlCVXlN5lIw==")
QUESTION_ID = "q-ledger-agent-server"
TZ = ZoneInfo("Asia/Kolkata")
DATA = {"orders": [], "rates": {"USD": 1.0}, "ready": False, "error": None}

def api_get(path, **extra):
    params = {"email": EMAIL, "quizSign": QUIZ_SIGN, "questionId": QUESTION_ID, "path": path}
    params.update(extra)
    response = requests.get(API_URL, params=params, timeout=25)
    response.raise_for_status()
    return response.json()

def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None

def timestamp(row):
    dt = parse_dt(row.get("updated_at"))
    return dt.timestamp() if dt else 0

def load_data():
    try:
        rate_obj = api_get("/rates")
        rates = rate_obj.get("usd_per_unit") or rate_obj.get("rates") or {"USD": 1.0}
        first = api_get("/orders", page=1)
        pages = int(first.get("pages", 1))
        rows = list(first.get("orders", []))
        def fetch(page):
            return api_get("/orders", page=page).get("orders", [])
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())
        # One current row per order id, using latest updated_at as the API instructs.
        current = {}
        for row in rows:
            oid = str(row.get("id", row.get("order_id", "")))
            if oid not in current or timestamp(row) > timestamp(current[oid]):
                current[oid] = row
        DATA.update(orders=list(current.values()), rates=rates, ready=True, error=None)
    except Exception as exc:
        DATA.update(ready=False, error=str(exc))

@app.on_event("startup")
def startup():
    load_data()

class Question(BaseModel):
    question: str

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

def usd(row):
    amount = float(row.get("amount", row.get("total", 0)) or 0)
    currency = str(row.get("currency", "USD")).upper()
    rates = DATA["rates"]
    rate = rates.get(currency)
    if rate is None:
        rate = next((v for k, v in rates.items() if str(k).upper() == currency), None)
    if rate is None:
        if currency == "USD":
            rate = 1
        else:
            raise ValueError("Missing USD conversion rate for " + currency)
    return amount * float(rate)

def money_round(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def values(field):
    return sorted({str(r.get(field)) for r in DATA["orders"] if r.get(field) is not None}, key=len, reverse=True)

def extract_filters(question):
    q = question.lower()
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None
    month = None
    for name, number in sorted(MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            month = number
            break
    region = next((v for v in values("region") if re.search(r"\b" + re.escape(v.lower()) + r"\b", q)), None)
    product = next((v for v in values("product") if re.search(r"\b" + re.escape(v.lower()) + r"\b", q)), None)
    customer = next((v for v in values("customer") if re.search(r"\b" + re.escape(v.lower()) + r"\b", question, re.I)), None)
    return year, month, region, product, customer

def date_range(question):
    q = question.lower()
    year, month, _, _, _ = extract_filters(question)
    # Explicit quarters
    quarter = re.search(r"\b(?:q|quarter)\s*([1-4])\b|\b([1-4])(?:st|nd|rd|th)?\s+quarter\b", q)
    if quarter and year:
        qn = int(quarter.group(1) or quarter.group(2))
        start_month = 1 + 3 * (qn - 1)
        start = datetime(year, start_month, 1, tzinfo=TZ)
        end = datetime(year + (start_month + 2)//12, ((start_month + 2) % 12) + 1, 1, tzinfo=TZ)
        return start, end
    if month and year:
        start = datetime(year, month, 1, tzinfo=TZ)
        end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=TZ)
        return start, end
    if year:
        return datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ)
    return None, None

def filtered_orders(question):
    q = question.lower()
    year, month, region, product, customer = extract_filters(question)
    # Unless explicitly asking for another status or all statuses, use paid orders.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        statuses = {"refunded"}
    elif re.search(r"\bvoid(ed)?\b", q):
        statuses = {"void"}
    elif re.search(r"\b(unpaid|pending)\b", q):
        statuses = {"unpaid", "pending"}
    elif re.search(r"\b(all statuses|all orders|regardless of status|including unpaid|any status)\b", q):
        statuses = None
    else:
        statuses = {"paid"}

    start, end = date_range(question)
    result = []
    for row in DATA["orders"]:
        status = str(row.get("status", "")).lower()
        if statuses is not None and status not in statuses:
            continue
        if region and str(row.get("region", "")).lower() != region.lower():
            continue
        if product and str(row.get("product", "")).lower() != product.lower():
            continue
        if customer and str(row.get("customer", "")).lower() != customer.lower():
            continue
        if start or end:
            dt = parse_dt(row.get("created_at") or row.get("date") or row.get("order_date"))
            if not dt or (start and dt < start) or (end and dt >= end):
                continue
        result.append(row)
    return result

def asks_distinct_customers(q):
    return (
        bool(re.search(r"\b(distinct|unique|different|individual)\s+customers?\b", q))
        or bool(re.search(r"\bhow many customers\b", q))
        or bool(re.search(r"\bnumber of customers\b", q))
        or bool(re.search(r"\bcustomer count\b", q))
        or bool(re.search(r"\bcustomers?\b.*\b(at least one|one or more|any)\b", q))
        or bool(re.search(r"\bhow many different customers\b", q))
    )

def asks_distinct_products(q):
    return bool(re.search(r"\b(distinct|unique|different)\s+products?\b", q)) or bool(re.search(r"\bhow many products\b", q))

def asks_average(q):
    return bool(re.search(r"\b(average|avg|mean|on average)\b", q))

def asks_units(q):
    return bool(re.search(r"\b(units?|quantity|quantities)\b", q))

def asks_money(q):
    return bool(re.search(r"\b(revenue|sales|amount|value|total|sum|usd|dollars?|worth|money|refund amount)\b", q))

def asks_count(q):
    return bool(re.search(r"\b(how many|number of|count|counted)\b", q))

def group_dimension(q):
    # Prefer the entity explicitly being ranked; avoid treating "customers who bought a product"
    # as a request to group by customer unless ranking language is present.
    if re.search(r"\b(per customer|by customer|customer with|customer generated|customer spent|customer revenue|top customer|highest spending customer|best customer)\b", q):
        return "customer"
    if re.search(r"\b(per product|by product|top product|which product|what product|best-selling product|best selling product|product with|highest revenue product|top-selling product|top selling product)\b", q):
        return "product"
    if re.search(r"\b(per region|by region|top region|which region|what region|region with|highest revenue region)\b", q):
        return "region"
    if re.search(r"\b(by month|per month|monthly|month with|which month|top month)\b", q):
        return "month"
    return None

def ranking_requested(q):
    return bool(re.search(r"\b(top|highest|lowest|best|worst|most|least|largest|smallest|greatest|maximum|minimum|ranked|leading)\b", q))

def bucket_value(row, dimension):
    if dimension == "month":
        dt = parse_dt(row.get("created_at") or row.get("date") or row.get("order_date"))
        return dt.strftime("%B %Y") if dt else None
    return row.get(dimension)

def answer_question(question):
    if not DATA["ready"]:
        raise RuntimeError("Ledger data failed to load: " + str(DATA["error"]))
    q = question.lower()
    rows = filtered_orders(question)

    # Counts of entities must be evaluated before generic "how many orders" logic.
    if asks_distinct_customers(q):
        return len({str(r.get("customer")) for r in rows if r.get("customer") not in (None, "")})
    if asks_distinct_products(q):
        return len({str(r.get("product")) for r in rows if r.get("product") not in (None, "")})

    # Ranking questions: e.g. top product by revenue, most units by product, highest-revenue region.
    dimension = group_dimension(q)
    if dimension and ranking_requested(q):
        buckets = {}
        for row in rows:
            key = bucket_value(row, dimension)
            if key in (None, ""):
                continue
            if asks_units(q):
                metric = float(row.get("qty", row.get("quantity", 0)) or 0)
            elif asks_count(q) and not asks_money(q):
                metric = 1
            else:
                metric = usd(row)
            buckets[str(key)] = buckets.get(str(key), 0) + metric
        if not buckets:
            return "No matching data"
        reverse = not bool(re.search(r"\b(lowest|least|smallest|minimum|worst)\b", q))
        return sorted(buckets.items(), key=lambda item: (item[1], item[0]), reverse=reverse)[0][0]

    if asks_average(q):
        if not rows:
            return 0.0
        if asks_units(q):
            return money_round(sum(float(r.get("qty", r.get("quantity", 0)) or 0) for r in rows) / len(rows))
        return money_round(sum(usd(r) for r in rows) / len(rows))

    if asks_units(q):
        return sum(float(r.get("qty", r.get("quantity", 0)) or 0) for r in rows)

    if asks_count(q):
        # "how many orders/transactions" means row/order count; data has been deduplicated by order id.
        if re.search(r"\b(order|orders|transaction|transactions)\b", q):
            return len(rows)
        if re.search(r"\b(customer|customers)\b", q):
            return len({str(r.get("customer")) for r in rows if r.get("customer") not in (None, "")})
        if re.search(r"\b(product|products)\b", q):
            return len({str(r.get("product")) for r in rows if r.get("product") not in (None, "")})
        return len(rows)

    if asks_money(q):
        return money_round(sum(usd(r) for r in rows))

    # Questions phrased as "which/what product ... generated the most revenue"
    if re.search(r"\b(which|what)\s+products?\b", q):
        totals = {}
        for row in rows:
            name = str(row.get("product", ""))
            totals[name] = totals.get(name, 0) + usd(row)
        return max(totals.items(), key=lambda item: item[1])[0] if totals else "No matching data"

    return money_round(sum(usd(r) for r in rows))

@app.get("/")
def health():
    return {"service": "Acme Appliances Ledger Agent", "ready": DATA["ready"],
            "orders_loaded": len(DATA["orders"]), "health": "ok" if DATA["ready"] else "error",
            "error": DATA["error"]}

@app.post("/")
def ask(body: Question):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    try:
        return {"answer": answer_question(body.question)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
