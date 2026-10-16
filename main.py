
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
    return float(
        Decimal(value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def find_period(question):
    q = normalize(question)

    date_range = re.search(
        r"\bfrom\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(?:to|through|until|and)\s+(\d{4}-\d{2}-\d{2})\b",
        q,
    )

    if date_range:
        start = date.fromisoformat(date_range.group(1))
        end = date.fromisoformat(date_range.group(2))
        return start, end

    quarter_match = re.search(r"\bq([1-4])\s+(20\d{2})\b", q)

    if quarter_match:
        quarter = int(quarter_match.group(1))
        year = int(quarter_match.group(2))
        start_month = 3 * (quarter - 1) + 1
        end_month = start_month + 2

        return (
            date(year, start_month, 1),
            date(
                year,
                end_month,
                calendar.monthrange(year, end_month)[1],
            ),
        )

    month_lookup = {
        name.lower(): month
        for month in range(1, 13)
        for name in (
            calendar.month_name[month],
            calendar.month_abbr[month],
        )
    }

    month_pattern = (
        r"\b("
        + "|".join(sorted(month_lookup, key=len, reverse=True))
        + r")\s+(20\d{2})\b"
    )

    match = re.search(month_pattern, q)

    if match:
        month = month_lookup[match.group(1)]
        year = int(match.group(2))

        return (
            date(year, month, 1),
            date(
                year,
                month,
                calendar.monthrange(year, month)[1],
            ),
        )

    year_match = re.search(r"\b(20\d{2})\b", q)

    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)

    return None, None


def match_entity(question, values):
    q = normalize(question)

    for value in sorted(
        {str(value) for value in values if value is not None},
        key=len,
        reverse=True,
    ):
        pattern = r"(?<!\w)" + re.escape(value.lower()) + r"(?!\w)"

        if re.search(pattern, q):
            return value

    return None


def filter_orders(question):
    q = normalize(question)
    selected = list(ORDERS)

    start, end = find_period(q)

    if start is not None:
        selected = [
            order
            for order in selected
            if start <= order_date(order) <= end
        ]

    region = match_entity(
        q,
        [order.get("region") for order in ORDERS],
    )

    if region is not None:
        selected = [
            order
            for order in selected
            if str(order.get("region", "")).lower()
            == region.lower()
        ]

    product = match_entity(
        q,
        [order.get("product") for order in ORDERS],
    )

    if product is not None:
        selected = [
            order
            for order in selected
            if str(order.get("product", "")).lower()
            == product.lower()
        ]

    customer = match_entity(
        q,
        [order.get("customer") for order in ORDERS],
    )

    if customer is not None:
        selected = [
            order
            for order in selected
            if str(order.get("customer", "")).lower()
            == customer.lower()
        ]

    status_match = re.search(
        r"\b(paid|refunded|pending|cancelled|canceled)\b",
        q,
    )

    if status_match:
        status = status_match.group(1)

        if status == "canceled":
            status = "cancelled"

        selected = [
            order
            for order in selected
            if str(order.get("status", "")).lower() == status
        ]

    return selected


def is_count_question(q):
    return bool(
        re.search(
            r"\b(how many|count|number of|number|"
            r"distinct|unique|different)\b",
            q,
        )
    )


def is_revenue_question(q):
    return bool(
        re.search(
            r"\b(revenue|sales|turnover|earned|earn|"
            r"income|profit|brought in|generated|generate|"
            r"how much|total amount)\b",
            q,
        )
    )


def is_ranking_question(q):
    return bool(
        re.search(
            r"\b(highest|lowest|top|most|least|"
            r"best-selling|bestselling|largest|smallest)\b",
            q,
        )
    )


def is_distinct_customer_question(q):
    return (
        "customer" in q
        and is_count_question(q)
    )


def customer_count(orders):
    return len({
        str(order["customer"]).strip()
        for order in orders
        if order.get("customer") is not None
        and str(order["customer"]).strip()
    })


def total_revenue(orders):
    paid_orders = [
        order for order in orders
        if str(order.get("status", "")).lower() == "paid"
    ]

    return money(
        sum(
            (amount_usd(order) for order in paid_orders),
            Decimal("0"),
        )
    )


def total_refunds(orders):
    refunded_orders = [
        order for order in orders
        if str(order.get("status", "")).lower() == "refunded"
    ]

    return money(
        sum(
            (amount_usd(order) for order in refunded_orders),
            Decimal("0"),
        )
    )


def count_units(orders):
    return sum(
        int(order.get("qty", 0))
        for order in orders
        if str(order.get("status", "")).lower() == "paid"
    )


def ranking_result(question, orders):
    q = normalize(question)

    if "customer" in q:
        group_field = "customer"
    elif "region" in q:
        group_field = "region"
    elif "product" in q:
        group_field = "product"
    else:
        return None

    paid_orders = [
        order for order in orders
        if str(order.get("status", "")).lower() == "paid"
    ]

    quantity_mode = bool(
        re.search(r"\b(quantity|units|volume)\b", q)
    )

    totals = defaultdict(Decimal)

    for order in paid_orders:
        group_value = order.get(group_field)

        if group_value is None:
            continue

        if quantity_mode:
            totals[str(group_value)] += Decimal(
                str(order.get("qty", 0))
            )
        else:
            totals[str(group_value)] += amount_usd(order)

    if not totals:
        return "No matching results"

    if re.search(r"\b(lowest|least|smallest)\b", q):
        return min(totals, key=totals.get)

    return max(totals, key=totals.get)


def aggregate(question):
    q = normalize(question)
    orders = filter_orders(q)

    # Customer counts must be handled before generic counts.
    if is_distinct_customer_question(q):
        return customer_count(orders)

    # Refund questions use refunded orders, not paid revenue.
    if "refund" in q:
        if is_count_question(q):
            return sum(
                1 for order in orders
                if str(order.get("status", "")).lower() == "refunded"
            )

        return total_refunds(orders)

    # Rankings must be handled before general revenue questions.
    if is_ranking_question(q):
        result = ranking_result(q, orders)

        if result is not None:
            return result

    # Other counts.
    if is_count_question(q):
        if "paid" in q:
            return sum(
                1 for order in orders
                if str(order.get("status", "")).lower() == "paid"
            )

        if "product" in q and "order" not in q:
            return len({
                order.get("product")
                for order in orders
                if order.get("product")
            })

        if "order" in q:
            return len(orders)

        return len(orders)

    # Revenue: only paid orders contribute.
    if is_revenue_question(q):
        return total_revenue(orders)

    # Units sold.
    if re.search(r"\b(quantity|units sold|number of units)\b", q):
        return count_units(orders)

    # Average paid order value.
    if re.search(r"\b(average|avg|mean)\b", q):
        paid_orders = [
            order for order in orders
            if str(order.get("status", "")).lower() == "paid"
        ]

        if not paid_orders:
            return 0

        total = sum(
            (amount_usd(order) for order in paid_orders),
            Decimal("0"),
        )

        return money(total / len(paid_orders))

    return {
        "error": "Question type not recognized",
    }


@app.get("/")
def health_check():
    return {
        "status": "running",
        "orders_loaded": len(ORDERS),
    }


@app.post("/")
def answer_question(request: QuestionRequest):
    question = request.question.strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="Question cannot be empty",
        )

    try:
        answer = aggregate(question)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"answer": answer}
