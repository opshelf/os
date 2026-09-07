"""Bounded CAS Common Chemistry client; no upstream payloads in errors."""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any

import httpx
from pydantic import BaseModel


class Chemical(BaseModel):
    name: str
    cas_number: str


class ChemicalSearch(BaseModel):
    count: int
    results: list[Chemical]


class ChemicalDetail(Chemical):
    synonyms: list[str]
    source_url: str


class ChemicalUnavailable(Exception):
    pass


class ChemicalNotFound(Exception):
    pass


def normalize_cas(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[1-9][0-9]{4,9}", value):
        value = f"{value[:-3]}-{value[-3:-1]}-{value[-1]}"
    if not re.fullmatch(r"[1-9][0-9]{1,6}-[0-9]{2}-[0-9]", value):
        raise ValueError("Invalid CAS number")
    digits = value.replace("-", "")
    if sum(int(d) * w for w, d in enumerate(reversed(digits[:-1]), 1)) % 10 != int(digits[-1]):
        raise ValueError("Invalid CAS number checksum")
    return value


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid text")
    parser = _PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


class CommonChemistry:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport
        self._cache: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, dict[str, Any]]] = OrderedDict()
        self._pending: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[dict[str, Any]]] = {}
        self._cooldown_until = 0.0

    async def _fetch(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self._api_key:
            raise ChemicalUnavailable()
        key = (path, tuple(sorted(params.items())))
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        if time.monotonic() < self._cooldown_until:
            raise ChemicalUnavailable()
        if key not in self._pending:
            if len(self._pending) >= 32:
                raise ChemicalUnavailable()
            task = asyncio.create_task(self._request(path, params))
            self._pending[key] = task

            def completed(done: asyncio.Task[dict[str, Any]]) -> None:
                self._pending.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    self._cache[key] = (time.monotonic() + 3600, done.result())
                    self._cache.move_to_end(key)
                    while len(self._cache) > 256:
                        self._cache.popitem(last=False)

            task.add_done_callback(completed)
        return await asyncio.shield(self._pending[key])

    async def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(3):
                async with httpx.AsyncClient(transport=self._transport, timeout=3) as client:
                    response = await client.get(
                        f"https://commonchemistry.cas.org/api/{path}", params=params,
                        headers={"X-API-KEY": self._api_key},
                    )
            if response.status_code == 429:
                try:
                    delay = min(300, max(5, int(response.headers.get("Retry-After", "60"))))
                except ValueError:
                    delay = 60
                self._cooldown_until = time.monotonic() + delay
                raise ChemicalUnavailable()
            if response.status_code == 404 and path == "detail":
                raise ChemicalNotFound()
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid response")
            if path == "search":
                self._search_result(data)
            else:
                self._detail_result(data)
            return data
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError, TypeError):
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + 5)
            raise ChemicalUnavailable() from None

    @staticmethod
    def _chemical(data: Any) -> Chemical:
        if not isinstance(data, dict):
            raise ValueError("Invalid chemical")
        name = plain_text(data.get("name"))
        if not name or not isinstance(data.get("rn"), str):
            raise ValueError("Invalid chemical")
        return Chemical(name=name, cas_number=normalize_cas(data["rn"]))

    @classmethod
    def _search_result(cls, data: dict[str, Any]) -> ChemicalSearch:
        count = int(data["count"])
        if count < 0 or not isinstance(data["results"], list):
            raise ValueError("Invalid search response")
        return ChemicalSearch(count=count, results=[cls._chemical(row) for row in data["results"][:3]])

    @classmethod
    def _detail_result(cls, data: dict[str, Any]) -> ChemicalDetail:
        chemical = cls._chemical(data)
        synonyms = data.get("synonyms", [])
        if not isinstance(synonyms, list):
            raise ValueError("Invalid synonyms")
        return ChemicalDetail(
            **chemical.model_dump(),
            synonyms=list(dict.fromkeys(filter(None, (plain_text(value) for value in synonyms)))),
            source_url=f"https://commonchemistry.cas.org/detail?cas_rn={chemical.cas_number}",
        )

    async def search(self, query: str, offset: int = 0) -> ChemicalSearch:
        query = query.strip().replace("–", "-").replace("−", "-")
        prefix = bool(re.fullmatch(r"[1-9][0-9]{1,6}-(?:[0-9]{0,2}|[0-9]{2}-)", query))
        identifier = bool(re.fullmatch(r"[0-9*-]+", query))
        if prefix:
            query += "*"
        elif identifier:
            try:
                query = normalize_cas(query)
            except ValueError:
                return ChemicalSearch(count=0, results=[])
        params = {"q": query, "offset": str(offset), "size": "3"}
        try:
            data = await self._fetch("search", params)
            result = self._search_result(data)
            if result.count == 0 and not identifier and not query.endswith("*"):
                data = await self._fetch("search", {**params, "q": query + "*"})
                result = self._search_result(data)
            return result
        except (KeyError, ValueError, TypeError):
            raise ChemicalUnavailable() from None

    async def detail(self, cas_number: str) -> ChemicalDetail:
        data = await self._fetch("detail", {"cas_rn": normalize_cas(cas_number)})
        try:
            return self._detail_result(data)
        except (KeyError, ValueError, TypeError):
            raise ChemicalUnavailable() from None


@lru_cache(maxsize=1)
def get_common_chemistry() -> CommonChemistry:
    return CommonChemistry(os.getenv("CAS_COMMON_CHEMISTRY_API_KEY", "").strip())
