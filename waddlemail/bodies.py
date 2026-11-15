"""Hybrid body access: serve from the local cache, otherwise fetch from Graph and cache."""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .graph import GraphClient
from .store import Store


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

    def get(self, message_id: str) -> Body:
        cached = self.store.get_body(message_id)
        if cached:
            return Body(cached[0], cached[1], True)
        content_type, content = self.graph.get_message_body(message_id)
        self.store.put_body(message_id, content_type, content, self.max_bytes)
        return Body(content_type, content, False)
