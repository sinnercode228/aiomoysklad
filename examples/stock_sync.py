"""Keep a local copy of stock: a full report first, then only the changes.

Authorization, either one:

* a token: ``MOYSKLAD_TOKEN=...``, plus ``MOYSKLAD_TOKEN_KIND=solution`` (or
  ``draft_solution``) when it is a solution's token; by default it is taken as a user's, and
  a draft solution's token left that way is undercounted
* login and password: ``MOYSKLAD_LOGIN=admin@example MOYSKLAD_PASSWORD=...``

Run::

    MOYSKLAD_TOKEN=... python examples/stock_sync.py

The names come from the assortment endpoint without stock (``assortmentWithoutStock``), the
amounts from the short report ``/report/stock/all/current``, paired by id. Every 10 minutes it
asks for the items changed since the last poll, with 5 minutes of overlap, and reads a full
report again once a day, as the MoySklad docs advise. Each report request costs 5 units of the
45-unit basket.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aiomoysklad import (
    AuthKind,
    BasicAuth,
    BearerToken,
    MoySklad,
    StockPoller,
    join_stock,
)


def auth_from_env() -> BasicAuth | BearerToken:
    token = os.environ.get("MOYSKLAD_TOKEN")
    if token:
        kind = AuthKind(os.environ.get("MOYSKLAD_TOKEN_KIND", "user"))
        return BearerToken(token, kind=kind)
    return BasicAuth(os.environ["MOYSKLAD_LOGIN"], os.environ["MOYSKLAD_PASSWORD"])


async def load_assortment(ms: MoySklad) -> list[dict[str, Any]]:
    """Products and variants, without stock fields."""
    return [row async for row in ms.assortment.iter(group_by="variant")]


def line(value: float | None, name: str) -> str:
    shown = "?" if value is None else f"{value:g}"
    return f"{shown:>10}  {name}"


async def poll_once(poller: StockPoller, assortment: list[dict[str, Any]]) -> None:
    update = await poller.poll()
    if update.full:
        # A full report has every item with stock, zero rows included; one missing from it was
        # never used in any operation.
        print(f"full report, {len(assortment)} items")
        for row, value in join_stock(assortment, update.rows, default=0.0):
            print(line(value, row["name"]))
        return
    by_id = {row["id"]: row for row in assortment}
    since = update.since.strftime("%Y-%m-%d %H:%M:%S") if update.since else "?"
    print(f"changes since {since}, {len(update.rows)} items")
    for stock in update.rows:
        item = by_id.get(stock.assortment_id)
        print(line(stock.value, item["name"] if item else stock.assortment_id))


async def main(rounds: int | None = None, interval: float = 600.0) -> None:
    async with MoySklad(auth_from_env()) as ms:
        assortment = await load_assortment(ms)
        poller = StockPoller(ms.stock, stock_type="quantity")
        done = 0
        while rounds is None or done < rounds:
            if done:
                await asyncio.sleep(interval)
            await poll_once(poller, assortment)
            done += 1


if __name__ == "__main__":
    asyncio.run(main())
