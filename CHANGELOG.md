# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

Tested against mocked HTTP only; not yet tested against a live MoySklad account.

### Added

- `MoySklad`, an async client for the JSON API 1.2 of one user or solution, on httpx. It
  sends exactly `Accept-Encoding: gzip` on every request, never follows redirects, sends
  credentials only to URLs under its API root, encodes spaces as `%20` and refuses a request
  whose line and headers exceed 8 KB or whose body exceeds 20 MB. A closed client refuses
  requests with a `MoySkladError`.
- `BasicAuth` and `BearerToken` with an `AuthKind` (user, solution, draft solution), and
  `issue_user_token` for `POST /security/token`.
- Request weights from the dated schedule of the docs (`request_weight`), overridable per
  path; the full stock reports cost 5 units, and the short `/current` ones are charged the
  same as a precaution until a live account confirms it.
- `RateLimiter`, a 45-unit basket where each unit returns 3 seconds after use, tuned down from
  `X-RateLimit-Remaining` and paused by `X-Lognex-Reset`; `ConcurrencyLimit`, 5 requests in
  flight by default. Both can be shared between clients.
- `RetryPolicy`: 429 waits for `X-Lognex-Retry-After` or `X-Lognex-Reset`, except error 1073,
  which backs off; 502, 503 and 504 are retried; a `POST` is resent only with a `syncId`.
- `ErrorGuard`, which refuses to send before the documented automatic cut-offs: 150 identical
  failures, 150 answers of 429 or 75 `PUT`s to one entity per minute by default. It checks
  again when a request leaves, counts `PUT`s then, and counts requests in flight as would-be
  failures, so concurrent callers cannot pass it together.
- An exception per HTTP status under `MoySkladError`, with the parsed `errors` array.
- `iter_rows` and `Paginator`, following `nextHref`, with the documented `limit` and `expand`
  rules; extra headers go with every page.
- `Filter`, a builder for `filter=` with the nine operators, the `\;` escape and dates in
  Moscow time; it refuses an href that holds `;` or a backslash.
- `ms.stock`: the short reports `/report/stock/all/current` and `/bystore/current` as typed
  `StockRow` and `StoreStockRow`, with `changedSince` checked against the 24-hour window and
  long id lists split under the header limit. `StockPoller` polls the changes with an overlap
  and reads a full report at least daily; `join_stock` pairs assortment rows with stock.
- `ms.assortment.iter()`: the assortment endpoint without stock
  (`X-Lognex-Remap-Beta-Feature: assortmentWithoutStock`), refusing the filters, orders and
  expands it does not support, and warning when rows come back with stock fields.
- `ms.customer_orders`: `iter_updated_since` pages by `updated` and id from a saved
  `OrderCursor`; `create` adds a `syncId`; `set_state` takes a state's name or id; `states`
  reads them from the metadata and `find_state` picks one by name or id.
- `ms.webhooks` and `ms.webhook_stocks`: list, get, create, update, enable, disable, delete,
  bulk delete and `ensure`, with the documented values checked before sending. Stock webhooks
  are created with `"enabled": true`, as the docs' example does.
  `ms.webhooks.health()` reports, and can re-enable, the disabled webhooks under a URL prefix.
- `WebhookReceiver`: parses entity and stock notifications into typed models, drops repeats
  by `requestId` through a pluggable `Dedupe` store (the in-memory one warns once it is full
  and starts letting repeats through), and fetches entities and stock reports
  only from the API host.
- Examples: `examples/stock_sync.py`, `examples/sync_orders.py` and `examples/webhooks.py`,
  run as tests against mocked endpoints.
- READMEs in English and Russian whose samples run as tests, and `docs/design.md` with the
  documentation each limit comes from and the questions left for a live account.

[0.1.0]: https://github.com/sinnercode228/aiomoysklad/releases/tag/v0.1.0
