"""Local relevance and bounded excerpts for the ordinary conversation lookup."""

from __future__ import annotations

import json
import re

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core.notes.models import NoteRecord, normalized_name

MAX_CONTEXT_CHARS = 4000
MAX_CONTEXT_RESULTS = 3
MAX_CONTEXT_EXCERPT_CHARS = 600
MIN_CONTEXT_EXCERPT_CHARS = 80

WORD = re.compile(r"\w+(?:[+#]{1,2})?", re.UNICODE)
STOP_WORDS = frozenset(
    "а без бы в во вот вы где да для до его ее если есть еще же за и из или к как ко "
    "ли мне мы на не но о об от по при про с со так то у уже что это я "
    "a an and are as at be by for from in is it of on or that the this to with".split()
)


def topic_words(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        normalized_name(word) for word in WORD.findall(query)
        if normalized_name(word) not in STOP_WORDS or (len(word) > 1 and word.isupper())
    ))


def topic_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    # Match whole words, including technical names such as C# and C++.
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in words) + r")(?![\w+#])")


@dataclass(frozen=True)
class ContextText:
    version: str
    names: tuple[str, ...]
    tags: str
    body: str

    @classmethod
    def from_record(cls, record: NoteRecord) -> ContextText:
        aliases = record.metadata.get("aliases", []) if record.metadata["type"] == "person" else []
        return cls(
            record.version,
            tuple(normalized_name(name) for name in [record.metadata["title"], *aliases]),
            normalized_name(" ".join(record.metadata["tags"])),
            record.body.casefold().replace("ё", "е"),
        )

    def score(self, pattern: re.Pattern[str], phrase: str) -> tuple[int, int, int]:
        names = {match.group() for name in self.names for match in pattern.finditer(name)}
        tags = {match.group() for match in pattern.finditer(self.tags)}
        body = {match.group() for match in pattern.finditer(self.body)}
        # Coverage beats repetition in a long body; exact titles/aliases rank first.
        return int(phrase in self.names), len(names | tags | body), 4 * len(names) + 2 * len(tags) + len(body)


def excerpt(body: str, words: tuple[str, ...]) -> tuple[str, int, bool]:
    """Choose a contiguous window covering the most distinct topic words."""
    wanted = set(words)
    matches = [match for match in WORD.finditer(body) if normalized_name(match.group()) in wanted]
    first = last = best_count = left = 0
    counts: Counter[str] = Counter()
    for right, match in enumerate(matches):
        counts[normalized_name(match.group())] += 1
        while left < right and match.end() - matches[left].start() > MAX_CONTEXT_EXCERPT_CHARS:
            word = normalized_name(matches[left].group())
            counts[word] -= 1
            if not counts[word]:
                del counts[word]
            left += 1
        if len(counts) > best_count:
            first, last, best_count = matches[left].start(), match.end(), len(counts)
    start = max(0, first - 80, last - MAX_CONTEXT_EXCERPT_CHARS)
    start = min(start, max(0, len(body) - MAX_CONTEXT_EXCERPT_CHARS))
    content = body[start:start + MAX_CONTEXT_EXCERPT_CHARS]
    return content, start, start > 0 or start + len(content) < len(body)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def pack_context(
    items: list[dict[str, Any]], *, words: tuple[str, ...] = (),
    has_more: bool = False, index_truncated: bool = False,
) -> str:
    """Bound the complete JSON text, preserving identities and source links intact."""
    payload: dict[str, Any] = {"results": [], "has_more": has_more, "index_truncated": index_truncated}
    wanted = set(words)
    for item in items[:MAX_CONTEXT_RESULTS]:
        payload["results"].append(dict(item))
        if len(_dump(payload)) <= MAX_CONTEXT_CHARS:
            continue
        payload["has_more"] = True
        result = payload["results"][-1]
        content = item["content"]
        result["truncated"] = True
        # Keep the topic itself when a large source/title leaves room for only a
        # short excerpt. The usual 80-character introduction can otherwise fill it.
        focus = next((match for match in WORD.finditer(content) if normalized_name(match.group()) in wanted), None)
        if focus is not None:
            content = content[focus.start():]
            result["offset"] += focus.start()
        minimum = min(MIN_CONTEXT_EXCERPT_CHARS, len(content))
        if focus is not None:
            minimum = max(minimum, focus.end() - focus.start())
        # JSON escaping also counts against the budget (newlines, quotes, etc.).
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            result["content"] = content[:middle]
            if len(_dump(payload)) <= MAX_CONTEXT_CHARS:
                low = middle
            else:
                high = middle - 1
        result["content"] = content[:low]
        if low < minimum or len(_dump(payload)) > MAX_CONTEXT_CHARS:
            payload["results"].pop()
    if len(items) > MAX_CONTEXT_RESULTS:
        payload["has_more"] = True
    return _dump(payload)
