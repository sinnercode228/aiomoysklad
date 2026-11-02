# aiomoysklad

An async toolkit for integrations with the [MoySklad](https://www.moysklad.ru) JSON API 1.2:
a client that keeps to MoySklad's limits, plus helpers for stock, customer orders and
webhooks. Русская версия: [README.ru.md](README.ru.md).

**Status: 0.1.0, not released yet.** Everything is tested against mocked HTTP only. It has
not been tested against a live MoySklad account yet. Every limit, header and error code comes
from the official docs; [docs/design.md](docs/design.md) cites them and lists what only a live
account can answer.

## Why another client

The official generated SDK is synchronous, has no rate limiting and takes `filter` as a raw
string. An existing async client hard-codes 45 requests per 3 seconds, which has been wrong
for user tokens since May 2026. aiomoysklad is async (httpx) and counts what MoySklad counts:

- **Request weight.** The basket holds 45 units per 3 seconds, counted per user and per
  solution. A solution's request costs 1 unit; a draft or in-moderation solution's 4; a
  user's (login and password, or a user token) 3 from 2026-09-01 and 4 from 2026-12-01. The
  stock reports cost 5 units each.
- **At most 5 requests in flight** per user or solution (20 per account).
- **429 retries** that wait for `X-Lognex-Retry-After` or `X-Lognex-Reset`, and slow down
  early from `X-RateLimit-Remaining`. A `POST` is resent only when it carries a `syncId`.
- **A cut-off guard.** MoySklad turns API access off after more than 200 identical failing
  requests a minute, 200 answers of 429, or 100 `PUT`s to one entity. The client stops at
  150, 150 and 75.
- **`Accept-Encoding: gzip`**, which is mandatory, on every request.

## Install

Not on PyPI yet. From a checkout:

```
pip install .
```

Python 3.10 to 3.13; the dependencies are httpx and pydantic 2.

## Start

<!-- sample: start -->
```python
from aiomoysklad import AuthKind, BearerToken, MoySklad


async def main(token: str) -> None:
    async with MoySklad(BearerToken(token, kind=AuthKind.SOLUTION)) as ms:
        async for product in ms.iter_rows("/entity/product", limit=100):
            print(product["name"])
```

The token's kind sets each request's weight; `BearerToken` defaults to `AuthKind.USER`. Until
2026-12-01 a draft or in-moderation solution costs more than a user (4 units against 3), so
such a token left on the default is undercounted and runs into 429s. `BasicAuth(login,
password)` is the other option. Clients with the same credentials can share one `RateLimiter`
and `ConcurrencyLimit`.

`Filter` builds `filter=` strings with the nine documented operators and escapes `;`:

<!-- sample: filter -->
```python
from aiomoysklad import Filter

f = Filter().eq("state.name", "Новый", "Принят").contains("name", "a;b")
assert str(f) == r"state.name=Новый;state.name=Принят;name~a\;b"
```

## Stock and assortment

The assortment comes from the new endpoint without stock (the legacy one is being phased out
in 2027); the amounts come from the short report, paired by id:

<!-- sample: stock -->
```python
from aiomoysklad import join_stock

rows = [row async for row in ms.assortment.iter(group_by="variant")]
stock = await ms.stock.all_current("quantity", include_zero=True)
for row, quantity in join_stock(rows, stock, default=0.0):
    print(row["name"], quantity)
```

`ms.stock.bystore_current()` groups by store as well. Rows are typed (`StockRow`,
`StoreStockRow`). `StockPoller` polls with
`changedSince` and an overlap, and reads a full report at least daily, as the docs advise.
Long id lists are split to stay under the 8 KB header limit.

## Customer orders

<!-- sample: orders -->
```python
from datetime import datetime

from aiomoysklad import OrderCursor

paid = await ms.customer_orders.find_state("Оплачен")
# First run: a naive time is Moscow time.
cursor = OrderCursor.from_dict(saved) if saved else OrderCursor(datetime(2026, 11, 1))
orders = ms.customer_orders.iter_updated_since(cursor)
async for order in orders:
    state = order.get("state", {}).get("meta", {}).get("href")
    # A PUT changes updated: next run the order comes again, already moved.
    if order["payedSum"] >= order["sum"] > 0 and state != paid.meta.href:
        await ms.customer_orders.set_state(order["id"], paid)
saved = orders.cursor.as_dict()  # store it for the next run

order = await ms.customer_orders.create({"organization": org, "agent": agent})
```

The iterator pages by `updated` and id instead of offset, so orders changed during the walk
are not skipped. `create` adds a `syncId`, so a retried request returns the order created the
first time.

## Webhooks

Webhooks work on paid plans and in the trial only. `ensure` creates a webhook or turns a
disabled one back on, so a deploy script can run it every time:

<!-- sample: webhooks -->
```python
from aiomoysklad import WebhookReceiver

url = "https://hooks.example.com/moysklad/a-long-secret"
await ms.webhooks.ensure("customerorder", "CREATE", url)

receiver = WebhookReceiver(ms)
for received in await receiver.receive(body, request_id, fetch=True):
    print(received.event, received.entity)
```

MoySklad expects 200 or 204 within 1500 ms and resends with the same `requestId`. In an
endpoint, call `receiver.accept(body, request_id)` (parse and dedupe only), answer 204, and
fetch the entities afterwards. Nothing in the docs signs a webhook, so use a secret path; the
receiver fetches entities only from the API host. The default dedupe store is in memory;
anything with an atomic `first_seen(key, ttl)`, such as Redis, can replace it.

MoySklad disables webhooks whose receiver failed on more than seven of the last ten days.
`await ms.webhooks.health(url_prefix)` lists the disabled ones and can re-enable them.

## Errors

Each HTTP status has its own exception (`NotFoundError`, `RateLimitError`, `ValidationError`
and so on), all under `MoySkladError`, with the parsed `errors` array and codes.
`GuardTrippedError` means the client refused to send a request that could get access cut off.

## Examples

[stock_sync.py](examples/stock_sync.py) (stock), [sync_orders.py](examples/sync_orders.py)
(orders) and [webhooks.py](examples/webhooks.py) (setup, an ASGI receiver, a health check).

## Not covered

No full data model (entities are dicts), no positions sub-resources, no async tasks, no file
downloads, no legacy assortment endpoint. Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
