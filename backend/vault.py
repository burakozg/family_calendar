"""The hobby vault's CouchDB, in Self-hosted LiveSync's document format.

Vendored from `clippings-topics/clippings_topics/vault.py` (itself ported
from `taster/backend/app/couchdb_client.py`, the canonical implementation) —
see `~/.claude/skills/obsidian-vault-writer`. Trimmed to just the write path:
this app only ever creates/updates its own whole-file notes, never reads
another writer's content and never reaps duplicates, so `list_prefix`/
`read`/`soft_delete` aren't needed here.

Two documents per file:

* a **chunk**, content-addressed, so identical text is stored once;
* an **entry**, keyed by the *lowercased* vault path, listing its chunks.

Two plugin settings must stay off, and both break this silently: ``encrypt``
(E2EE) and ``usePathObfuscation``. We write plaintext chunks keyed by path.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_CHUNK_PREFIX = "h:t"


class VaultUnavailable(Exception):
    """The vault database cannot be reached, or refused the write."""


@dataclass(frozen=True)
class VaultConfig:
    couchdb_url: str = ""
    db: str = "hobby"
    user: str = ""
    timeout_s: float = 30.0


def _chunk_id(content: str) -> str:
    return _CHUNK_PREFIX + hashlib.sha1(content.encode("utf-8")).hexdigest()[:24]  # noqa: S324


def _q(doc_id: str) -> str:
    return quote(doc_id, safe="")


class LiveSyncVault:
    def __init__(self, cfg: VaultConfig, password: str | None) -> None:
        self._cfg = cfg
        base = (cfg.couchdb_url or "").rstrip("/")
        self._client = (
            httpx.AsyncClient(base_url=base, auth=(cfg.user, password or ""), timeout=cfg.timeout_s)
            if base
            else None
        )

    @property
    def name(self) -> str:
        return f"vault:{(self._cfg.couchdb_url or '').rstrip('/')}/{self._cfg.db}"

    async def project(self, path: str, markdown: str, *, mtime_ms: int) -> bool:
        """Write one whole-file note. False when nothing needed writing."""
        if self._client is None:
            raise VaultUnavailable("vault.couchdb_url is not set")

        current = await self._existing_markdown(path.lower())
        if current is not None and markdown == current:
            return False  # already says exactly this

        chunk_id = _chunk_id(markdown)
        await self._put_chunk(chunk_id, markdown)

        entry: dict[str, Any] = {
            "_id": path.lower(),
            "path": path,
            "children": [chunk_id],
            "ctime": mtime_ms,
            "mtime": mtime_ms,
            "size": len(markdown.encode("utf-8")),
            "type": "plain",
            "eden": {},
        }
        written = await self._put_entry(entry)
        if written:
            log.info("vault.projected path=%s bytes=%d", path, entry["size"])
        return written

    async def _existing_markdown(self, entry_id: str) -> str | None:
        entry = await self._get(entry_id)
        if entry is None or entry.get("deleted"):
            return None
        return await self._markdown_from([str(c) for c in (entry.get("children") or [])])

    async def _markdown_from(self, children: list[str]) -> str | None:
        parts = []
        for chunk_id in children:
            chunk = await self._get(str(chunk_id))
            if chunk is None:
                return None
            parts.append(str(chunk.get("data") or ""))
        return "".join(parts)

    async def _put_chunk(self, chunk_id: str, markdown: str) -> None:
        body = {"_id": chunk_id, "data": markdown, "type": "leaf"}
        response = await self._put(chunk_id, body)
        if response.status_code != 409:
            return
        existing = await self._get(chunk_id)
        if existing is not None and not existing.get("deleted"):
            return
        rev = existing.get("_rev") if existing else await self._tombstone_rev(chunk_id)
        if rev is None:
            raise VaultUnavailable(f"conflict on chunk {chunk_id} with no revision to take over")
        await self._put_or_raise(chunk_id, {**body, "_rev": rev})

    async def _put_entry(self, entry: dict[str, Any]) -> bool:
        entry_id = str(entry["_id"])
        response = await self._put(entry_id, entry)
        if response.status_code != 409:
            return True
        existing = await self._get(entry_id)
        if existing is None:
            log.info("vault.skipped_deleted path=%s deletion=raced", entry["path"])
            return False
        if existing.get("deleted"):
            log.info("vault.skipped_deleted path=%s deletion=soft", entry["path"])
            return False
        if list(existing.get("children") or []) == entry["children"]:
            return False
        await self._put_or_raise(
            entry_id,
            {**entry, "_rev": existing["_rev"], "ctime": existing.get("ctime") or entry["ctime"]},
        )
        return True

    async def _put(self, doc_id: str, body: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        try:
            response = await self._client.put(f"/{self._cfg.db}/{_q(doc_id)}", json=body)
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (201, 202, 409):
            return response
        raise VaultUnavailable(
            f"{self.name} refused a write: HTTP {response.status_code} {response.text[:200]}"
        )

    async def _put_or_raise(self, doc_id: str, body: dict[str, Any]) -> None:
        response = await self._put(doc_id, body)
        if response.status_code == 409:
            raise VaultUnavailable(f"{self.name}: repeated conflict writing {doc_id}")

    async def _get(self, doc_id: str) -> dict[str, Any] | None:
        assert self._client is not None
        try:
            response = await self._client.get(f"/{self._cfg.db}/{_q(doc_id)}")
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise VaultUnavailable(
                f"{self.name} refused a read: HTTP {response.status_code} {response.text[:200]}"
            )
        doc: dict[str, Any] = response.json()
        return doc

    async def _tombstone_rev(self, doc_id: str) -> str | None:
        assert self._client is not None
        try:
            response = await self._client.get(
                f"/{self._cfg.db}/{_q(doc_id)}",
                params={"open_revs": "all"},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise VaultUnavailable(f"{self.name} unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            return None
        for row in response.json():
            ok = row.get("ok") if isinstance(row, dict) else None
            if isinstance(ok, dict) and ok.get("_rev"):
                rev: str = ok["_rev"]
                return rev
        return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
