"""Set up customer order webhooks, receive them, and check that they stay on.

Authorization as in ``stock_sync.py``. ``WEBHOOK_URL`` is the public address of the receiver,
ending in a secret path segment, such as ``https://hooks.example.com/moysklad/<secret>``:
MoySklad does not sign webhooks, so the secret path keeps strangers out, and the receiver
fetches every entity from the API itself instead of trusting the payload.

Create or re-enable the webhooks (an administrator's or a solution's credentials, on a paid
plan or the trial)::

    MOYSKLAD_TOKEN=... WEBHOOK_URL=https://... python examples/webhooks.py setup

Check them from a monitor, and turn them back on after fixing the receiver::

    MOYSKLAD_TOKEN=... WEBHOOK_URL=https://... python examples/webhooks.py health [--reenable]

Serve the receiver with any ASGI server, for example::

    MOYSKLAD_TOKEN=... WEBHOOK_URL=https://... uvicorn --factory webhooks:app_from_env

The app is plain ASGI, no framework. It answers 204 right after parsing and deduping, since
MoySklad waits only 1500 ms, and fetches the entities in a background task.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiomoysklad import (
    Action,
    AuthKind,
    BasicAuth,
    BearerToken,
    Delivery,
    MoySklad,
    StockNotification,
    WebhookPayloadError,
    WebhookReceiver,
)

logger = logging.getLogger("webhooks")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
Handler = Callable[[WebhookReceiver, Delivery], Coroutine[Any, Any, None]]

PREFIX = "/moysklad/"
ACTIONS: tuple[Action, ...] = ("CREATE", "UPDATE", "DELETE")


def auth_from_env() -> BasicAuth | BearerToken:
    token = os.environ.get("MOYSKLAD_TOKEN")
    if token:
        kind = AuthKind(os.environ.get("MOYSKLAD_TOKEN_KIND", "user"))
        return BearerToken(token, kind=kind)
    return BasicAuth(os.environ["MOYSKLAD_LOGIN"], os.environ["MOYSKLAD_PASSWORD"])


async def log_delivery(receiver: WebhookReceiver, delivery: Delivery) -> None:
    """The default handler: fetch what changed and log it."""
    if isinstance(delivery.notification, StockNotification):
        rows = await receiver.fetch_stock(delivery.notification)
        logger.info("stock changed for %d items", len(rows))
        return
    for event in delivery.events:
        entity = await receiver.fetch(event)
        name = entity.get("name") if isinstance(entity, dict) else None
        logger.info("%s %s %s", event.action, event.meta.type, name or event.meta.href)


class WebhookApp:
    """An ASGI app that accepts MoySklad webhooks on ``/moysklad/<secret>``."""

    def __init__(
        self, ms: MoySklad, secret: str, *, handle: Handler = log_delivery, owns_client: bool
    ) -> None:
        if not secret:
            msg = "a webhook secret is required: it is the only thing keeping strangers out"
            raise ValueError(msg)
        self.ms = ms
        self.receiver = WebhookReceiver(ms)
        self.path = PREFIX + secret
        self.handle = handle
        self.tasks: set[asyncio.Task[None]] = set()
        self._owns_client = owns_client

    async def drain(self) -> None:
        """Wait for the background handlers."""
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _done(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("webhook handler failed", exc_info=task.exception())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        if not hmac.compare_digest(scope["path"].encode(), self.path.encode()):
            await respond(send, 404)
            return
        if scope["method"] != "POST":
            await respond(send, 405)
            return
        body = await read_body(receive)
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        request_id = query.get("requestId", [None])[0]
        try:
            delivery = await self.receiver.accept(body, request_id)
        except WebhookPayloadError:
            await respond(send, 400)
            return
        if delivery is not None:
            task = asyncio.create_task(self.handle(self.receiver, delivery))
            self.tasks.add(task)
            task.add_done_callback(self._done)
        await respond(send, 204)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.drain()
                if self._owns_client:
                    await self.ms.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return


async def read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def respond(send: Send, status: int) -> None:
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def create_app(ms: MoySklad, secret: str, *, handle: Handler = log_delivery) -> WebhookApp:
    return WebhookApp(ms, secret, handle=handle, owns_client=False)


def app_from_env() -> WebhookApp:
    url = os.environ["WEBHOOK_URL"]
    secret = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    # Writes made by the handlers do not trigger these webhooks again.
    ms = MoySklad(auth_from_env(), webhook_disable_prefixes=[url])
    return WebhookApp(ms, secret, owns_client=True)


async def setup(ms: MoySklad, url: str) -> None:
    for action in ACTIONS:
        hook = await ms.webhooks.ensure("customerorder", action, url)
        state = "on" if hook.enabled else "off"
        print(f"{hook.action:<7} {hook.entity_type} -> {hook.url} ({state})")
    await health(ms, url)


async def health(ms: MoySklad, url_prefix: str, *, reenable: bool = False) -> bool:
    report = await ms.webhooks.health(url_prefix, reenable=reenable)
    if report.ok:
        print("all webhooks under the prefix are on")
        return True
    for hook in report.disabled:
        print(f"disabled: {hook.id} {hook.url}")
    if report.reenabled:
        print(f"turned {len(report.reenabled)} back on")
    return False


async def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command not in ("setup", "health"):
        print(__doc__)
        return 2
    url = os.environ["WEBHOOK_URL"]
    async with MoySklad(auth_from_env()) as ms:
        if command == "setup":
            await setup(ms, url)
            return 0
        return 0 if await health(ms, url, reenable="--reenable" in argv) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
