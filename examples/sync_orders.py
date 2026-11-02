"""Read the customer orders changed since the last run and move paid ones to a state.

Authorization as in ``stock_sync.py`` (``MOYSKLAD_TOKEN`` or ``MOYSKLAD_LOGIN`` and
``MOYSKLAD_PASSWORD``). ``ORDERS_CURSOR`` names the file that keeps the cursor between runs
(default ``orders-cursor.json``); ``PAID_STATE`` names the state for paid orders, such as
``Оплачен``. Without it the orders are only printed.

Run it from cron, every few minutes::

    MOYSKLAD_TOKEN=... PAID_STATE=Оплачен python examples/sync_orders.py

The first run starts a day back. Only orders marked as applicable (``applicable=true``) come.
Moving an order to a state changes its ``updated``, so it comes once more on the next run, in
the state it was moved to, and is left alone then.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiomoysklad import (
    AuthKind,
    BasicAuth,
    BearerToken,
    MoySklad,
    OrderCursor,
)

MSK = timezone(timedelta(hours=3), "MSK")


def auth_from_env() -> BasicAuth | BearerToken:
    token = os.environ.get("MOYSKLAD_TOKEN")
    if token:
        kind = AuthKind(os.environ.get("MOYSKLAD_TOKEN_KIND", "user"))
        return BearerToken(token, kind=kind)
    return BasicAuth(os.environ["MOYSKLAD_LOGIN"], os.environ["MOYSKLAD_PASSWORD"])


def load_cursor(path: Path, now: datetime) -> OrderCursor:
    if path.exists():
        return OrderCursor.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return OrderCursor(now - timedelta(days=1))


def save_cursor(path: Path, cursor: OrderCursor) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cursor.as_dict()), encoding="utf-8")
    temporary.replace(path)


def is_paid(order: dict[str, Any]) -> bool:
    total = order.get("sum") or 0
    return total > 0 and (order.get("payedSum") or 0) >= total


async def sync(
    ms: MoySklad, cursor_path: Path, *, paid_state: str | None, now: datetime | None = None
) -> int:
    """Handle the orders changed since the saved cursor; return how many came."""
    cursor = load_cursor(cursor_path, now or datetime.now(MSK))
    # A name that matches no state raises LookupError naming it, before any order is read.
    target = await ms.customer_orders.find_state(paid_state) if paid_state else None
    orders = ms.customer_orders.iter_updated_since(cursor, filter="applicable=true")
    count = 0
    async for order in orders:
        count += 1
        text = (
            f"{order['name']}  {order['updated']}  "
            f"sum {order.get('sum')}, paid {order.get('payedSum')}"
        )
        state_href = (order.get("state") or {}).get("meta", {}).get("href")
        if target is not None and is_paid(order) and state_href != target.meta.href:
            await ms.customer_orders.set_state(order["id"], target)
            text += f" -> {target.name}"
        print(text)
    # Saved only after every order was handled: a crash means reading them again, not losing
    # them.
    save_cursor(cursor_path, orders.cursor)
    return count


async def main() -> None:
    path = Path(os.environ.get("ORDERS_CURSOR", "orders-cursor.json"))
    async with MoySklad(auth_from_env()) as ms:
        await sync(ms, path, paid_state=os.environ.get("PAID_STATE"))


if __name__ == "__main__":
    asyncio.run(main())
