import os
import re
import calendar
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent", version="3.0")
API_URL = "https://exam.sanand.workers.dev/questionData"
EMAIL = os.getenv("EXAM_EMAIL", "24f2005619@ds.study.iitm.ac.in")
QUIZ_SIGN = os.getenv("EXAM_QUIZ_SIGN", "ZRIwO+aVfPd8nxqco0fUps+Z1wEtWTKAdMjkoN1ZFipYfSsGTLYUYVer86zVtT7pS/Yz2G6eLDGk24ZDrH+xSUUH5sdstn2osRweL/nGFDe+gFkijDtkMhp+24iOW3XfVUr+FH3X1DYapA+KUKqPmXkj5KMzB9xtEQSrkUxFnFvM0aN11atV+eMXeO+hJMsy0VlQVkHE366Hbf121lt1+xA8Z2ZBCFje+3z68cdhFzmmChhzURuIZ8AaMLWfl251evL8UdaBEVAZdL2RkvhROb4ADXRDjdfzv+MQ4L9B3DAYdDHXz9xrc5ICYrriFMgj0cz3OGbtDb6ZlCVXlN5lIw==")
QUESTION_ID = "q-ledger-agent-server"
TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()
DATA: dict[str, Any] = {"orders": [], "rates": {"USD": 1.0}, "ready": False, "error": None, "raw_rows": 0, "pages": 0}


def api_get(path: str, **extra):
    params = {"email": EMAIL, "quizSign": QUIZ_SIGN, "questionId": QUESTION_ID, "path": path}
    params.update(extra)
    last_error = None
    for attempt in range(4):
        try:
            resp = SESSION.get(API_URL, params=params, timeout=(8, 35))
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"Unexpected API response for {path}: expected an object")
            return data
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"API request failed for {path}: {last_error}")


def get_list(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    # Some paginated APIs wrap the payload under data/result.
    for outer in ("data", "result", "results"):
        nested = payload.get(outer)
        if isinstance(nested, dict):
            for key in keys:
                if isinstance(nested.get(key), list):
                    return nested[key]
    raise ValueError(f"Could not find list {keys} in API response; keys={list(payload)}")


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def current_timestamp(row):
    dt = parse_dt(row.get("updated_at"))
    return dt.timestamp() if dt else float("-inf")


def load_data():
    try:
        rate_payload = api_get("/rates")
        # Preserve common API response shapes, but never silently discard non-USD rates.
        rates = rate_payload.get("usd_per_unit") or rate_payload.get("rates")
        if rates is None and isinstance(rate_payload.get("data"), dict):
            rates = rate_payload["data"].get("usd_per_unit") or rate_payload["data"].get("rates")
        if not isinstance(rates, dict):
            raise ValueError(f"Unrecognized /rates response structure: {list(rate_payload)}")
        rates = {str(k).upper(): float(v) for k, v in rates.items() if _is_number(v)}
        rates["USD"] = 1.0

        first = api_get("/orders", page=1)
        all_rows = get_list(first, "orders", "items", "results", "data")
        page_count = int(first.get("pages", first.get("total_pages", 1)) or 1)
        # Sequential pagination avoids rate-limit spikes and ensures every page is checked.
        for page in range(2, page_count + 1):
            payload = api_get("/orders", page=page)
            all_rows.extend(get_list(payload, "orders", "items", "results", "data"))

        # Keep exactly one current version per order ID, as specified by the ledger metadata.
        current = {}
        for row in all_rows:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("id", row.get("order_id", ""))).strip()
            if not oid:
                continue
            if oid not in current or current_timestamp(row) > current_timestamp(current[oid]):
                current[oid] = row
        if not current:
            raise ValueError("No valid order records were returned by /orders")
        DATA.update(orders=list(current.values()), rates=rates, ready=True, error=None,
                    raw_rows=len(all_rows), pages=page_count)
    except Exception as exc:
        DATA.update(ready=False, error=str(exc))


def _is_number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


@app.on_event("startup")
def startup():
    load_data()


class Question(BaseModel):
    question: str


MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def money_round(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def usd(row):
    amount = row.get("amount", row.get("total", row.get("value", 0)))
    amount = float(amount or 0)
    currency = str(row.get("currency", "USD")).upper()
    rate = DATA["rates"].get(currency)
    if rate is None:
        raise ValueError(f"Missing USD conversion rate for currency {currency}")
    return amount * float(rate)


def catalog_values(field):
    vals = {str(r.get(field)).strip() for r in DATA["orders"] if r.get(field) not in (None, "")}
    return sorted(vals, key=lambda x: len(norm(x)), reverse=True)


def find_catalog_value(question, field):
    q = norm(question)
    for value in catalog_values(field):
        v = norm(value)
        # Flexible spaces allow names such as "Rice Cooker" to match normalized variants.
        if v and re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", q):
            return value
    return None


def query_dates(question):
    q = question.lower()
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None
    month = None
    for name, num in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            month = num
            break
    # Explicit date range: between/from DATE and DATE, ISO or common month/day/year forms.
    iso_dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if len(iso_dates) >= 2:
        try:
            return datetime.fromisoformat(iso_dates[0]).replace(tzinfo=TZ), datetime.fromisoformat(iso_dates[1]).replace(tzinfo=TZ), None, None
        except ValueError:
            pass
    quarter = re.search(r"\b(?:q|quarter)\s*([1-4])\b|\b([1-4])(?:st|nd|rd|th)?\s+quarter\b", q)
    if quarter and year:
        qn = int(quarter.group(1) or quarter.group(2))
        sm = 1 + 3 * (qn - 1)
        return datetime(year, sm, 1, tzinfo=TZ), datetime(year + (sm + 2)//12, ((sm + 2) % 12) + 1, 1, tzinfo=TZ), year, None
    if year and month:
        return datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=TZ).replace(day=1) if False else datetime(year, month, 1, tzinfo=TZ), datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=TZ), year, month
    if year:
        return datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ), year, None
    return None, None, None, month


def classify(question):
    q = norm(question)
    is_revenue = bool(re.search(r"\b(revenue|sales|sales value|amount|monetary value|dollars?|usd|worth|spend|spent|turnover|earn|earns|earned|earning|earnings|generated|income|profit)\b", q) or re.search(r"\b(refund amount|amount refunded|value of refunds|how much .*refund|how much .*refunded)\b", q))
    average = bool(re.search(r"\b(average|avg|mean|on average)\b", q))
    units = bool(re.search(r"\b(units?|quantity|quantities|items sold)\b", q))
    count = bool(re.search(r"\b(how many|number of|count|counted)\b", q))
    distinct_customer = bool(re.search(r"\b(distinct|unique|different|individual) customers?\b", q) or
                            re.search(r"\bhow many customers\b", q) or
                            re.search(r"\bcustomer count\b", q) or
                            re.search(r"\bcustomers?\b.*\bat least one\b", q))
    distinct_product = bool(re.search(r"\b(distinct|unique|different) products?\b", q) or re.search(r"\bhow many products\b", q))
    ranking = bool(re.search(r"\b(top|highest|lowest|best|worst|most|least|largest|smallest|greatest|maximum|minimum|ranked|leading|best selling|top selling)\b", q))
    # Explicit status wins. Revenue is paid-only by business rule; ordinary order counts are not.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        status = {"refunded"}
    elif re.search(r"\bvoid(ed)?\b", q):
        status = {"void"}
    elif re.search(r"\b(unpaid|pending)\b", q):
        status = {"unpaid", "pending"}
    elif re.search(r"\b(all statuses|all orders|regardless of status|including unpaid|any status)\b", q):
        status = None
    elif re.search(r"\b(paid|revenue|sales|turnover|sold|selling|earn|earns|earned|earning|earnings|generated|income|profit)\b", q):
        status = {"paid"}
    else:
        status = None
    return q, is_revenue, average, units, count, distinct_customer, distinct_product, ranking, status


def filtered_rows(question, metric_is_revenue=False):
    q, is_revenue, average, units, count, distinct_customer, distinct_product, ranking, status = classify(question)
    start, end, year, month = query_dates(question)
    region = find_catalog_value(question, "region")
    product = find_catalog_value(question, "product")
    customer = find_catalog_value(question, "customer")
    # For money questions, paid-only is the ledger's explicit revenue rule unless a status was explicitly requested.
    if metric_is_revenue and not re.search(r"\b(refund|refunded|refunds|void|unpaid|pending|all statuses|all orders|regardless of status|including unpaid|any status)\b", q):
        status = {"paid"}
    output = []
    for row in DATA["orders"]:
        if status is not None and norm(row.get("status")) not in status:
            continue
        if region and norm(row.get("region")) != norm(region):
            continue
        if product and norm(row.get("product")) != norm(product):
            continue
        if customer and norm(row.get("customer")) != norm(customer):
            continue
        dt = parse_dt(row.get("created_at") or row.get("date") or row.get("order_date"))
        if start and (not dt or dt < start):
            continue
        if end and (not dt or dt >= end):
            continue
        # Month without year: filter the month number, not the year.
        if month and not year and (not dt or dt.month != month):
            continue
        output.append(row)
    return output


def grouping_dimension(question):
    q = norm(question)
    if re.search(r"\b(per customer|by customer|top customer|highest spending customer|customer with|customer generated|customer spent|customer revenue)\b", q):
        return "customer"
    if re.search(r"\b(per product|by product|top product|which product|what product|best selling product|top selling product|product with|highest revenue product|best seller)\b", q):
        return "product"
    if re.search(r"\b(per region|by region|top region|which region|what region|region with|highest revenue region)\b", q):
        return "region"
    if re.search(r"\b(by month|per month|monthly|month with|which month|top month)\b", q):
        return "month"
    return None


def row_metric(row, question):
    q, is_revenue, average, units, count, dc, dp, ranking, status = classify(question)
    if units:
        return float(row.get("qty", row.get("quantity", 0)) or 0)
    if is_revenue:
        return usd(row)
    return 1.0


def answer_question(question):
    if not DATA["ready"]:
        raise RuntimeError("Ledger data failed to load: " + str(DATA["error"]))
    q, is_revenue, average, units, count, distinct_customer, distinct_product, ranking, status = classify(question)
    rows = filtered_rows(question, metric_is_revenue=is_revenue)

    if distinct_customer:
        return len({str(r.get("customer")).strip() for r in rows if r.get("customer") not in (None, "")})
    if distinct_product:
        return len({norm(r.get("product")) for r in rows if r.get("product") not in (None, "")})

    dim = grouping_dimension(question)
    if dim and ranking:
        totals = {}
        for row in rows:
            if dim == "month":
                dt = parse_dt(row.get("created_at") or row.get("date") or row.get("order_date"))
                key = dt.strftime("%B %Y") if dt else None
            else:
                key = row.get(dim)
            if key in (None, ""):
                continue
            totals[str(key)] = totals.get(str(key), 0.0) + row_metric(row, question)
        if not totals:
            return "No matching data"
        reverse = not bool(re.search(r"\b(lowest|least|smallest|minimum|worst)\b", q))
        # Tie-break alphabetically for deterministic answers.
        return sorted(totals.items(), key=lambda kv: ((-kv[1]) if reverse else kv[1], kv[0].lower()))[0][0]

    if average:
        if not rows:
            return 0.0
        if units:
            return money_round(sum(float(r.get("qty", r.get("quantity", 0)) or 0) for r in rows) / len(rows))
        if is_revenue:
            return money_round(sum(usd(r) for r in rows) / len(rows))
        return money_round(len(rows) / 1.0)

    if units:
        return money_round(sum(float(r.get("qty", r.get("quantity", 0)) or 0) for r in rows))
    if count:
        if re.search(r"\b(customer|customers)\b", q):
            return len({str(r.get("customer")).strip() for r in rows if r.get("customer") not in (None, "")})
        if re.search(r"\b(product|products)\b", q):
            return len({norm(r.get("product")) for r in rows if r.get("product") not in (None, "")})
        # Count orders, including non-paid statuses unless the question explicitly restricts status.
        return len(rows)
    if is_revenue:
        return money_round(sum(usd(r) for r in rows))
    if re.search(r"\b(which|what) products?\b", q):
        totals = {}
        for row in rows:
            key = str(row.get("product", ""))
            totals[key] = totals.get(key, 0.0) + usd(row)
        return max(totals.items(), key=lambda kv: kv[1])[0] if totals else "No matching data"
    return len(rows)


@app.get("/")
def health():
    return {"service": "Acme Appliances Ledger Agent", "ready": DATA["ready"],
            "orders_loaded": len(DATA["orders"]), "raw_rows": DATA["raw_rows"],
            "pages_loaded": DATA["pages"], "health": "ok" if DATA["ready"] else "error",
            "error": DATA["error"], "currencies": sorted(DATA["rates"].keys())}


@app.post("/")
def ask(body: Question):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    try:
        return {"answer": answer_question(body.question)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
