import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(make_client):
    return make_client()


# ===========================================================================
# Diary plugin tests
# ===========================================================================

# --- 1. Plugin listed ---

def test_diary_plugin_listed(client):
    names = {p["name"] for p in client.get("/plugins").json()}
    assert "diary" in names


# --- 2. Create diary entry ---

def test_diary_create(client):
    resp = client.post("/plugins/diary", data={"content": "Today was great."})
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "diary"
    assert data["mood"] is None
    assert data["tags"] == []

    vault_dir = Path(os.environ["VAULTS_DIR"]) / "test-main"
    diary_files = list((vault_dir / "Diary").glob("*.md"))
    assert len(diary_files) == 1


# --- 3. Create diary with audio ---

def test_diary_create_with_audio(client):
    resp = client.post(
        "/plugins/diary",
        data={"content": "Voice diary entry"},
        files={"audio": ("recording.webm", b"fake-audio-data", "audio/webm")},
    )
    assert resp.status_code == 201

    vault_dir = Path(os.environ["VAULTS_DIR"]) / "test-main"
    audio_files = list((vault_dir / "Diary" / "audio").glob("*"))
    assert len(audio_files) == 1


def _get_diary_plugin():
    import src.main
    return src.main.plugin_manager._plugins["diary"]["instance"]


# --- 4. Analysis fires and saves facts via memory API ---

def test_diary_analysis_fires(client):
    save_fact_calls = []

    class FakeMemoryApi:
        def save_fact(self, vault_name, key, value):
            save_fact_calls.append((vault_name, key, value))
        def save_person(self, vault_name, name, rel, notes, tags):
            pass

    import src.main
    registry = src.main.plugin_manager.get_registry()
    original = registry.get_api("memory")
    registry._apis["memory"] = FakeMemoryApi()

    analysis = {
        "mood": "happy",
        "tags": ["personal"],
        "projects": [],
        "people": [],
        "facts": {"hobby": "coding", "city": "Prague"},
        "summary": "Had a good day.",
    }

    async def fake_analyze(*a, **kw):
        return analysis

    async def fake_questions(*a, **kw):
        return []

    diary_plugin = _get_diary_plugin()
    try:
        with patch.object(diary_plugin, "analyze_diary_entry", side_effect=fake_analyze), \
             patch.object(diary_plugin, "generate_follow_up_questions", side_effect=fake_questions), \
             patch.object(diary_plugin, "should_generate_questions", return_value=False):
            resp = client.post("/plugins/diary", data={"content": "Had a good day coding."})
            assert resp.status_code == 201
            asyncio.run(diary_plugin._analyze_and_update("test-main", "Had a good day coding."))
    finally:
        registry._apis["memory"] = original

    assert len(save_fact_calls) >= 2
    keys = {c[1] for c in save_fact_calls}
    assert "hobby" in keys
    assert "city" in keys


# --- 5. diary_saved event emitted ---

def test_diary_saved_event_emitted(client):
    import src.main
    event_bus = src.main.plugin_manager.get_event_bus()

    received = []

    async def handler(payload):
        received.append(payload)

    event_bus.on("diary_saved", handler)

    resp = client.post("/plugins/diary", data={"content": "Event test entry."})
    assert resp.status_code == 201

    for _ in range(50):
        if received:
            break
        client.get("/health")
        time.sleep(0.1)

    assert len(received) >= 1
    assert "content" in received[0]
    assert "Event test entry." == received[0]["content"]


# --- 6. Generate diary from context ---

def test_diary_generate(client):
    with patch("src.llm.analyze_diary") as mock_analyze:
        mock_analyze.return_value = {
            "mood": "productive",
            "tags": ["work"],
            "summary": "Made good progress on [[AnotherMe]].",
        }
        resp = client.post("/plugins/diary/generate", json={"mood": "calm", "tags": ["ai"]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "diary"
    assert data["mood"] == "calm"
    assert "ai" in data["tags"]

    vault_dir = Path(os.environ["VAULTS_DIR"]) / "test-main"
    assert len(list((vault_dir / "Diary").glob("*.md"))) == 1


# --- 7. Latest diary ---

def test_diary_latest(client):
    client.post("/plugins/diary", data={"content": "My latest diary entry."})

    resp = client.get("/plugins/diary/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data
    assert "My latest diary entry." in data["content"]


# --- 8. Timeline ---

def test_diary_timeline(client):
    client.post("/plugins/diary", data={"content": "First entry today.", "mood": "happy"})
    client.post("/plugins/diary", data={"content": "Second entry today.", "mood": "tired"})

    resp = client.get("/plugins/diary/timeline")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) >= 2
    assert "mood" in entries[0]
    assert "preview" in entries[0]
    assert "filename" in entries[0]


# --- 9. Mood ---

def test_diary_mood(client):
    client.post("/plugins/diary", data={"content": "Mood test 1.", "mood": "happy"})
    client.post("/plugins/diary", data={"content": "Mood test 2.", "mood": "tired"})

    resp = client.get("/plugins/diary/mood")
    assert resp.status_code == 200
    points = resp.json()
    moods = {p["mood"] for p in points}
    assert "happy" in moods
    assert "tired" in moods


# --- 10. Old diary endpoint removed ---

# (Old /diary endpoint removed in Phase 7)


# --- 11. Unified timeline ---

def test_unified_timeline_returns_entries(client):
    client.post("/plugins/diary", data={"content": "Diary entry for unified timeline."})
    client.post("/plugins/stories", data={"title": "Story One", "content": "A story for the timeline."})

    resp = client.get("/plugins/diary/unified-timeline")
    assert resp.status_code == 200
    entries = resp.json()
    types = {e["type"] for e in entries}
    assert "diary" in types
    assert "story" in types
    for e in entries:
        assert "type" in e
        assert "title" in e
        assert "date" in e
        assert "preview" in e
        assert "path" in e


def test_unified_timeline_sorted_desc(client):
    import time
    client.post("/plugins/diary", data={"content": "First entry."})
    time.sleep(0.1)
    client.post("/plugins/stories", data={"title": "Later Story", "content": "Second entry."})

    resp = client.get("/plugins/diary/unified-timeline")
    assert resp.status_code == 200
    entries = resp.json()
    if len(entries) >= 2:
        dates = [e.get("date", "") for e in entries]
        assert dates == sorted(dates, reverse=True)


def test_unified_timeline_empty(client):
    resp = client.get("/plugins/diary/unified-timeline")
    assert resp.status_code == 200
    entries = resp.json()
    assert entries == []
