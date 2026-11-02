# aiomoysklad v0.1 design

Status: written before any code and updated where the implementation settled details.
Nothing in it has been checked against a live MoySklad account.

## Sources

Every limit, header, path, parameter and error format below comes from one of these, read on
2026-11-02:

- `moysklad/api-remap-1.2-doc`, branch `master`, commit `9a811ce` (2026-10-26), the `md/`
  files. Cited as `file § section`, for example `_restrictions.md § Лимит запросов за
  3-секундный период`.
- `moysklad/api-remap-1.2-openapi-specification`, commit `2439d7d` (2026-10-28). Cited as
  `openapi: <file>`.

Where the two disagree, the Markdown docs win, because they are the newer text:

- `openapi: src/openapi.yaml` (`info.description`) still says "45 requests per 3 seconds per
  account". `_restrictions.md` counts per user and per solution and adds request weights.
- `openapi: src/components/headers.yaml` (`AcceptEncoding`) defaults to `gzip, deflate, br`.
  `_general.md § Сжатие содержимого ответов` says only `gzip` is accepted.
- The OpenAPI file for `/entity/assortment` does not mention the beta header that selects
  the new assortment endpoint.

Where the docs are silent, this spec says so and picks the conservative option. The open
questions are collected in the last section.

## Corrections to the prior-art notes

Checking the research notes against the docs changed a few points:

| Note | What the docs say |
|---|---|
| Draft solutions get 11 | Solutions in status `Черновик` **or** `На модерации`: weight 4, so 11 per 3 s, from 2026-04-13 (`_restrictions.md § Лимит запросов за 3-секундный период`). |
| User auth: 15, then 11 | It is a weight on a 45-unit basket: 2 units from 2026-05-12 (22 requests), 3 from 2026-09-01 (15), 4 from 2026-12-01 (11). Same section. |
| Stock reports cost 5 | 5 units for `GET /api/remap/1.2/report/stock/all` and `GET /api/remap/1.2/report/stock/bystore`, **whatever the auth kind**. The `/current` sub-paths are not named (open question 1). Same section; `notifications/rate-limits.md`. |
| Cut-off after 200 errors/min | "More than 200 identical failing requests per minute **during the last hour**", same for 429s; "more than 100 PUT requests to the same entity per minute during the last hour" (`_restrictions.md § Автоматическое отключение доступа`). |
| Webhooks off after 7 of 10 days | More than seven of the last ten days answered only with errors (or unreachable) **and** not one webhook handled in those ten days. Re-enabling is self-service (same section). |
| New lightweight assortment endpoint | Same path `GET /entity/assortment`, selected with the header `X-Lognex-Remap-Beta-Feature: assortmentWithoutStock`; the old behaviour is removed gradually in 2027 (`notifications/new-assortment-endpoint.md`; `dictionaries/_assortment.md § Ассортимент`). |
| `Accept-Encoding: gzip` is mandatory | Yes, and **any other** encoding is refused too: no header, or a header with a different encoding, gets 415 with no body (`_general.md § Сжатие содержимого ответов`). httpx sends `gzip, deflate` by default, so the client must override it. |

Also found: `X-Lognex-WebHook-Disable` stopped working on 2026-10-01; the replacement is
`X-Lognex-WebHook-DisableByPrefix` (`notifications/deprecate-header-x-lognex-webhook-disable.md`).

## Scope of v0.1

In: auth, the transport (limiter, concurrency cap, retries, cut-off guard, typed errors),
pagination, the filter builder, the short stock reports, the assortment endpoint without
stock, customer orders (list since a cursor, create, change state), webhook and webhookstock
CRUD, a webhook receiver helper and a webhook health check.

Out: a full data model, positions sub-resources, the legacy assortment endpoint, async tasks
(`_async.md`), file downloads (they redirect, `_general.md § Ссылки на файлы`), the extended
stock report, the vendor API.

Entities stay plain `dict[str, Any]`. Only the small structures the toolkit itself reads
(meta, errors, states, webhooks, webhook payloads) get pydantic models with
`extra="allow"`, because "a new field may be added without breaking the response structure"
(`_general.md § Замечания по разработке клиентских приложений`). Stock rows are frozen
dataclasses with only the documented fields instead (section 5.1): a report can hold tens of
thousands of rows, and each row has one value whose key is the stock type.

## 1. Auth

Two header forms (`_general.md § Аутентификация`):

- Basic: `Authorization: Basic base64(login:password)`.
- Bearer: `Authorization: Bearer <Access-Token>`.

`_restrictions.md § Лимиты и ограничения в JSON API` names three ways in: login and password
(Basic), a user token (Bearer), a solution token (Bearer). A Bearer token does not say whose
it is, so the caller declares it:

```python
class AuthKind(enum.Enum):
    USER = "user"                      # Basic, or a user's Bearer token
    SOLUTION = "solution"              # token of a public or private solution
    DRAFT_SOLUTION = "draft_solution"  # solution in status Черновик or На модерации

BasicAuth(login: str, password: str)            # kind is always USER
BearerToken(token: str, *, kind: AuthKind = AuthKind.USER)
```

`USER` is the default because it is the most expensive kind: a wrong guess then makes the
client slower, never blocked.

Weight of one request, from `_restrictions.md § Лимит запросов за 3-секундный период`:

| Auth kind | Before 2026-05-12 | From 2026-05-12 | From 2026-09-01 | From 2026-12-01 |
|---|---|---|---|---|
| `SOLUTION` | 1 | 1 | 1 | 1 |
| `DRAFT_SOLUTION` | 1 (4 from 2026-04-13) | 4 | 4 | 4 |
| `USER` | 1 | 2 | 3 | 4 |
| any, `GET report/stock/all` or `report/stock/bystore` | 5 | 5 | 5 | 5 |

The basket holds 45 units per 3 seconds. The docs give bare dates; the client compares them
with today's date in Moscow time, from a clock the caller can replace. The table lives in one module,
`_weights.py`, with the doc citation, so a change in the docs is a one-file change.

`issue_user_token(login, password) -> str` wraps `POST /security/token` with Basic auth and
returns `access_token` (`_general.md § Получение нового токена`). The docstring says what the
docs say: issuing a token revokes the user's earlier tokens.

## 2. Transport

### 2.1 Client

`MoySklad` wraps one `httpx.AsyncClient`:

- base URL `https://api.moysklad.ru/api/remap/1.2` (`openapi: src/openapi.yaml`, `servers`);
- `Accept-Encoding: gzip`, exactly (`_general.md § Сжатие содержимого ответов`); httpx
  decodes gzip itself;
- `Accept: application/json;charset=utf-8` (error 1062 in `_errors.md` names it as the only
  supported value);
- `Content-Type: application/json` on bodies (`openapi: src/components/headers.yaml`,
  `ContentTypeJson`; error 1037);
- `follow_redirects=False`. 301, 302, 303 and 308 carry `Location`
  (`_general.md § Обработка ошибок`); v0.1 raises `RedirectError` with the location instead
  of resending credentials to an unchecked URL;
- absolute hrefs from responses (`meta.href`, `nextHref`, webhook payloads) are accepted only
  if scheme and host match the base URL, so a forged payload cannot make the client send the
  token elsewhere;
- a `User-Agent` of `aiomoysklad/<version>`;
- query strings are encoded by the client with `%20` for spaces and every reserved character
  percent-encoded. The docs require `filter` and `order` to be urlencoded and show unencoded
  examples "for readability" (`_general.md § Фильтрация выборки с помощью параметра filter`);
  error 1058 is "could not decode the URL parameters" (`_errors.md`);
- the request line and headers are checked against the 8 KB header limit before sending
  (`_restrictions.md § Лимиты и ограничения в JSON API`), raising `ValueError` instead of
  wasting a request on 414.

Optional request headers exposed as arguments: `X-Lognex-WebHook-DisableByPrefix`, one header
per prefix, each prefix with scheme and full host (`dictionaries/_webhook.md § Заголовок
временного отключения X-Lognex-WebHook-DisableByPrefix через API`). An integration that writes
back to MoySklad sets its own receiver URL here so its writes do not trigger its own webhooks.

`X-Lognex-API-Version-Deprecated` on a response is logged once as a warning
(`_general.md § Обработка ошибок`).

### 2.2 Weighted limiter

`RateLimiter(capacity=45, window=3.0)` is a token bucket of 45 units where each unit spent
returns to the bucket exactly `window` seconds after it was spent. That is the same as a
sliding window: the weights sent in any 3-second interval never exceed 45. The docs describe
"a basket of 45 handed out every 3 seconds" without saying when the window starts; a sliding
window stays under the limit for any window alignment and still allows 45 units per
3 seconds on average.

- `await limiter.acquire(weight)` waits until `weight` units are free. Waiters are served
  in arrival order, so a cheap request cannot starve a 5-unit stock report. A cancelled
  waiter gives nothing back because it took nothing.
- The weight comes from `request_weight(kind, method, path, today)` (section 1). Callers can
  override it per path: `MoySklad(..., weights={"/report/stock/all/current": 1})`.
- Units are taken inside the concurrency slot, right before the request goes out, so the
  recorded time is the real send time.
- One limiter can be passed to several clients that use the same credentials. The limit is
  per user and per solution since 2026-02-16, not per account
  (`_restrictions.md § Лимит запросов за 3-секундный период`), so clients with different
  credentials should not share one.
- `clock` and `sleep` are injectable; the tests drive the limiter with a fake clock.

### 2.3 Concurrency cap

An `asyncio.Semaphore(5)` around every exchange: "no more than 5 parallel requests from one
user or solution" (`_restrictions.md § Лимиты и ограничения в JSON API`). The account-wide
cap of 20 (same section) cannot be enforced from one process; it is documented, and the
`ConcurrencyLimit` object can be shared between clients like the limiter. Over-parallel
requests are answered with 429 and error code 1073, and the rate headers do not reflect
them (`_general.md § Обработка ошибок`; `_errors.md`, 1073).

### 2.4 Adaptive tuning from headers

`_general.md § Обработка ошибок` documents:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | requests that can be made evenly during the interval before a 429 |
| `X-RateLimit-Remaining` | requests left before a 429 |
| `X-Lognex-Retry-TimeInterval` | the interval, in milliseconds |
| `X-Lognex-Reset` | milliseconds until the limit resets; 0 if no limit is set |
| `X-Lognex-Retry-After` | milliseconds until the limit resets |

The docs do not say whether `Limit` and `Remaining` count requests or weight units for a user
token (open question 2). The limiter therefore uses only their ratio, which is the same in
either unit: after each response it sets its free units to
`min(own estimate, capacity * Remaining / Limit)`. It never raises its estimate from headers,
because responses can arrive out of order under concurrency. When `Remaining` is 0 it holds
new requests for `X-Lognex-Reset` milliseconds. A `X-Lognex-Retry-TimeInterval` between 1 and
60 seconds replaces the window length. The observed drop in `Remaining` per request is logged
at debug level, so the real weight of an endpoint can be read off a live run.

### 2.5 Retry on 429

- Wait `X-Lognex-Retry-After` ms; if absent, `X-Lognex-Reset` ms; if both are absent (error
  1073 carries no rate headers), exponential backoff from 0.5 s with jitter, capped at 10 s.
- At most 3 retries by default (`RetryPolicy(max_retries=3, max_wait=60.0)`). A wait longer
  than `max_wait` raises `RateLimitError` with `retry_after` set instead of sleeping.
- GET, PUT and DELETE are retried. POST is retried only when every object in the body carries
  a `syncId`: the docs promise that resending a create with the same `syncId` returns the
  object created first (`_general.md § Назначение поля syncId`). Other POSTs are retried only
  with `retry_unsafe_writes=True`, because the docs do not say that a 429 means "not
  processed" (open question 6).
- 502, 503 and 504 (`_general.md § Обработка ошибок`) and connection errors are retried with
  the same method rules. 500 is not retried: an identical failing request counts toward the
  cut-off below.

### 2.6 Cut-off guard

`ErrorGuard` keeps three rolling 60-second counters and refuses to send, raising
`GuardTrippedError`, before the documented thresholds of
`_restrictions.md § Автоматическое отключение доступа` are reached:

| Counter | Documented cut-off | Default stop |
|---|---|---|
| identical failing requests (same method, URL and body hash, any 4xx/5xx) | more than 200 per minute | 150 |
| 429 responses | more than 200 per minute | 150 |
| PUT requests to one entity (same path) | more than 100 per minute | 75 |

The stops are configurable but cannot be set above the documented values. The guard sits
before the limiter, so a request it refuses spends no units. It is shared like the limiter.
Getting access back needs MoySklad support (same section), which is why the guard fails
closed.

### 2.7 Typed errors

An error body is `{"errors": [error, ...]}` (`_general.md § Обработка ошибок`;
`openapi: src/components/schemas/common/errors.yaml`). Bulk requests can answer 400 with an
array of such objects (`openapi: src/components/responses.yaml`, `BatchBadRequest`), and the
error response may also be `text/html` (`CommonError`), so parsing falls back to raw text.

```python
class ApiErrorDetail(BaseModel):  # fields from _general.md § СТРУКТУРА ОБЪЕКТА error
    error: str
    parameter: str | None
    code: int | None  # if empty, the HTTP status applies
    error_message: str | None
    more_info: str | None  # "moreInfo"
    line: int | None
    column: int | None
    dependencies: list[Meta] | None
    meta: Meta | None
```

`MoySkladError` carries `status`, `errors`, `codes`, `method`, `url`, and the
`X-Lognex-Auth` and `X-Lognex-Auth-Message` headers when present. Subclasses by status, from
the table in `_general.md § Обработка ошибок`:

| Status | Exception |
|---|---|
| 301, 302, 303, 308 | `RedirectError` (`location`) |
| 400, 412 | `ValidationError` |
| 401 | `AuthError` |
| 403 | `ForbiddenError` |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 410 | `ApiVersionGoneError` |
| 413 | `PayloadTooLargeError` |
| 415 | `UnsupportedMediaTypeError` (no body) |
| 429 | `RateLimitError` (`retry_after`, `parallel` when code 1073) |
| 500, 502, 503, 504 | `ServerError` |
| other | `MoySkladError` |

Local failures: `NetworkError`, `GuardTrippedError`.

## 3. Pagination

`limit` is 1 to 1000, default 1000; `offset` starts at 0
(`workbook/_workbook_filter_paging_search_sort.md § Листание`; `documents/_customerOrder.md
§ Получить список Заказов покупателей`). When more rows exist, the collection `meta` has
`nextHref` and `previousHref` (`_general.md § Мета коллекций`), and `nextHref` already
carries `limit` and `offset` (`workbook/_workbook_paging.md § Комбинации offset и limit`).

`ms.iter_rows(path, *, filter=None, order=None, search=None, expand=None, limit=1000,
params=None)` returns an async iterator of rows. It follows `meta.nextHref` as given until it
is missing, checks the host (section 2.1), and raises if an offset does not move forward
instead of looping. With `expand`, `limit` above 100 raises `ValueError`, because the server
would silently drop the expand (`_general.md § На expand действуют следующие правила`). The
iterator also exposes `size` from the first page's `meta`.

## 4. Filter builder

From `_general.md § Фильтрация выборки с помощью параметра filter` and `§ Оператор
фильтрации "подобие"`:

- conditions are joined with `;`; operators are `=`, `>`, `<`, `>=`, `<=`, `!=`, `~`, `~=`,
  `=~`;
- `~` is a substring, `~=` a prefix, `=~` a suffix match, string fields only, case
  insensitive;
- several `=` on one field mean OR, several `!=` mean AND;
- `=` and an inequality on the same field is an error; repeating `>`, `<`, `>=` or `<=` on a
  field keeps only the first and "will be an error in future versions";
- `field=` with no value matches empty, `field!=` matches non-empty (`§ Проверка на пустое
  значение`);
- a literal `;` in a text value is written `\;`. That is the only escape the docs define;
- the whole value is urlencoded (section 2.1).

```python
f = (
    Filter()
    .eq("state.name", "Новый", "Принят")  # state.name=Новый;state.name=Принят
    .ge("updated", datetime(2026, 11, 1, 9, 0))  # updated>=2026-11-01 09:00:00
    .contains("name", "a;b")  # name~a\;b
    .is_empty("description")  # description=
)
str(f)
```

Methods: `eq`, `ne`, `gt`, `lt`, `ge`, `le`, `contains` (`~`), `startswith` (`~=`),
`endswith` (`=~`), `is_empty`, `not_empty`, `raw(expr)`, and `&` to combine two filters.

Values: `str` with `;` escaped; `bool` as `true`/`false`; `int`, `float`, `Decimal` as is;
`UUID` as text; a `datetime` as `YYYY-MM-DD HH:MM:SS` in Moscow time, converting aware values
and taking naive ones as Moscow time (`_general.md § Формат даты и времени`: "MSK"; MSK is a
fixed UTC+3, so no tz database is needed); a meta dict or anything with `href` as the href,
for reference fields (`§ Фильтрация ссылочных полей`). Custom attributes are filtered by the
attribute's href as the field name (`§ Фильтрация по дополнительным полям`).

The builder raises `ValueError` early for what the docs call an error: `=` plus an inequality
on one field, a repeated comparison on one field, and a like operator on a non-string value.
A string ending in `\` is refused, since the docs do not say how a backslash before the `;`
separator is read (open question 7).

## 5. Stock and assortment

### 5.1 Short stock reports

From `reports/_report_stock.md § Краткий отчет об остатках` and `openapi:
src/paths/reports/stocks/stock-all-current.yaml`, `stock-bystore-current.yaml`:

| | `GET /report/stock/all/current` | `GET /report/stock/bystore/current` |
|---|---|---|
| groups by | assortment | assortment and store |
| row | `{"assortmentId", <stockType>}` | `{"assortmentId", "storeId", <stockType>}`; `storeId` may be `null` (reserves without a store) |
| `stockType` | `stock` (default), `freeStock`, `quantity`, `reserve`, `inTransit`; one per request | same |
| `include=zeroLines` | include zero rows | same |
| `changedSince` | `YYYY-MM-DD HH:MM:SS`; not in the future; at most 24 h back; implies zero rows | same |
| `withRecalculate=true` | not available | include rows being recalculated, value `null` |
| `filter` | `assortmentId=<uuid>[,<uuid>]`, `storeId=<uuid>[,<uuid>]`, ids not hrefs, `=` only | same |

The answer is one JSON array with no paging and no order. Only an administrator or an
employee with the stock permission may call it. Rows with `changedSince` are current totals of
the items that changed, not deltas.

```python
await ms.stock.all_current(stock_type="stock", *, include_zero=False,
                           changed_since=None, assortment_ids=(), store_ids=())
    -> list[StockRow]                  # assortment_id, stock_type, value: float | None
await ms.stock.bystore_current(stock_type="stock", *, include_zero=False,
                               changed_since=None, assortment_ids=(), store_ids=(),
                               with_recalculate=False)
    -> list[StoreStockRow]             # + store_id: str | None
```

Ids are `str`, not `UUID`: they are joined with the `id` strings of assortment rows, and the
doc examples themselves hold ids that do not parse as UUIDs
(`7944ef04-f831-11e5-7a69-971500188b1`). `stockType` is always sent. A row is read by the key
of the requested type; a row that names another documented type is read as that type, because
the `changedSince` example of `§ Параметр changedSince` asks for the default `stock` and shows
rows with `quantity`. A row with no known type is an error.

- `changed_since` outside "now minus 24 h" to "now" raises `ValueError`.
- Long id lists are split so each URL stays under the 8 KB header limit (section 2.1); every
  chunk is a separate request at weight 5.
- Weight: 5 by default for anything under `/report/stock/all` and `/report/stock/bystore`,
  `/current` included (section 1, open question 1).

`StockPoller` follows the docs' advice: poll with `changedSince` and an overlap (their example
is every 30 minutes for the past 35), run a full report without `changedSince` at least daily,
and fall back to a full report whenever the cursor is older than 24 hours.

- `StockPoller(ms.stock, *, by_store=False, stock_type="stock", overlap=5 min,
  full_every=24 h, assortment_ids=(), store_ids=(), last_poll=None, last_full=None)`;
  `await poller.poll() -> StockUpdate(rows, full, since, at)`. It does not sleep.
- A full report asks for `include=zeroLines`: without it an item that ran out would just be
  missing. `full_every` cannot exceed 24 hours, `overlap` must be under 24 hours.
- `last_poll` and `last_full` move only after a report was read, and can be saved and passed
  back. A clock that went back is clamped, so `changedSince` is never in the future.

`join_stock(rows, stock, *, default=None)` returns `(row, value)` pairs. Rows of a report by
store are summed per item, `None` if any is `None`; mixing stock types is an error. `default`
is the value for items missing from the report: `0.0` fits a full report, `None` a report with
`changedSince`.

### 5.2 Assortment without stock

`GET /entity/assortment` with `X-Lognex-Remap-Beta-Feature: assortmentWithoutStock`
(`dictionaries/_assortment.md § Ассортимент`). Without the header the request goes to the
legacy endpoint, which is to be removed during 2027 (`notifications/new-assortment-endpoint.md`).

- Parameters: `limit` 1 to 1000, `offset`, `groupBy` = `product`, `variant` or
  `consignment`, `search`, `filter` (`§ Атрибуты доступные для фильтрации`).
- No `stock`, `reserve`, `inTransit`, `quantity` fields; join with the stock report on
  `id` = `assortmentId` (`§ Поля остатков`). `join_stock(rows, stock_rows)` does that.
- Filters it rejects, from `§ Недоступные фильтры`: `alcoholic.type`, `article`,
  `isSerialTrackable`, `minimumBalance`, `owner`, `quantityMode`, `salePrice`, `search`
  (as a filter field; the `search` parameter works), `shared`, `stockMode`, `stockMoment`,
  `stockStore`, `updatedBy`, `weighed`. `barcode` has no `!=`. The builder checks these
  before sending.
- `expand` only `product`, `images`, `components`, first level (`§ Ограничения expand`).
- It is a beta: the contract may change (`_general.md § Заголовок
  X-Lognex-Remap-Beta-Feature`), the header takes one value, and an unknown tag is ignored
  silently. If a row comes back with stock fields, the client logs a warning that the header
  was ignored.

```python
ms.assortment.iter(*, filter=None, search=None, group_by=None, order=None, expand=None,
                   limit=1000, params=None) -> Paginator
```

Also checked before sending, from the same page: on one field, like operators combine only as
one `~=` with one `=~` (`§ Изменения в поведении фильтров`); `type=consignment` needs
`groupBy=consignment` and `withSubFolders` needs a `productFolder` filter (`§ Атрибуты
доступные для фильтрации`); `order` cannot use `inTransit`, `minimumBalance`, `quantity`,
`reserve`, `salePrice` or `stock` (`§ Недоступные сортировки`). The checks read the filter as
text (`split_conditions`), so they apply to a `Filter` and a raw string alike. The paginator
sends the beta header with every page, since `nextHref` does not carry it.

## 6. Customer orders

Paths from `openapi: src/openapi.yaml` and `documents/_customerOrder.md`:
`GET/POST /entity/customerorder`, `GET/PUT /entity/customerorder/{id}`,
`GET /entity/customerorder/metadata`.

### 6.1 List since an updated cursor

`updated` supports `=`, `!=`, `<`, `>`, `<=`, `>=` (`documents/_customerOrder.md § Атрибуты
сущности`); `updated` and `id` are sortable (`workbook/_workbook_sorting.md § Доступные поля
для сортировки`); times are Moscow time, returned with milliseconds, and the docs list
filter formats down to seconds (`_general.md § Формат даты и времени`).

Offset paging over a set that changes while it is read can skip rows: an order updated
mid-walk moves to the end and shifts the rest. So the iterator pages by key instead:

1. Request `filter=updated>=T;<caller filter>`, `order=updated,asc;id,asc`, `limit=N`,
   with `T` the cursor rounded down to the second.
2. Yield rows not already seen; remember `(id, updated)` of rows at the last second.
3. Next page: `T` = `updated` of the last row, rounded down; repeat until a page is short.
4. If a whole page falls in one second, page through that second with `offset` and
   `updated>=T;updated<T+1s`, then continue from `T+1s`.

```python
orders = ms.customer_orders.iter_updated_since(cursor, filter=None, page_size=1000, expand=None)
async for order in orders:
    ...
cursor = orders.cursor  # OrderCursor(updated: datetime, seen: frozenset[str]); persist it
saved = cursor.as_dict()  # {"updated": "YYYY-MM-DD HH:MM:SS", "seen": [...]}
```

Details settled while implementing:

- After step 4, the cursor stays on the full second, with all its ids seen, until an order of
  a later second is read: more orders may still be written in that second. A later run then
  reads that second again by offset, skipping what it saw.
- The caller's filter cannot hold `updated`; the cursor sets it.
- An order changed during the walk comes again with its new `updated`. Offset paging inside
  one second (step 4) can still skip an order if another order of that second changes during
  that walk; that needs more than `page_size` orders saved in one second.

Deleted orders do not show up: documents in the trash are excluded unless `isDeleted` is
filtered (`_general.md § Фильтрация документов`). Use DELETE webhooks for deletions.

### 6.2 Create

`POST /entity/customerorder`; `organization` and `agent` metas are required (`§ Создать
Заказ покупателя`). `create(body, *, sync_id=None)` adds a `syncId` (uuid4) unless one is
given, so a retry returns the same order (section 2.5). More than 1000 positions raises
`ValueError` (`_restrictions.md § Ограничения на размер данных`; 413). A `syncId` that is not
a UUID, or one that contradicts `sync_id`, raises `ValueError`. The caller's dict is not
changed. `get(order_id, *, expand=None)` reads one order.

### 6.3 Change state

States come from `GET /entity/customerorder/metadata`, field `states`
(`documents/_customerOrder.md § Метаданные Заказов покупателей`); each state has `name`,
`stateType` (`Regular`, `Successful`, `Unsuccessful`) and a meta href under
`/entity/customerorder/metadata/states/{id}` (`dictionaries/_states.md`). The order's `state`
field is that meta (`documents/_customerOrder.md § Атрибуты сущности`).

```python
await ms.customer_orders.set_state(order_id, "Отгружен")  # name or state UUID
# PUT /entity/customerorder/{id}  {"state": {"meta": {"href": ".../metadata/states/<id>",
#                                                    "type": "state",
#                                                    "mediaType": "application/json"}}}
```

States are cached per client and reloaded once when a name is not found. Each call counts
toward the PUT-per-entity counter (section 2.6). `states(*, reload=False) -> list[State]`
exposes them; `State` is a model of `dictionaries/_states.md § Атрибуты сущности`.
`set_state` also takes a `State`. A name that no state has, or that two states share, raises
`LookupError`. Ids are checked against `[0-9A-Za-z-]{1,64}` before they go into a path.

## 7. Webhooks

Webhooks need a paid plan or the trial: on the free plan nothing is sent and webhooks cannot
be created or changed (`_restrictions.md § Тарифные ограничения`;
`dictionaries/_webhook.md § Вебхуки`). Managing them needs an administrator or a solution
with the right permissions (error 30004).

### 7.1 CRUD

| | webhook (`dictionaries/_webhook.md`) | webhookstock (`dictionaries/_webhookstock.md`) |
|---|---|---|
| list, create | `GET`, `POST /entity/webhook` | `GET`, `POST /entity/webhookstock` |
| get, update, delete | `GET`, `PUT`, `DELETE /entity/webhook/{id}` | same under `/entity/webhookstock/{id}` |
| bulk delete | `POST /entity/webhook/delete` | `POST /entity/webhookstock/delete` |
| disable | `PUT {"enabled": false}` | same |
| required on create | `url`, `action`, `entityType` | `url`, `reportType`, `stockType` |
| values | `action`: `CREATE`, `UPDATE`, `DELETE`, `PROCESSED` (async tasks only); `diffType`: `NONE`, `FIELDS`, only with `UPDATE` | `reportType`: `all`, `bystore`; `stockType`: `stock` |
| uniqueness | `entityType` + `action` + `url`; per `entityType` + `action` at most 5 urls for users, 1 for a solution | `stockType` + `reportType` + `url`; same 5 and 1 |

`url` is at most 255 characters (error 30010). `entityType` cannot be `webhook`, `discount`
or `notes` (`§ Доступные типы сущностей`). The client checks these before sending.
`ensure(entity_type, action, url)` lists, re-enables a matching disabled webhook, or creates
one, so a deploy script can run it every time.

Both kinds have `list()`, `get(id)`, `update(id, changes)` (API field names, checked like a
create), `enable(id)`, `disable(id)`, `delete(id)` and `delete_many(webhooks_or_ids)`, which
posts metas 1000 per request (`_general.md § Создание и обновление нескольких объектов`).
`ms.webhooks.create(entity_type, action, url, *, diff_type=None)`,
`ms.webhook_stocks.create(url, *, report_type="all", stock_type="stock")`. `ensure` with a
`diff_type` also sets it on a matching webhook. Creates are not resent: a webhook has no
`syncId`.

### 7.2 Payloads

MoySklad POSTs JSON to `url?requestId=<id>` with
`User-Agent: MoySklad webhook touch agent 2.0 (/https://www.moysklad.ru)`
(`workbook/_workbook_webhooks.md § Отправка вебхука в клиентское приложение`).

- Entity webhook: `{"auditContext": {meta, uid, moment}, "events": [{meta, action,
  accountId, updatedFields?}]}`; `events` may hold several events; `updatedFields` appears
  only with `diffType=FIELDS` and `action=UPDATE` (`dictionaries/_webhook.md § Пример
  вебхука`).
- Stock webhook: `{"accountId", "stockType", "reportType", "reportUrl"}`, where `reportUrl`
  points at `/report/stock/{all|bystore}/current?changedSince=...`; sent every 1 to 5 minutes
  when stock changed (`dictionaries/_webhookstock.md`).
- The receiver must answer 200 or 204 within 1500 ms. Otherwise MoySklad tries 3 more times,
  back to back, with the same `requestId`, then drops the notification.

The docs describe no signature. Authenticity is handled by never trusting the payload beyond
the href: the receiver fetches the entity itself, only from the API host. The README will
suggest a secret path segment in the webhook URL.

### 7.3 Receiver helper

```python
receiver = WebhookReceiver(ms, dedupe=MemoryDedupe(), ttl=600.0)
delivery = await receiver.accept(body: bytes, request_id: str | None)  # None if duplicate
# answer 204 now; handle `delivery` in a background task or queue
entity = await receiver.fetch(event)        # GET event.meta.href; None for DELETE
rows = await receiver.fetch_stock(delivery.notification)  # GET reportUrl, weight 5
events = await receiver.receive(body, request_id, fetch=True)  # all of the above
```

- `accept` parses and dedupes only, so the handler can answer inside 1500 ms. It is
  framework-agnostic: it takes the raw body and the `requestId` query value.
- The dedupe key is `requestId`, or a SHA-256 of the body when it is missing. `Dedupe` is a
  protocol with one atomic `async def first_seen(key, ttl) -> bool`, so Redis can back it.
- `fetch` skips `DELETE` events, since the entity is gone, and checks the href host. An
  entity that answers 404 was deleted after the event and gives `None`.
- `fetch_stock` accepts only a `reportUrl` on `/report/stock/all/current` or
  `/report/stock/bystore/current` under the API root.
- `receive(body, request_id, *, fetch=False) -> list[ReceivedEvent]` is the one-call form:
  raw body in, events out, an empty list for a repeat. `Delivery.events` drops exact repeats
  (same href, action and `updatedFields`) inside one notification, and `fetch=True` reads
  each href once. A stock notification gives one event with its report rows.
- A body that is not a webhook raises `WebhookPayloadError` (a `ValueError`) and is not
  recorded as seen.

### 7.4 Health check

The API has no delivery log. What it exposes is each webhook's `enabled` flag; the docs say
the owner re-enables auto-disabled webhooks without support (`_restrictions.md
§ Автоматическое отключение доступа`).

```python
health = await ms.webhooks.health(url_prefix="https://hooks.example.com/ms/")
health.disabled  # webhooks and webhookstocks under the prefix with enabled == False
await ms.webhooks.health(url_prefix, reenable=True)
```

On the receiver side, `WebhookReceiver.stats` records the last day a delivery was accepted
(`last_accepted`; repeats count, since they are answered too), and counts accepted, repeated
and rejected bodies. `stats.days_without_success()` lets a monitor alert well before
MoySklad's seven-of-ten-days rule. It only sees deliveries that reached the receiver.

`health()` returns `WebhookHealth(url_prefix, webhooks, stock_webhooks, reenabled)` with the
properties `disabled` and `ok`.

## 8. Public API and layout

```python
from aiomoysklad import AuthKind, BearerToken, Filter, MoySklad

async with MoySklad(BearerToken(token, kind=AuthKind.SOLUTION)) as ms:
    async for order in ms.customer_orders.iter_updated_since(cursor):
        ...
```

```python
class MoySklad:
    def __init__(
        self,
        auth: BasicAuth | BearerToken,
        *,
        base_url: str = API_URL,
        limiter: RateLimiter | None = None,  # None: a private one
        concurrency: ConcurrencyLimit | None = None,  # None: a private Semaphore(5)
        guard: ErrorGuard | None = None,
        retry: RetryPolicy | None = None,
        weights: Mapping[str, int] | None = None,
        webhook_disable_prefixes: Sequence[str] = (),
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
        today: Callable[[], date] = moscow_today,  # Moscow date for the weight table
    ) -> None: ...
    async def request(
        self,
        method: str,
        path_or_href: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        weight: int | None = None,
    ) -> Any: ...
    async def get(self, href: str) -> Any: ...
    def iter_rows(self, path: str, **query: Any) -> Paginator: ...

    stock: StockReports
    assortment: Assortment
    customer_orders: CustomerOrders
    webhooks: Webhooks  # entity webhooks; .health() covers both kinds
    webhook_stocks: WebhookStocks
```

Exported: `MoySklad`, `API_URL`, `AuthKind`, `BasicAuth`, `BearerToken`, `issue_user_token`,
`request_weight`, `RateLimiter`, `ConcurrencyLimit`, `ErrorGuard`, `RetryPolicy`, `Filter`,
`Paginator`, `OrderCursor`, `StockRow`, `StoreStockRow`, `StockPoller`, `join_stock`,
`Webhook`, `WebhookStock`, `EntityNotification`, `StockNotification`, `WebhookEvent`,
`WebhookReceiver`, `Dedupe`, `MemoryDedupe`, `WebhookHealth`, `Meta`, `ApiErrorDetail`, the
exceptions of section 2.7, `__version__`. Added while implementing: the helper classes
(`StockReports`, `Assortment`, `CustomerOrders`, `OrderStream`, `Webhooks`, `WebhookStocks`),
`StockUpdate`, `State`, `AuditContext`, `Delivery`, `ReceivedEvent`, `ReceiverStats`,
`WebhookPayloadError`, `parse_webhook`, and the literal types `StockType`, `GroupBy`,
`Action`, `DiffType` and `ReportType`.

```
src/aiomoysklad/
  __init__.py      _version.py   py.typed
  _auth.py         AuthKind, BasicAuth, BearerToken
  _weights.py      weight schedule and per-path weights (cites _restrictions.md)
  _ratelimit.py    RateLimiter, ConcurrencyLimit, header tuning
  _guard.py        ErrorGuard
  _retry.py        RetryPolicy, delay from X-Lognex-* headers
  _errors.py       exceptions, error body parsing
  _time.py         Moscow time formatting and parsing
  _client.py       MoySklad, issue_user_token, URL and header handling
  _pagination.py   Paginator
  _filters.py      Filter
  _models.py       Meta, ApiErrorDetail, State, Webhook, WebhookStock, payload models
  _stock.py        StockRow, StoreStockRow, StockReports, StockPoller, join_stock
  _assortment.py   Assortment
  _orders.py       CustomerOrders, OrderCursor
  _webhooks.py     Webhooks, WebhookStocks, WebhookHealth
  _receiver.py     WebhookReceiver, Dedupe, MemoryDedupe
tests/             one test module per source module; payloads copied from the doc examples
```

Tests never touch the network: `httpx.MockTransport` or `respx`, with a fake clock and sleep
for the limiter, guard and retries. Each module is written test-first. Code that relies on a
documented limit or header cites the doc file in a comment.

## Open questions for a live account

1. Do `/report/stock/all/current` and `/report/stock/bystore/current` cost 5 units? The docs
   name only the parent paths. v0.1 charges 5; the debug log of `Remaining` will tell.
2. Do `X-RateLimit-Limit` and `X-RateLimit-Remaining` count requests or weight units for a
   user token? v0.1 uses only their ratio.
3. Is the server window fixed or sliding? v0.1 assumes the stricter sliding one.
4. Does the `updated` filter accept milliseconds? v0.1 uses seconds and `>=`.
5. Does auto-disabling a webhook set `enabled` to `false` on the entity? The health check
   assumes it does.
6. Is a request answered with 429 never processed? Until confirmed, POST without `syncId` is
   not resent.
7. How is a backslash in a filter value read? v0.1 refuses a value ending in `\`.
