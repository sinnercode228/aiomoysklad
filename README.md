# aiomoysklad

Async toolkit for integrations with the [MoySklad](https://www.moysklad.ru) JSON API 1.2.
Русская версия: [README.ru.md](README.ru.md).

**Status: design stage.** There is no usable code yet. The plan for v0.1 is in
[docs/design.md](docs/design.md). Nothing here has been tested against a live MoySklad
account.

v0.1 is meant to cover the parts an integrator needs most and that existing Python clients
leave out: a limiter that counts request weight the way MoySklad does, at most 5 requests in
flight, retries on 429 driven by the `X-Lognex-*` headers, a guard against the automatic API
cut-off, pagination, a `filter=` builder, the short stock reports, the assortment endpoint
without stock, customer orders and webhooks.

## License

MIT, see [LICENSE](LICENSE).
