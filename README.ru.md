# aiomoysklad

Асинхронный набор инструментов для интеграций с JSON API 1.2 [МоегоСклада](https://www.moysklad.ru):
клиент, который соблюдает лимиты МоегоСклада, и помощники для остатков, заказов покупателей и
вебхуков. English version: [README.md](README.md).

**Статус: 0.1.0, ещё не выпущен.** Всё проверено только на имитации HTTP. С живым аккаунтом
МоегоСклада библиотеку ещё не проверяли. Каждый лимит, заголовок и код ошибки взят из
официальной документации; в [docs/design.md](docs/design.md) есть ссылки на неё и список
вопросов, на которые ответит только живой аккаунт.

## Зачем ещё один клиент

Официальный сгенерированный SDK синхронный, лимиты запросов не соблюдает, а `filter`
принимает готовой строкой. Один из асинхронных клиентов жёстко зашил 45 запросов за 3 секунды,
а для токенов пользователя это неверно с мая 2026 года. aiomoysklad асинхронный (httpx) и
считает то же, что считает МойСклад:

- **Вес запроса.** В корзинке 45 единиц на 3 секунды, отдельно для каждого пользователя и
  каждого решения. Запрос решения стоит 1 единицу, решения в статусе «Черновик» или «На
  модерации» — 4, запрос пользователя (логин и пароль или токен пользователя) — 3 с 01.09.2026
  и 4 с 01.12.2026. Отчёты об остатках стоят 5 единиц.
- **Не больше 5 параллельных запросов** от пользователя или решения (20 от аккаунта).
- **Повторы после 429** ждут `X-Lognex-Retry-After` или `X-Lognex-Reset`, а по
  `X-RateLimit-Remaining` клиент притормаживает заранее. `POST` повторяется, только если в теле
  есть `syncId`.
- **Защита от отключения.** МойСклад отключает доступ к API после более чем 200 одинаковых
  запросов с ошибкой в минуту, 200 ответов 429 или 100 `PUT` к одной сущности. Клиент
  останавливается на 150, 150 и 75.
- **`Accept-Encoding: gzip`**, обязательный, в каждом запросе.

## Установка

На PyPI пока нет. Из рабочей копии:

```
pip install .
```

Python 3.10–3.13; зависимости — httpx и pydantic 2.

## Начало

<!-- sample: start -->
```python
from aiomoysklad import AuthKind, BearerToken, MoySklad


async def main(token: str) -> None:
    # Укажите, чей это токен: от этого зависит вес каждого запроса.
    async with MoySklad(BearerToken(token, kind=AuthKind.SOLUTION)) as ms:
        async for product in ms.iter_rows("/entity/product", limit=100):
            print(product["name"])
```

По умолчанию `BearerToken` считается токеном пользователя (`AuthKind.USER`) — самым дорогим,
поэтому ошибка в выборе только замедлит клиент, но не приведёт к блокировке. Второй вариант —
`BasicAuth(login, password)`. Несколько клиентов с одними учётными данными могут делить один
`RateLimiter` и один `ConcurrencyLimit`.

`Filter` собирает строку `filter=` из девяти документированных операторов и экранирует `;`:

<!-- sample: filter -->
```python
from aiomoysklad import Filter

f = Filter().eq("state.name", "Новый", "Принят").contains("name", "a;b")
assert str(f) == r"state.name=Новый;state.name=Принят;name~a\;b"
```

## Остатки и ассортимент

Ассортимент берётся из нового эндпоинта без остатков (старый в 2027 году будут постепенно
отключать), а остатки — из краткого отчёта; их связывают по id:

<!-- sample: stock -->
```python
from aiomoysklad import join_stock

rows = [row async for row in ms.assortment.iter(group_by="variant")]
stock = await ms.stock.all_current("quantity", include_zero=True)
for row, quantity in join_stock(rows, stock, default=0.0):
    print(row["name"], quantity)
```

`ms.stock.bystore_current()` даёт ещё и разбивку по складам. Строки типизированы (`StockRow`,
`StoreStockRow`) и содержат только то, что возвращает отчёт. `StockPoller` опрашивает отчёт с
`changedSince` и перекрытием интервалов и не реже раза в сутки читает полный отчёт, как
советует документация. Длинные списки id делятся на части, чтобы уложиться в 8 КБ заголовков.

## Заказы покупателей

<!-- sample: orders -->
```python
from aiomoysklad import OrderCursor

orders = ms.customer_orders.iter_updated_since(OrderCursor.from_dict(saved))
async for order in orders:
    if order["payedSum"] >= order["sum"] > 0:
        await ms.customer_orders.set_state(order["id"], "Оплачен")
saved = orders.cursor.as_dict()  # сохраните до следующего запуска

order = await ms.customer_orders.create({"organization": org, "agent": agent})
```

Итератор листает не по offset, а по `updated` и id, поэтому заказы, изменённые во время
чтения, не теряются. `create` добавляет `syncId`, и повторный запрос вернёт заказ, созданный в
первый раз. `set_state` принимает название статуса или его id.

## Вебхуки

Вебхуки работают только на платных тарифах и в пробный период. `ensure` создаёт вебхук или
включает отключённый, так что его можно вызывать при каждом деплое:

<!-- sample: webhooks -->
```python
from aiomoysklad import WebhookReceiver

url = "https://hooks.example.com/moysklad/a-long-secret"
await ms.webhooks.ensure("customerorder", "CREATE", url)

receiver = WebhookReceiver(ms)
for received in await receiver.receive(body, request_id, fetch=True):
    print(received.event, received.entity)
```

МойСклад ждёт ответа 200 или 204 в течение 1500 мс и повторяет отправку с тем же `requestId`.
В обработчике вызовите `receiver.accept(body, request_id)` (только разбор и отсев повторов),
ответьте 204, а сущности загружайте потом. Подписи у вебхуков в документации нет, поэтому
используйте секретный путь; сущности загружаются только с хоста API. Хранилище ключей по
умолчанию живёт в памяти; его можно заменить чем угодно с атомарным `first_seen(key, ttl)`,
например Redis.

МойСклад отключает вебхуки, если получатель отвечал ошибками больше семи дней из последних
десяти. `await ms.webhooks.health(url_prefix)` покажет отключённые и может включить их снова.

## Ошибки

У каждого HTTP-статуса своё исключение (`NotFoundError`, `RateLimitError`, `ValidationError`
и другие), все наследуют `MoySkladError` и несут разобранный массив `errors` и коды.
`GuardTrippedError` означает, что клиент не отправил запрос, из-за которого доступ могли бы
отключить.

## Примеры

- [examples/stock_sync.py](examples/stock_sync.py): полный отчёт об остатках, затем изменения
  каждые 10 минут.
- [examples/sync_orders.py](examples/sync_orders.py): заказы, изменённые с прошлого запуска;
  оплаченные переводятся в нужный статус.
- [examples/webhooks.py](examples/webhooks.py): настройка вебхуков, простой ASGI-приёмник и
  проверка состояния.

## Чего нет

Нет полной модели данных (сущности — словари), подресурсов позиций, асинхронных задач,
скачивания файлов и старого эндпоинта ассортимента. Изменения перечислены в
[CHANGELOG.md](CHANGELOG.md).

## Лицензия

MIT, см. [LICENSE](LICENSE).
