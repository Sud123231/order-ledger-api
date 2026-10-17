
import json
import re
import calendar
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "latest-orders.json"
IST = ZoneInfo("Asia/Kolkata")

USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.1"),
    "INR": Decimal("0.01126"),
}

VALID_STATUSES = {
    "paid", "refunded", "pending", "cancelled", "void"
}


def normalize(text):
    text = str(text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def parse_timestamp(value):
    timestamp = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)

    return timestamp


def order_date(order):
    return parse_timestamp(order["created_at"]).astimezone(IST).date()


def load_orders():
    with DATA_FILE.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("Order data must be a JSON list")

    latest_by_id = {}

    for order in data:
        if not isinstance(order, dict):
            continue

        order_id = order.get("id")

        if not order_id:
            continue

        previous = latest_by_id.get(str(order_id))

        if previous is None:
            latest_by_id[str(order_id)] = order
            continue

        current_time = parse_timestamp(order["updated_at"])
        previous_time = parse_timestamp(previous["updated_at"])

        if current_time > previous_time:
            latest_by_id[str(order_id)] = order

    return list(latest_by_id.values())


ORDERS = load_orders()

app = FastAPI(title="Acme Appliances Order Ledger API")


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


def amount_usd(order):
    currency = str(order.get("currency", "USD")).upper()

    if currency not in USD_PER_UNIT:
        raise ValueError(f"Unknown currency: {currency}")

    amount = Decimal(str(order.get("amount", 0)))
    return amount * USD_PER_UNIT[currency]


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

        if end < start:
            raise ValueError("End date must not precede start date")

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

    months = {
        name.casefold(): month
        for month in range(1, 13)
        for name in (
            calendar.month_name[month],
            calendar.month_abbr[month],
        )
    }

    month_pattern = (
        r"\b("
        + "|".join(sorted(months, key=len, reverse=True))
        + r")\s+(20\d{2})\b"
    )

    match = re.search(month_pattern, q)

    if match:
        month = months[match.group(1)]
        year = int(match.group(2))

        return (
            date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]),
        )

    year_match = re.search(r"\b(20\d{2})\b", q)

    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)

    return None, None


def find_entity(question, field):
    q = normalize(question)

    values = {
        str(order[field]).strip()
        for order in ORDERS
        if order.get(field) is not None
        and str(order[field]).strip()
    }

    for value in sorted(values, key=len, reverse=True):
        pattern = r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)"

        if re.search(pattern, q):
            return value

    return None


def filter_orders(question):
    q = normalize(question)
    selected = list(ORDERS)

    start, end = find_period(q)

    if start is not None:
        selected = [
            order for order in selected
            if start <= order_date(order) <= end
        ]

    for field in ("region", "product", "customer"):
        entity = find_entity(q, field)

        if entity is not None:
            selected = [
                order for order in selected
                if normalize(order.get(field, "")) == normalize(entity)
            ]

    status_match = re.search(
        r"\b(paid|refunded|pending|cancelled|canceled|void)\b",
        q,
    )

    if status_match:
        status = status_match.group(1)

        if status == "canceled":
            status = "cancelled"

        selected = [
            order for order in selected
            if normalize(order.get("status", "")) == status
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


def is_average_question(q):
    return bool(
        re.search(
            r"\b(average|avg|mean|on average)\b",
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


def is_revenue_question(q):
    return bool(
        re.search(
            r"\b(revenue|sales|turnover|earned|earn|"
            r"income|profit|brought in|generated|generate|"
            r"how much|total amount|worth)\b",
            q,
        )
    )


def customer_count(orders):
    return len({
        normalize(order["customer"])
        for order in orders
        if order.get("customer") is not None
        and normalize(order["customer"])
    })


def sum_usd(orders):
    return money(
        sum(
            (amount_usd(order) for order in orders),
            Decimal("0"),
        )
    )


def average_order_value(orders):
    paid_orders = [
        order for order in orders
        if normalize(order.get("status", "")) == "paid"
    ]

    if not paid_orders:
        return 0.0

    total = sum(
        (amount_usd(order) for order in paid_orders),
        Decimal("0"),
    )

    return money(total / len(paid_orders))


def count_units(orders):
    return sum(
        int(order.get("qty", 0))
        for order in orders
        if normalize(order.get("status", "")) == "paid"
    )


def ranking_result(question, orders):
    q = normalize(question)

    if "customer" in q:
        field = "customer"
    elif "region" in q:
        field = "region"
    elif "product" in q:
        field = "product"
    else:
        return None

    paid_orders = [
        order for order in orders
        if normalize(order.get("status", "")) == "paid"
    ]

    quantity_mode = bool(
        re.search(r"\b(quantity|units|volume)\b", q)
    )

    totals = defaultdict(Decimal)

    for order in paid_orders:
        key = order.get(field)

        if key is None:
            continue

        if quantity_mode:
            totals[str(key)] += Decimal(str(order.get("qty", 0)))
        else:
            totals[str(key)] += amount_usd(order)

    if not totals:
        return "No matching results"

    if re.search(r"\b(lowest|least|smallest)\b", q):
        return min(totals, key=totals.get)

    return max(totals, key=totals.get)


def aggregate(question):
    q = normalize(question)
    orders = filter_orders(q)

    paid_orders = [
        order for order in orders
        if normalize(order.get("status", "")) == "paid"
    ]

    refunded_orders = [
        order for order in orders
        if normalize(order.get("status", "")) == "refunded"
    ]

    # 1. Refunds
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        if is_count_question(q):
            return len(refunded_orders)

        return sum_usd(refunded_orders)

    # 2. Distinct customer counts
    if "customer" in q and is_count_question(q):
        return customer_count(orders)

    # 3. Average must precede general revenue detection.
    # Average order value uses paid orders only.
    if is_average_question(q):
        if re.search(r"\b(customer|per customer)\b", q):
            return (
                money(
                    sum(
                        (amount_usd(o) for o in paid_orders),
                        Decimal("0"),
                    ) / customer_count(paid_orders)
                )
                if customer_count(paid_orders)
                else 0.0
            )

        return average_order_value(orders)

    # 4. Ranking
    if is_ranking_question(q):
        result = ranking_result(q, orders)

        if result is not None:
            return result

    # 5. Counts
    if is_count_question(q):
        if "paid" in q:
            return len(paid_orders)

        if "product" in q and "order" not in q:
            return len({
                order.get("product")
                for order in orders
                if order.get("product")
            })

        if "order" in q:
            return len(orders)

        return len(orders)

    # 6. Units sold
    if re.search(r"\b(quantity|units sold|number of units)\b", q):
        return count_units(orders)

    # 7. Revenue and other monetary totals
    if is_revenue_question(q):
        return sum_usd(paid_orders)

    return {"error": "Question type not recognized"}


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
        return {"answer": aggregate(question)}
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))
