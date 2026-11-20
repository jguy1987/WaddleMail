"""Hybrid body access: serve from the local cache, otherwise fetch from Graph and cache.

Pictures an HTML body links to with cid: (its inline attachments) are fetched once, stored
beside the cached body, and put into the content as data: URIs, so they show offline too.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config
from .graph import GraphClient
from .inline import cid_refs, resolve_cids
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Body:
    content_type: str  # "html" or "text"
    content: str
    from_cache: bool


class BodyService:
    def __init__(self, store: Store, graph: GraphClient,
                 max_bytes: int = config.BODY_CACHE_MAX_BYTES) -> None:
        self.store = store
        self.graph = graph
        self.max_bytes = max_bytes
        self._asked: set[str] = set()  # messages whose missing pictures the server was asked for

    def get(self, message_id: str) -> Body:
        cached = self.store.get_body(message_id)
        if cached:
            body = Body(cached[0], cached[1], True)
        else:
            content_type, content = self.graph.get_message_body(message_id)
            self.store.put_body(message_id, content_type, content, self.max_bytes)
            body = Body(content_type, content, False)
        if body.content_type == "html":
            body.content = self._pictures(message_id, body.content)
        return body

    def with_pictures(self, message_id: str, content: str) -> str:
        """`content` (an HTML body) with the pictures it links to put in as data: URIs."""
        return self._pictures(message_id, content)

    def _pictures(self, message_id: str, content: str) -> str:
        wanted = cid_refs(content)
        if not wanted:
            return content
        images = self.store.inline_images(message_id)
        missing = wanted - images.keys()
        if missing and message_id not in self._asked:  # once a session: some never resolve
            try:
                fetched = self.graph.inline_images(self.graph.message_path(message_id),
                                                   missing)
            except Exception as exc:  # offline, say: show what we have; try again next time
                log.info("pictures of %s not fetched: %s", message_id, exc)
            else:
                self._asked.add(message_id)
                self.store.put_inline_images(message_id, fetched, self.max_bytes)
                images.update(fetched)
        return resolve_cids(content, images)
