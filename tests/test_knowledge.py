import asyncio

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.entities import Event, ItemKind
from app.core.knowledge import KnowledgeRepository, KnowledgeSearch
from app.core.knowledge.search import build_match_query, fuse_rankings

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def test_build_match_query_cuts_long_word_endings():
    assert build_match_query("Встречи с Петром") == '"встре"* OR "с" OR "петр"*'
    assert build_match_query("дача Wi-Fi") == '"дач"* OR "wi" OR "fi"'
    assert build_match_query("  ,.!  ") is None


def test_fuse_rankings_prefers_items_ranked_by_both():
    fused = fuse_rankings([[("note", "1"), ("note", "2")], [("note", "2"), ("event", "x")]])

    assert [key for key, _ in fused] == [("note", "2"), ("note", "1"), ("event", "x")]


def test_search_finds_notes_and_events_and_follows_changes(tmp_path: Path):
    async def scenario():
        repository = KnowledgeRepository(tmp_path / "kb.db")
        await repository.startup()
        search = KnowledgeSearch(repository, embedder=None)
        try:
            note = await repository.add_note("Бюджет", "Обсудить бюджет на найм", ("работа", ), None, NOW)
            event = Event(id="e1", title="Встреча по бюджету", start=NOW, end=NOW + timedelta(hours=1), location="Офис")
            await repository.upsert_events([event])

            hits = await search.search("бюджеты", list(ItemKind), 10)
            assert {(hit.kind, hit.ref) for hit in hits} == {(ItemKind.NOTE, str(note.id)), (ItemKind.EVENT, "e1")}

            events_only = await search.search("бюджеты", [ItemKind.EVENT], 10)
            assert [hit.ref for hit in events_only] == ["e1"]

            # A renamed event is found by its new title only
            await repository.upsert_events([Event(id="e1", title="Обед", start=NOW, end=NOW + timedelta(hours=1))])
            assert [hit.ref for hit in await search.search("бюджеты", [ItemKind.EVENT], 10)] == []

            # The window sync drops an event gone from the calendar
            changed, removed = await repository.replace_window([], NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert (changed, removed) == (0, 1)
            assert await search.search("обед", list(ItemKind), 10) == []

            await repository.delete_note(note.id)
            assert await search.search("бюджеты", list(ItemKind), 10) == []
        finally:
            await repository.shutdown()

    asyncio.run(scenario())


class _FakeEmbedder:
    """Two-dimensional 'meaning': calls vs everything else"""
    signature = "fake:2"

    async def embed(self, texts):
        import numpy as np
        calls = ("созвон", "звонок", "позвонить")
        vectors = [[1.0, 0.0] if any(word in text.lower() for word in calls) else [0.0, 1.0] for text in texts]
        return np.asarray(vectors, dtype=np.float32)


def test_semantic_search_finds_items_without_shared_words(tmp_path: Path):
    async def scenario():
        repository = KnowledgeRepository(tmp_path / "kb.db")
        await repository.startup()
        search = KnowledgeSearch(repository, embedder=_FakeEmbedder())
        try:
            call = await repository.add_note("Позвонить Ивану", "Позвонить Ивану насчёт аренды", (), None, NOW)
            await repository.add_note("Список покупок", "Молоко, хлеб", (), None, NOW)

            assert await search.index_pending() == 2
            assert await search.index_pending() == 0

            hits = await search.search("созвон", list(ItemKind), 1)
            assert [hit.ref for hit in hits] == [str(call.id)]
        finally:
            await repository.shutdown()

    asyncio.run(scenario())
