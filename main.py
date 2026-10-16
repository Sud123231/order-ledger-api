import json
import re
import calendar
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "latest-orders.json"
IST = ZoneInfo("Asia/Kolkata")

USD_PER_UNIT = {
    "USD": 1.0,
    "EUR": 1.1,
    "INR": 0.01126,
}

with DATA_FILE.open("r", encoding="utf-8-sig") as file:
    ORDERS = json.load(file)

app = FastAPI(title="Acme Appliances Order Ledger API")


class QuestionRequest(BaseModel):
    question: str


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def order_date(order):
    timestamp = datetime.fromisoformat(
        order["created_at"].replace("Z", "+00:00")
    )
    return timestamp.astimezone(IST).date()


def amount_usd(order):
    currency = order.get("currency", "USD").upper()
    rate = USD_PER_UNIT.get(currency)
    if rate is None:
        raise ValueError(f"Unknown currency: {currency}")
    return float(order.get("amount", 0)) * rate


def find_period(question):
    q = normalize(question)
    start = None
    end = None

    month_lookup = {
        name.lower(): number
        for number in range(1, 13)
        for name in (
            calendar.month_name[number],
            calendar.month_abbr[number],
        )
    }

    month_pattern = (
        r"\b(" + "|".join(sorted(month_lookup, key=len, reverse=True))
        + r")\s+(20\d{2})\b"
    )
    match = re.search(month_pattern, q)

    if match:
        month = month_lookup[match.group(1)]
        year = int(match.group(2))
        start = date(year, month, 1)
        end = date(
            year,
            month,
            calendar.monthrange(year, month)[1],
        )
        return start, end

    quarter_match = re.search(r"\bq([1-4])\s+(20\d{2})\b", q)
    if quarter_match:
        quarter = int(quarter_match.group(1))
        year = int(quarter_match.group(2))
        month = 3 * (quarter - 1) + 1
        start = date(year, month, 1)
        end_month = month + 2
        end = date(
            year,
            end_month,
            calendar.monthrange(year, end_month)[1],
        )
        return start, end

    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)

    range_match = re.search(
        r"\bfrom\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(?:to|through|until|and)\s+(\d{4}-\d{2}-\d{2})\b",
        q,
    )
    if range_match:
        return (
            date.fromisoformat(range_match.group(1)),
            date.fromisoformat(range_match.group(2)),
        )

    return start, end


def filter_orders(question):
    q = normalize(question)
    selected = list(ORDERS)

    start, end = find_period(q)
    if start or end:
        selected = [
            order for order in selected
            if (start is None or order_date(order) >= start)
            and (end is None or order_date(order) <= end)
        ]

    regions = sorted(
        {str(o.get("region", "")) for o in ORDERS if o.get("region")},
        key=len,
        reverse=True,
    )
    for region in regions:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            selected = [
                order for order in selected
                if str(order.get("region", "")).lower() == region.lower()
            ]
            break

    products = sorted(
        {str(o.get("product", "")) for o in ORDERS if o.get("product")},
        key=len,
        reverse=True,
    )
    for product in products:
        if product.lower() in q:
            selected = [
                order for order in selected
                if str(order.get("product", "")).lower() == product.lower()
            ]
            break

    customers = sorted(
        {str(o.get("customer", "")) for o in ORDERS if o.get("customer")},
        key=len,
        reverse=True,
    )
    for customer in customers:
        if customer.lower() in q:
            selected = [
                order for order in selected
                if str(order.get("customer", "")).lower() == customer.lower()
            ]
            break

    statuses = {
        "paid": ["paid"],
        "refunded": ["refunded"],
        "cancelled": ["cancelled", "canceled"],
        "pending": ["pending"],
    }
    for status, words in statuses.items():
        if any(re.search(r"\b" + re.escape(word) + r"\b", q) for word in words):
            selected = [
                order for order in selected
                if str(order.get("status", "")).lower() == status
            ]
            break

    return selected


def money(value):
    return round(value + 0.000000001, 2)


def aggregate(question):
    q = normalize(question)
    orders = filter_orders(q)

    if not orders:
        return 0

    paid_orders = [
        order for order in orders
        if str(order.get("status", "")).lower() == "paid"
    ]
    refunded_orders = [
        order for order in orders
        if str(order.get("status", "")).lower() == "refunded"
    ]

    if "refund" in q:
        if any(word in q for word in ["how many", "number of", "count"]):
            return len(refunded_orders)
        return money(sum(amount_usd(o) for o in refunded_orders))

    if any(word in q for word in ["average", "avg", "mean"]):
        if "order" in q or "transaction" in q:
            if not paid_orders:
                return 0
            return money(
                sum(amount_usd(o) for o in paid_orders) / len(paid_orders)
            )

    if any(word in q for word in ["how many", "number of", "count"]):
        if "distinct customer" in q or "unique customer" in q:
            return len({
              order.get("customer")
              for order in orders
              if order.get("customer")
              and str(order.get("status", "")).lower() == "paid"
            })

        if "customer" in q:
            return len({
            order.get("customer")
            for order in orders
            if order.get("customer")
        })

        if "product" in q and "order" not in q:
            return len({
            order.get("product")
            for order in orders
            if order.get("product")
        })

        if "paid" in q or "revenue" in q:
           return len(paid_orders)

        return len(orders)

    if any(word in q for word in ["top", "highest", "most", "best-selling", "bestselling"]):
            group_field = None
            if "customer" in q:
                group_field = "customer"
            elif "region" in q:
                group_field = "region"
            elif "product" in q:
                group_field = "product"
    
            if group_field:
                totals = defaultdict(float)
                for order in paid_orders:
                    totals[order.get(group_field, "Unknown")] += amount_usd(order)
    
                if not totals:
                    return "No matching results"
    
                if "quantity" in q or "units" in q:
                    totals = defaultdict(float)
                    for order in paid_orders:
                        totals[order.get(group_field, "Unknown")] += int(
                            order.get("qty", 0)
                        )
    
                return max(totals, key=totals.get)
    
    if any(
    word in q
    for word in [
        "revenue",
        "sales",
        "turnover",
        "earned",
        "earn",
        "how much did",
        "how much has",
        "how much was",
        "how much in",
        "income",
        "brought in",
        "generate",
        "generated",
    ]
):
        return money(sum(amount_usd(o) for o in paid_orders))

    if "quantity" in q or "units sold" in q:
        return sum(int(o.get("qty", 0)) for o in paid_orders)

    if "customer" in q and any(word in q for word in ["total", "revenue", "sales"]):
        return money(sum(amount_usd(o) for o in paid_orders))

    if "order" in q:
        return len(orders)

    return {
        "error": (
            "I could not confidently interpret this question. "
            "Try asking about revenue, refunds, order counts, products, "
            "customers, regions, or a date range."
        )
    }


@app.get("/")
def health_check():
    return {"status": "running", "orders_loaded": len(ORDERS)}


@app.post("/")
def answer_question(request: QuestionRequest):
    question = request.question.strip()

    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        answer = aggregate(question)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"answer": answer}
