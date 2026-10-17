
import json
import re
import calendar
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "latest-orders.json"
IST = ZoneInfo("Asia/Kolkata")

USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.1"),
    "INR": Decimal("0.01126"),
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
    currency = str(order.get("currency", "USD")).upper()
    if currency not in USD_PER_UNIT:
        raise ValueError(f"Unknown currency: {currency}")
    return Decimal(str(order.get("amount", 0))) * USD_PER_UNIT[currency]


def money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def find_period(question):
    q = normalize(question)

    date_range = re.search(
        r"\bfrom\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(?:to|through|until|and)\s+(\d{4}-\d{2}-\d{2})\b",
        q,
    )
    if date_range:
        return (
            date.fromisoformat(date_range.group(1)),
            date.fromisoformat(date_range.group(2)),
        )

    quarter = re.search(r"\bq([1-4])\s+(20\d{2})\b", q)
    if quarter:
        qtr, year = int(quarter.group(1)), int(quarter.group(2))
        month = 3 * (qtr - 1) + 1
        end_month = month + 2
        return (
            date(year, month, 1),
            date(year, end_month, calendar.monthrange(year, end_month)[1]),
        )

    months = {
        name.lower(): number
        for number in range(1, 13)
        for name in (calendar.month_name[number], calendar.month_abbr[number])
    }
    month_pattern = (
        r"\b(" + "|".join(sorted(months, key=len, reverse=True))
        + r")\s+(20\d{2})\b"
    )
    match = re.search(month_pattern, q)
    if match:
        year, month = int(match.group(2)), months[match.group(1)]
        return (
            date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]),
        )

    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)

    return None, None


def filter_orders(question):
    q = normalize(question)
    selected = list(ORDERS)

    start, end = find_period(q)
    if start is not None:
        selected = [
            o for o in selected
            if start <= order_date(o) <= end
        ]

    regions = sorted(
        {str(o["region"]) for o in ORDERS if o.get("region")},
        key=len,
        reverse=True,
    )
    for region in regions:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            selected = [
                o for o in selected
                if str(o.get("region", "")).lower() == region.lower()
            ]
            break

    products = sorted(
        {str(o["product"]) for o in ORDERS if o.get("product")},
        key=len,
        reverse=True,
    )
    for product in products:
        if re.search(r"\b" + re.escape(product.lower()) + r"\b", q):
            selected = [
                o for o in selected
                if str(o.get("product", "")).lower() == product.lower()
            ]
            break

    customers = sorted(
        {str(o["customer"]) for o in ORDERS if o.get("customer")},
        key=len,
        reverse=True,
    )
    for customer in customers:
        if re.search(r"\b" + re.escape(customer.lower()) + r"\b", q):
            selected = [
                o for o in selected
                if str(o.get("customer", "")).lower() == customer.lower()
            ]
            break

    status_match = re.search(
        r"\b(paid|refunded|pending|cancelled|canceled)\b", q
    )
    if status_match:
        status = status_match.group(1)
        if status == "canceled":
            status = "cancelled"
        selected = [
            o for o in selected
            if str(o.get("status", "")).lower() == status
        ]

    return selected


def is_count_question(q):
    return any(
        phrase in q
        for phrase in (
            "how many", "number of", "count", "distinct", "unique",
            "number",
        )
    )


def is_revenue_question(q):
    return any(
        phrase in q
        for phrase in (
            "revenue", "sales", "turnover", "earned", "earn",
            "income", "brought in", "generated", "generate",
            "how much", "total amount",
        )
    )


def aggregate(question):
    q = normalize(question)
    orders = filter_orders(q)

    paid_orders = [
        o for o in orders
        if str(o.get("status", "")).lower() == "paid"
    ]
    refunded_orders = [
        o for o in orders
        if str(o.get("status", "")).lower() == "refunded"
    ]

    # Refund questions take precedence over general amount questions.
    if "refund" in q:
        if is_count_question(q):
            return len(refunded_orders)
        return money(sum((amount_usd(o) for o in refunded_orders), Decimal(0)))

    # Distinct customer counts must always deduplicate customer IDs.
    if "customer" in q and is_count_question(q):
        customer_orders = orders
        if "paid" in q or "bought" in q or "purchased" in q:
            customer_orders = paid_orders

        return len({
            str(o["customer"])
            for o in customer_orders
            if o.get("customer")
        })

    # Counts of orders, products, or other records.
    if is_count_question(q):
        if "paid" in q:
            return len(paid_orders)

        if "product" in q and "order" not in q:
            return len({
                o.get("product") for o in orders if o.get("product")
            })

        if "order" in q:
            return len(orders)

        return len(orders)

    # Ranking questions must run before the general revenue calculation.
    if any(
        word in q
        for word in ("highest", "lowest", "top", "most", "best-selling",
                     "bestselling", "least")
    ):
        group_field = None
        if "customer" in q:
            group_field = "customer"
        elif "region" in q:
            group_field = "region"
        elif "product" in q:
            group_field = "product"

        if group_field:
            totals = defaultdict(Decimal)
            quantity_mode = "quantity" in q or "units" in q

            for o in paid_orders:
                key = o.get(group_field)
                if key is None:
                    continue

                if quantity_mode:
                    totals[key] += Decimal(str(o.get("qty", 0)))
                else:
                    totals[key] += amount_usd(o)

            if not totals:
                return "No matching results"

            if "lowest" in q or "least" in q:
                return min(totals, key=totals.get)

            return max(totals, key=totals.get)

    # Revenue includes paid orders only.
    if is_revenue_question(q):
        return money(sum((amount_usd(o) for o in paid_orders), Decimal(0)))

    if "quantity" in q or "units sold" in q:
        return sum(int(o.get("qty", 0)) for o in paid_orders)

    if any(word in q for word in ("average", "avg", "mean")):
        if not paid_orders:
            return 0
        return money(
            sum((amount_usd(o) for o in paid_orders), Decimal(0))
            / len(paid_orders)
        )

    return {
        "error": "Question type not recognized",
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
        return {"answer": aggregate(question)}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))
