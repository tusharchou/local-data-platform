"""Deterministic synthetic data for a small restaurant chain: the dataset every layer of the restaurant example uses.

Six restaurants in six cities, a menu, 150 customers, their orders from January to early August 2026, the order
lines, and some reviews. The same ``seed`` always gives the same files, and nothing here needs a network or a
random-number library beyond :mod:`random`.

The orders arrive the way a restaurant system exports them, in dated batches:

| Batch | What it holds |
|---|---|
| ``batch1`` | every order from January to June 2026 |
| ``batch2`` | every order of July 2026, and five late orders from the end of June: created on time, but only |
|  | synced in July, so their ``updated_at`` is in July |
| ``2026-08-01`` to ``2026-08-07`` | one drop file a day, for the later milestones that schedule a load for each day |

Each order has an ``updated_at``, the time the restaurant system last changed it. A model can load only what is
new by keeping the orders whose ``updated_at`` is later than the newest one it has, and the late orders show why
that, and not ``order_ts``, is the column to use. The late June orders belong to customers who looked as if they had
stopped ordering in June, so when they arrive, June's churn falls.

The restaurants are written as an Excel workbook (``restaurants.xlsx``, with a title row above the header), as an
export from a restaurant system often is, and everything else as CSV. Run it on its own to look at the files::

    python examples/restaurant/generate.py --out /tmp/restaurant_data
"""

import argparse
import csv
import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path

SEED = 7
CUSTOMERS = 150
LATE_ORDERS = 5
FIRST_DAY = dt.date(2026, 1, 1)
BATCH1_END = dt.date(2026, 6, 30)
BATCH2_END = dt.date(2026, 7, 31)
DROP_DAYS = tuple(dt.date(2026, 8, day) for day in range(1, 8))
CHANNELS = (("dine_in", 0.5), ("takeaway", 0.25), ("delivery", 0.25))
STATIC_FILES = ("restaurants.xlsx", "menu_items.csv", "customers.csv", "reviews.csv")

RESTAURANTS = (
    ("R01", "Dosa Corner", "Bengaluru", "South Indian", dt.datetime(2023, 6, 1, 9, 0)),
    ("R02", "Spice Route", "Mumbai", "North Indian", dt.datetime(2022, 11, 15, 9, 0)),
    ("R03", "Chaat Junction", "Delhi", "Street food", dt.datetime(2024, 2, 10, 9, 0)),
    ("R04", "Brew & Bun", "Pune", "Cafe", dt.datetime(2024, 9, 1, 9, 0)),
    ("R05", "Kolkata Kitchen", "Kolkata", "Bengali", dt.datetime(2023, 1, 20, 9, 0)),
    ("R06", "Biryani Bazaar", "Hyderabad", "Biryani", dt.datetime(2025, 3, 5, 9, 0)),
)
# Six items for each restaurant: (name, category, price).
MENUS = {
    "R01": (("Masala dosa", "main", 120), ("Idli sambar", "main", 80), ("Medu vada", "starter", 70),
            ("Rava kesari", "dessert", 60), ("Filter coffee", "drink", 40), ("Uttapam", "main", 110)),
    "R02": (("Butter chicken", "main", 340), ("Dal makhani", "main", 260), ("Paneer tikka", "starter", 280),
            ("Garlic naan", "main", 60), ("Gulab jamun", "dessert", 90), ("Sweet lassi", "drink", 80)),
    "R03": (("Pani puri", "starter", 60), ("Aloo tikki chaat", "starter", 90), ("Dahi bhalla", "starter", 100),
            ("Pav bhaji", "main", 140), ("Kulfi", "dessert", 70), ("Masala chai", "drink", 30)),
    "R04": (("Cappuccino", "drink", 180), ("Cold brew", "drink", 200), ("Bun maska", "starter", 90),
            ("Croissant", "main", 150), ("Cheesecake", "dessert", 240), ("Veg sandwich", "main", 190)),
    "R05": (("Kosha mangsho", "main", 380), ("Luchi", "main", 90), ("Aloo posto", "main", 180),
            ("Fish fry", "starter", 260), ("Mishti doi", "dessert", 80), ("Aam panna", "drink", 70)),
    "R06": (("Chicken biryani", "main", 320), ("Mutton biryani", "main", 420), ("Mirchi ka salan", "starter", 120),
            ("Double ka meetha", "dessert", 110), ("Irani chai", "drink", 40), ("Veg biryani", "main", 240)),
}
FIRST_NAMES = ("Aarav", "Diya", "Kabir", "Meera", "Rohan", "Isha", "Vikram", "Anaya", "Arjun", "Tara", "Neel",
               "Sana", "Dev", "Riya", "Kiran")
LAST_NAMES = ("Rao", "Shah", "Iyer", "Gupta", "Das", "Nair", "Mehta", "Bose", "Reddy", "Khan")
COMMENTS = ("Great food", "Loved it", "Quick service", "Too salty", "Will come again", "A bit slow",
            "Good value", "Fresh and hot")


@dataclass
class Dataset:
    """Every table of the example, as lists of dicts, built by :func:`generate`."""

    seed: int
    restaurants: list[dict]
    menu_items: list[dict]
    customers: list[dict]
    orders: list[dict]
    order_lines: list[dict]
    reviews: list[dict]

    def batches(self) -> list[str]:
        """The batch names, in the order the batches arrive."""
        return ["batch1", "batch2", *(day.isoformat() for day in DROP_DAYS)]

    def batch_orders(self, batch: str) -> list[dict]:
        """The orders of one batch, oldest first by ``updated_at``."""
        return sorted((order for order in self.orders if order["batch"] == batch),
                      key=lambda order: (order["updated_at"], order["order_id"]))

    def batch_order_lines(self, batch: str) -> list[dict]:
        ids = {order["order_id"] for order in self.orders if order["batch"] == batch}
        return [line for line in self.order_lines if line["order_id"] in ids]


def _pick(rng: random.Random, items: tuple | list):
    return items[int(rng.random() * len(items))]


def _weighted(rng: random.Random, choices: tuple):
    point = rng.random()
    total = 0.0
    for value, weight in choices:
        total += weight
        if point < total:
            return value
    return choices[-1][0]


def _rating(rng: random.Random) -> int:
    """One in five reviews is unhappy (1 or 2 stars), and the rest give 3, 4 or 5."""
    return 1 + int(rng.random() * 2) if rng.random() < 0.2 else 3 + int(rng.random() * 3)


def _month_days(month_index: int) -> list[dt.date]:
    """The days of month ``month_index`` (0 is January 2026), or 1 to 7 August for index 7."""
    if month_index == 7:
        return list(DROP_DAYS)
    first = dt.date(2026, month_index + 1, 1)
    following = dt.date(2026 + (month_index + 1) // 12, (month_index + 1) % 12 + 1, 1)
    return [first + dt.timedelta(days=offset) for offset in range((following - first).days)]


def generate(seed: int = SEED) -> Dataset:
    """Build every table.

    The numbers do not depend on the Python version: each random stream is seeded with text, and only
    ``random()`` is called on it.
    """
    restaurants = [{"restaurant_id": rid, "name": name, "location": city, "cuisine": cuisine, "created_at": created}
                   for rid, name, city, cuisine, created in RESTAURANTS]
    menu = [{"menu_item_id": f"M{number:03d}", "restaurant_id": rid, "name": name, "category": category,
             "price": price}
            for number, (rid, name, category, price) in enumerate(
                ((rid, *item) for rid, items in MENUS.items() for item in items), start=1)]
    menu_by_restaurant = {rid: [item for item in menu if item["restaurant_id"] == rid] for rid in MENUS}

    customers, orders = [], []
    for number in range(1, CUSTOMERS + 1):
        customer_id = f"C{number:04d}"
        rng = random.Random(f"{seed}:customer:{customer_id}")
        # 60% were already customers in January, the rest sign up through the first half of the year.
        if rng.random() < 0.6:
            before = FIRST_DAY - dt.timedelta(days=1 + int(rng.random() * 200))
            signed_up = dt.datetime.combine(before, dt.time(9 + int(rng.random() * 12), int(rng.random() * 60)))
            start_month = 0
        else:
            start_month = int(rng.random() * 7)  # January to July
            day = _pick(rng, _month_days(start_month))
            signed_up = dt.datetime.combine(day, dt.time(9 + int(rng.random() * 12), int(rng.random() * 60)))
        home = _pick(rng, restaurants)
        customers.append({"customer_id": customer_id, "name": f"{_pick(rng, FIRST_NAMES)} {_pick(rng, LAST_NAMES)}",
                          "city": home["location"], "signed_up_at": signed_up})
        active_probability = 0.45 + 0.5 * rng.random()   # how likely they order in a month they are still customers
        leave_probability = 0.03 + 0.09 * rng.random()   # the chance they stop for good after an active month
        for month_index in range(start_month, 8):
            scale = 7 / 31 if month_index == 7 else 1.0  # August is only its first week
            if rng.random() < active_probability * scale:
                for _ in range(1 + (rng.random() < 0.5) + (rng.random() < 0.25)):
                    restaurant = home if rng.random() < 0.7 else _pick(rng, restaurants)
                    day = _pick(rng, _month_days(month_index))
                    placed = dt.datetime.combine(day, dt.time(10 + int(rng.random() * 12), int(rng.random() * 60),
                                                              int(rng.random() * 60)))
                    orders.append({"customer_id": customer_id, "restaurant_id": restaurant["restaurant_id"],
                                   "order_ts": placed, "channel": _weighted(rng, CHANNELS),
                                   "updated_at": placed + dt.timedelta(minutes=1 + int(rng.random() * 9)),
                                   "_rng": rng})
            if month_index < 7 and rng.random() < leave_probability:
                break

    _add_late_orders(orders, restaurants, seed)
    return _finish(seed, restaurants, menu, menu_by_restaurant, customers, orders)


def _add_late_orders(orders: list[dict], restaurants: list[dict], seed: int) -> None:
    """Add five June orders that reach the lake only in July.

    They belong to customers who ordered in May and not in June, so that when they arrive the churn of June falls:
    those customers had looked as if they had left.
    """
    rng = random.Random(f"{seed}:late")
    by_month: dict[int, set[str]] = {}
    for order in orders:
        by_month.setdefault(order["order_ts"].month, set()).add(order["customer_id"])
    candidates = sorted(by_month.get(5, set()) - by_month.get(6, set()))
    chosen = sorted(candidates, key=lambda _: rng.random())[:LATE_ORDERS]
    homes = {order["customer_id"]: order["restaurant_id"] for order in reversed(orders)}  # the first order's place
    for customer_id in sorted(chosen):
        placed = dt.datetime.combine(dt.date(2026, 6, 22 + int(rng.random() * 8)),
                                     dt.time(18 + int(rng.random() * 4), int(rng.random() * 60)))
        synced = dt.datetime.combine(dt.date(2026, 7, 1 + int(rng.random() * 3)),
                                     dt.time(8 + int(rng.random() * 10), int(rng.random() * 60)))
        orders.append({"customer_id": customer_id, "restaurant_id": homes[customer_id], "order_ts": placed,
                       "channel": _weighted(rng, CHANNELS), "updated_at": synced, "_rng": rng, "late": True})


def _finish(seed: int, restaurants: list[dict], menu: list[dict], menu_by_restaurant: dict[str, list[dict]],
            customers: list[dict], orders: list[dict]) -> Dataset:
    """Number the orders in time order, give each its lines and its batch, and add the reviews."""
    orders.sort(key=lambda order: (order["order_ts"], order["customer_id"]))
    lines, reviews = [], []
    for number, order in enumerate(orders, start=1):
        rng = order.pop("_rng")
        order["order_id"] = f"O{number:06d}"
        items = list(menu_by_restaurant[order["restaurant_id"]])
        total = 0
        for line_no in range(1, 2 + int(rng.random() * 4)):
            item = items.pop(int(rng.random() * len(items)))
            quantity = 1 + int(rng.random() * 3)
            lines.append({"order_id": order["order_id"], "line_no": line_no, "menu_item_id": item["menu_item_id"],
                          "quantity": quantity, "unit_price": item["price"]})
            total += quantity * item["price"]
        order["total_amount"] = total
        day = order["order_ts"].date()
        if order.pop("late", False) or BATCH1_END < day <= BATCH2_END:
            order["batch"] = "batch2"
        elif day <= BATCH1_END:
            order["batch"] = "batch1"
        else:
            order["batch"] = day.isoformat()
        if rng.random() < 0.12:
            reviews.append({"review_id": f"V{len(reviews) + 1:05d}", "order_id": order["order_id"],
                            "customer_id": order["customer_id"], "restaurant_id": order["restaurant_id"],
                            "rating": _rating(rng), "comment": _pick(rng, COMMENTS),
                            "reviewed_at": order["order_ts"] + dt.timedelta(hours=3 + int(rng.random() * 48))})
    return Dataset(seed, restaurants, menu, customers, orders, lines, reviews)


# --------------------------------------------------------------------------- writing files


def _stamp(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict], formats: dict | None = None) -> Path:
    formats = formats or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([formats[column](row[column]) if column in formats else row[column]
                             for column in columns])
    return path


def _money(value: float) -> str:
    return f"{value:.2f}"


ORDER_COLUMNS = ("order_id", "restaurant_id", "customer_id", "order_ts", "channel", "total_amount", "updated_at")
LINE_COLUMNS = ("order_id", "line_no", "menu_item_id", "quantity", "unit_price")


def write_restaurants_xlsx(path: str | Path, dataset: Dataset) -> Path:
    """Write the restaurants as an Excel workbook, the way a restaurant system exports them.

    The sheet ``Restaurants`` has a title row above the header, and a sheet ``About`` comes first, so reading it needs
    ``sheet`` and ``header_row``. Needs the ``excel`` extra.
    """
    import openpyxl

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    about = workbook.active
    about.title = "About"
    about.append(["Export from the restaurant system"])
    about.append(["The sheet Restaurants has one row for each restaurant."])
    sheet = workbook.create_sheet("Restaurants")
    sheet.append(["Restaurants, exported 2026-07-31"])
    sheet.append(["restaurant_id", "name", "location", "cuisine", "created_at"])
    for row in dataset.restaurants:
        sheet.append([row["restaurant_id"], row["name"], row["location"], row["cuisine"], row["created_at"]])
    workbook.save(path)
    return path


def write_static(folder: str | Path, dataset: Dataset) -> list[Path]:
    """Write the tables that are written once: restaurants (Excel), menu items, customers and reviews."""
    folder = Path(folder)
    return [
        write_restaurants_xlsx(folder / "restaurants.xlsx", dataset),
        _write_csv(folder / "menu_items.csv", ("menu_item_id", "restaurant_id", "name", "category", "price"),
                   dataset.menu_items, {"price": _money}),
        _write_csv(folder / "customers.csv", ("customer_id", "name", "city", "signed_up_at"), dataset.customers,
                   {"signed_up_at": _stamp}),
        _write_csv(folder / "reviews.csv",
                   ("review_id", "order_id", "customer_id", "restaurant_id", "rating", "comment", "reviewed_at"),
                   dataset.reviews, {"reviewed_at": _stamp}),
    ]


def write_batch(folder: str | Path, dataset: Dataset, batch: str, orders_name: str = "orders.csv",
                lines_name: str = "order_lines.csv") -> tuple[Path, Path]:
    """Write one batch of orders and its order lines, replacing the files of the same names.

    Args:
        folder: Where the files go.
        dataset: What :func:`generate` returned.
        batch: ``batch1``, ``batch2`` or a day from ``2026-08-01`` to ``2026-08-07``.
        orders_name: The orders file. The restaurant example lands every batch as ``orders.csv``.
        lines_name: The order lines file.

    Raises:
        ValueError: If ``batch`` is not one of :meth:`Dataset.batches`.
    """
    if batch not in dataset.batches():
        raise ValueError(f"unknown batch {batch!r}; the batches are {dataset.batches()}")
    folder = Path(folder)
    return (
        _write_csv(folder / orders_name, ORDER_COLUMNS, dataset.batch_orders(batch),
                   {"order_ts": _stamp, "updated_at": _stamp, "total_amount": _money}),
        _write_csv(folder / lines_name, LINE_COLUMNS, dataset.batch_order_lines(batch), {"unit_price": _money}),
    )


def write_all(folder: str | Path, dataset: Dataset) -> list[Path]:
    """Write the static tables and every batch, each batch as ``orders_<batch>.csv`` and ``order_lines_<batch>.csv``."""
    folder = Path(folder)
    written = write_static(folder, dataset)
    for batch in dataset.batches():
        written += write_batch(folder, dataset, batch, f"orders_{batch}.csv", f"order_lines_{batch}.csv")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic restaurant chain's tables and order batches.")
    parser.add_argument("--out", required=True, help="the folder for the files")
    parser.add_argument("--seed", type=int, default=SEED, help=f"random seed (default {SEED})")
    args = parser.parse_args(argv)
    dataset = generate(args.seed)
    written = write_all(args.out, dataset)
    print(f"Wrote {len(written)} files to {args.out}: {len(dataset.restaurants)} restaurants, "
          f"{len(dataset.customers)} customers, {len(dataset.orders)} orders in {len(dataset.batches())} batches, "
          f"{len(dataset.order_lines)} order lines and {len(dataset.reviews)} reviews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
