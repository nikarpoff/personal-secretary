"""Bounded note retrieval and Markdown sections; no file mutations."""

import re

from datetime import datetime, timezone

from markdown_it import MarkdownIt

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.notes.context import MAX_CONTEXT_RESULTS, ContextText, excerpt, pack_context, topic_pattern, topic_words
from app.core.notes.index import NotesIndex
from app.core.notes.models import KINDS, MAX_READ_CHARS, MAX_RESULTS, MAX_SNIPPET_CHARS, STATUSES, NoteRecord, NotesConflictError, normalized_name


class NoteQueries:
    def __init__(self, index: NotesIndex) -> None:
        self.index = index
        self._context_cache: dict[str, ContextText] = {}
        self._markdown = MarkdownIt("commonmark")

    def clear(self) -> None:
        self._context_cache.clear()

    @staticmethod
    def _filters(kind: str | None, tag: str | None, status: str | None = None) -> None:
        if kind is not None and kind not in KINDS:
            raise InvalidRequestError("Unsupported note type")
        if status is not None and status not in STATUSES:
            raise InvalidRequestError("Unsupported note status")
        if tag is not None and (not isinstance(tag, str) or len(tag) > 40):
            raise InvalidRequestError("Tag must be at most 40 characters")

    @staticmethod
    def _context_visible(record: NoteRecord, kind: str | None, tag: str | None) -> bool:
        return (not record.archived and (kind is None or record.metadata["type"] == kind)
                and (tag is None or tag in record.metadata["tags"])
                and (record.metadata["type"] != "task" or record.metadata["status"] == "open"))

    def get_context(self, query: str, kind: str | None = None, tag: str | None = None) -> str:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise InvalidRequestError("Context query must contain 1..200 characters of topic words")
        self._filters(kind, tag)
        if tag == "":
            raise InvalidRequestError("Tag must contain 1..40 characters")
        words, phrase = topic_words(query), normalized_name(query)
        self.index.refresh()
        if self.index.truncated or not words:
            return pack_context([], index_truncated=self.index.truncated)
        pattern = topic_pattern(words)
        self._context_cache = {key: value for key, value in self._context_cache.items() if key in self.index.by_id}
        candidates = []
        for record in self.index.by_id.values():
            if not self._context_visible(record, kind, tag):
                continue
            text = self._context_cache.get(record.id)
            if text is None or text.version != record.version:
                text = self._context_cache[record.id] = ContextText.from_record(record)
            score = text.score(pattern, phrase)
            if score[1]:
                updated = datetime.fromisoformat(record.metadata["updated_at"]).astimezone(timezone.utc)
                candidates.append((score, updated, record.id, record))
        candidates.sort(key=lambda candidate: candidate[:3], reverse=True)
        items = []
        for _, _, note_id, indexed in candidates[:MAX_CONTEXT_RESULTS]:
            try:
                current = self.index.current(indexed)
                if not self._context_visible(current, kind, tag):
                    continue
                text = self._context_cache[note_id] = ContextText.from_record(current)
                if not text.score(pattern, phrase)[1]:
                    continue
                content, offset, truncated = excerpt(current.body, words)
                item = {"id": current.id, "title": current.metadata["title"], "type": current.metadata["type"],
                        "status": current.metadata["status"], "source": self.index.files.source(current),
                        "version": current.version, "content": content, "offset": offset, "truncated": truncated}
                if current.metadata["type"] == "task":
                    item.update({key: current.metadata[key] for key in ("due_at", "remind_at") if key in current.metadata})
                items.append(item)
            except (OSError, InvalidRequestError, NotFoundError, NotesConflictError):
                continue
        return pack_context(items, words=words, has_more=len(candidates) > len(items))

    def search(self, query: str = "", kind: str | None = None, status: str | None = None,
               tag: str | None = None, include_archive: bool = False, limit: int = MAX_RESULTS) -> dict:
        if not isinstance(query, str) or len(query) > 200:
            raise InvalidRequestError("Search query must be at most 200 characters")
        self._filters(kind, tag, status)
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise InvalidRequestError("Search limit must be within 1..5")
        self.index.refresh()
        normalized_query = normalized_name(query)
        words, matches = normalized_query.split(), []
        for record in self.index.by_id.values():
            if record.archived and not include_archive:
                continue
            if kind is not None and record.metadata["type"] != kind:
                continue
            effective_status = "archived" if record.archived else record.metadata["status"]
            if status is not None and effective_status != status:
                continue
            if tag is not None and tag not in record.metadata["tags"]:
                continue
            aliases = record.metadata["aliases"] if record.metadata["type"] == "person" else []
            names = [normalized_name(name) for name in [record.metadata["title"], *aliases]]
            haystack = normalized_name(f"{' '.join(names)}\n{' '.join(record.metadata['tags'])}\n{record.body}")
            if not all(word in haystack for word in words):
                continue
            score = sum(any(word in name for name in names) for word in words)
            if record.metadata["type"] == "person" and normalized_query and normalized_query in names:
                score += 100
            updated = datetime.fromisoformat(record.metadata["updated_at"]).astimezone(timezone.utc)
            matches.append((score, updated, record))
        matches.sort(key=lambda match: (match[0], match[1], match[2].id), reverse=True)
        results = []
        for _, _, record in matches[:limit]:
            snippet = " ".join(record.body.split())
            if words and (position := normalized_name(snippet).find(words[0])) > 60:
                snippet = "…" + snippet[position - 60:]
            results.append({**self.index.summary(record), "snippet": snippet[:MAX_SNIPPET_CHARS]})
        return {"results": results, "has_more": len(matches) > limit, "index_truncated": self.index.truncated}

    def _section(self, body: str, section: str) -> str:
        if not isinstance(section, str) or not section.strip() or len(section) > 200:
            raise InvalidRequestError("Section must be a heading up to 200 characters")
        offsets = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", body))]
        tokens, headings = self._markdown.parse(body), []
        for position, token in enumerate(tokens[:-1]):
            if token.type == "heading_open" and token.map is not None:
                headings.append((offsets[token.map[0]], int(token.tag[1:]), tokens[position + 1].content))
        chosen = next((heading for heading in headings if heading[2].casefold() == section.strip().casefold()), None)
        if chosen is None:
            raise NotFoundError("Section heading not found")
        end = next((start for start, level, _ in headings if start > chosen[0] and level <= chosen[1]), len(body))
        return body[chosen[0]:end]

    def read(self, note_id: str, offset: int = 0, limit: int = MAX_READ_CHARS, section: str | None = None) -> dict:
        if type(offset) is not int or not 0 <= offset <= 128_000:
            raise InvalidRequestError("Read offset must be within 0..128000")
        if type(limit) is not int or not 1 <= limit <= MAX_READ_CHARS:
            raise InvalidRequestError("Read limit must be within 1..4000 characters")
        record = self.index.lookup(note_id)
        body = record.body if section is None else self._section(record.body, section)
        return {**self.index.summary(record), "created_at": record.metadata["created_at"], "updated_at": record.metadata["updated_at"],
                "due_at": record.metadata.get("due_at"), "remind_at": record.metadata.get("remind_at"),
                "content": body[offset:offset + limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(body) else None}
