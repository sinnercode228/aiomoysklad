"""Every Python sample in README.md and README.ru.md runs against mocked endpoints.

Each ``python`` block follows a ``<!-- sample: name -->`` marker, and both READMEs carry the
same samples with only the comments translated. Fragments with top-level ``await`` run with
``ms``, an open client, and the names they assume (``saved``, ``org``, ``agent``, ``body``,
``request_id``).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import MoySklad

from .conftest import API

ROOT = Path(__file__).resolve().parent.parent
READMES = ("README.md", "README.ru.md")
_MARKED = re.compile(r"<!-- sample: ([a-z0-9-]+) -->\n```python\n(.*?)\n```", re.S)
_FENCE = re.compile(r"^```python$", re.M)
_COMMENT = re.compile(r"#.*$", re.M)


def samples(readme: str) -> dict[str, str]:
    text = (ROOT / readme).read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for match in _MARKED.finditer(text):
        assert match.group(1) not in found, f"{readme}: sample {match.group(1)} appears twice"
        found[match.group(1)] = match.group(2) + "\n"
    assert len(found) == len(_FENCE.findall(text)), f"{readme}: a Python block has no marker"
    return found


def test_both_readmes_carry_the_same_code() -> None:
    english, russian = (samples(name) for name in READMES)
    assert english.keys() == russian.keys()
    for name, code in english.items():
        assert _COMMENT.sub("", code) == _COMMENT.sub("", russian[name]), name


def test_the_english_readme_stays_short() -> None:
    assert len((ROOT / "README.md").read_text(encoding="utf-8").split()) <= 900


@pytest.mark.parametrize("readme", READMES)
def test_both_say_it_was_not_tested_live(readme: str) -> None:
    text = (ROOT / readme).read_text(encoding="utf-8")
    assert ("not been tested against a live" in text) or ("ещё не проверяли" in text)


async def run(code: str, **names: Any) -> dict[str, Any]:
    compiled = compile(code, "<readme>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    namespace: dict[str, Any] = {"__name__": "readme_sample", **names}
    result = eval(compiled, namespace)  # noqa: S307 - the README's own code
    if result is not None:
        await result
    return namespace


def collection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"meta": {"href": API, "size": len(rows), "limit": 1000, "offset": 0}, "rows": rows}


@pytest.mark.parametrize("readme", READMES)
async def test_start(
    readme: str, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    route = mock.get("/entity/product").respond(200, json=collection([{"name": "Товар"}]))
    namespace = await run(sample(readme, "start"))
    await namespace["main"]("token")
    assert route.calls.last.request.headers["Authorization"] == "Bearer token"
    assert capsys.readouterr().out == "Товар\n"


def sample(readme: str, name: str) -> str:
    return samples(readme)[name]


@pytest.mark.parametrize("readme", READMES)
async def test_filter(readme: str) -> None:
    await run(sample(readme, "filter"))


@pytest.mark.parametrize("readme", READMES)
async def test_stock(
    readme: str, ms: MoySklad, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    mock.get("/entity/assortment").respond(
        200, json=collection([{"id": "a", "name": "Товар А"}, {"id": "b", "name": "Товар Б"}])
    )
    mock.get("/report/stock/all/current").respond(200, json=[{"assortmentId": "a", "quantity": 2}])
    await run(sample(readme, "stock"), ms=ms)
    assert capsys.readouterr().out == "Товар А 2.0\nТовар Б 0.0\n"


@pytest.mark.parametrize("readme", READMES)
async def test_orders(readme: str, ms: MoySklad, mock: respx.MockRouter) -> None:
    new = {"meta": {"href": f"{API}/entity/customerorder/metadata/states/s0"}}
    paid = {"meta": {"href": f"{API}/entity/customerorder/metadata/states/s1"}}
    stored = {"id": "o1", "updated": "2026-11-02 10:00:00.000", "sum": 5, "payedSum": 5}
    stored["state"] = new

    def listed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=collection([dict(stored)]))

    def moved(request: httpx.Request) -> httpx.Response:
        # A PUT changes the order's updated, so the next run reads it again.
        stored["state"] = json.loads(request.content)["state"]
        stored["updated"] = "2026-11-02 10:05:00.000"
        return httpx.Response(200, json=dict(stored))

    mock.get("/entity/customerorder").mock(side_effect=listed)
    mock.get("/entity/customerorder/metadata").respond(
        200,
        json={
            "states": [
                {**new, "id": "s0", "name": "Новый"},
                {**paid, "id": "s1", "name": "Оплачен"},
            ]
        },
    )
    put = mock.put("/entity/customerorder/o1").mock(side_effect=moved)
    post = mock.post("/entity/customerorder").respond(200, json={"id": "o2"})
    meta = {"meta": {"href": f"{API}/entity/organization/x"}}
    namespace = await run(sample(readme, "orders"), ms=ms, saved=None, org=meta, agent=meta)
    assert (
        json.loads(put.calls.last.request.content)["state"]["meta"]["href"] == paid["meta"]["href"]
    )
    assert namespace["saved"] == {"updated": "2026-11-02 10:00:00", "seen": ["o1"]}
    assert "syncId" in json.loads(post.calls.last.request.content)
    assert namespace["order"] == {"id": "o2"}

    # The next run reads the order once more, already in the state, and leaves it alone.
    again = await run(
        sample(readme, "orders"), ms=ms, saved=namespace["saved"], org=meta, agent=meta
    )
    assert put.call_count == 1
    assert again["saved"] == {"updated": "2026-11-02 10:05:00", "seen": ["o1"]}


@pytest.mark.parametrize("readme", READMES)
async def test_webhooks(readme: str, ms: MoySklad, mock: respx.MockRouter) -> None:
    url = "https://hooks.example.com/moysklad/a-long-secret"
    hook = {
        "meta": {"href": f"{API}/entity/webhook/h1"},
        "id": "h1",
        "entityType": "customerorder",
        "action": "CREATE",
        "url": url,
        "enabled": True,
    }
    mock.get("/entity/webhook").respond(200, json=collection([]))
    created = mock.post("/entity/webhook").respond(200, json=hook)
    mock.get("/entity/customerorder/o1").respond(200, json={"id": "o1"})
    body = json.dumps(
        {
            "events": [
                {
                    "meta": {"type": "customerorder", "href": f"{API}/entity/customerorder/o1"},
                    "action": "CREATE",
                    "accountId": "a",
                }
            ]
        }
    ).encode()
    await run(sample(readme, "webhooks"), ms=ms, body=body, request_id="r1")
    assert json.loads(created.calls.last.request.content)["url"] == url


def test_the_samples_import_only_public_names() -> None:
    import aiomoysklad

    for name, code in samples("README.md").items():
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.ImportFrom) and node.module == "aiomoysklad":
                for alias in node.names:
                    assert alias.name in aiomoysklad.__all__, (name, alias.name)
