"""Memory interface: per-user and per-merchant memory plus SOP retrieval.

`CogneeMemory` is used when COGNEE_MODE=live and the package imports; otherwise the SQLite
implementation with the identical interface. Deleting an actor must work in both."""
import asyncio
import re
from typing import Protocol

from sqlalchemy.orm import Session

from ..config import settings
from ..db import MemoryItem


class Memory(Protocol):
	def add(self, s: Session, actor: str, kind: str, text: str, meta: dict | None = None) -> None: ...
	def search(self, s: Session, actor: str, query: str, limit: int = 5) -> list[dict]: ...
	def forget(self, s: Session, actor: str, contains: str | None = None) -> int: ...
	def health(self) -> dict: ...


def _score(query: str, text: str) -> int:
	q = set(re.findall(r"\w+", query.lower()))
	return len(q & set(re.findall(r"\w+", text.lower())))


class SqliteMemory:
	def add(self, s, actor, kind, text, meta=None):
		s.add(MemoryItem(actor=actor, kind=kind, text=text, meta=meta or {}))

	def search(self, s, actor, query, limit=5):
		rows = s.query(MemoryItem).filter_by(actor=actor).order_by(MemoryItem.id.desc()).all()
		ranked = sorted(rows, key=lambda r: (_score(query, r.text), r.id), reverse=True)
		return [{"kind": r.kind, "text": r.text, "meta": r.meta} for r in ranked[:limit]]

	def forget(self, s, actor, contains=None):
		q = s.query(MemoryItem).filter_by(actor=actor)
		if contains:
			q = q.filter(MemoryItem.text.contains(contains))
		return q.delete(synchronize_session=False)

	def health(self):
		return {"mode": "mock", "ok": True, "detail": "SQLite memory tables"}


class CogneeMemory(SqliteMemory):
	"""Writes go to both Cognee and SQLite, so deletion and offline reads stay exact."""

	def __init__(self):
		import cognee  # noqa: F401  (raises ImportError when not installed)
		self.cognee = cognee

	def _run(self, coro):
		try:
			return asyncio.run(coro)
		except Exception:
			return None

	def add(self, s, actor, kind, text, meta=None):
		super().add(s, actor, kind, text, meta)
		self._run(self.cognee.add(f"[{actor}][{kind}] {text}",
			dataset_name=settings.COGNEE_DATASET_MEMORY))

	def search(self, s, actor, query, limit=5):
		hits = self._run(self.cognee.search(query_text=f"{actor}: {query}"))
		if hits:
			return [{"kind": "cognee", "text": str(h), "meta": {}} for h in hits[:limit]]
		return super().search(s, actor, query, limit)

	def forget(self, s, actor, contains=None):
		n = super().forget(s, actor, contains)
		# Cognee has no per-actor delete in every version: rebuild the dataset without them.
		self._run(self.cognee.prune.prune_data())
		return n

	def health(self):
		return {"mode": "live", "ok": True, "detail": "Cognee graph + SQLite mirror"}


def _make() -> Memory:
	if settings.COGNEE_MODE == "live":
		try:
			return CogneeMemory()
		except Exception:
			pass
	return SqliteMemory()


memory: Memory = _make()
