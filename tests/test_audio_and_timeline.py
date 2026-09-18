"""Unit tests for the diary audio serving route and the timeline offset param."""
import asyncio
import re
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


class _FakeVault:
    def __init__(self, root):
        self._root = root

    def vault_path(self, name):
        return self._root / name

    def extract_frontmatter_field(self, content, field):
        m = re.search(rf"^{re.escape(field)}:\s*(.*)$", content, re.MULTILINE)
        return m.group(1).strip() if m else None


def _make_plugin(tmp_path):
    vault = _FakeVault(tmp_path)
    ctx = FakePluginContext(
        vault_manager=vault,
        db_module=MagicMock(),
        llm_client=MagicMock(),
        context_builder=MagicMock(),
        tool_executor=MagicMock(),
    )
    plugin = load_plugin_module().Plugin()
    plugin.on_load(ctx)
    return plugin, ctx


def _make_client(tmp_path):
    _, ctx = _make_plugin(tmp_path)
    app = FastAPI()
    for _, router in ctx.registry.routers:
        app.include_router(router, prefix="/plugins/diary")
    return TestClient(app)


def _find_endpoint(ctx, path):
    for _, router in ctx.registry.routers:
        for r in router.routes:
            if getattr(r, "path", None) == path:
                return r.endpoint
    raise AssertionError(f"route {path} not found")


def _write_entry(diary_dir, name, content):
    diary_dir.mkdir(parents=True, exist_ok=True)
    (diary_dir / name).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# GET /plugins/diary/audio/{filename}
# ---------------------------------------------------------------------------

def test_audio_serves_saved_file(tmp_path):
    client = _make_client(tmp_path)
    audio_dir = tmp_path / "main" / "Diary" / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "2026-01-01-120000.webm").write_bytes(b"fake-audio-bytes")

    resp = client.get("/plugins/diary/audio/2026-01-01-120000.webm")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/webm")
    assert resp.content == b"fake-audio-bytes"


def test_audio_rejects_separator_filename_over_http(tmp_path):
    client = _make_client(tmp_path)
    audio_dir = tmp_path / "main" / "Diary" / "audio"
    audio_dir.mkdir(parents=True)

    resp = client.get("/plugins/diary/audio/sub%2fsecret.webm")

    assert resp.status_code in (400, 404)


def test_audio_rejects_path_traversal(tmp_path):
    _, ctx = _make_plugin(tmp_path)
    endpoint = _find_endpoint(ctx, "/audio/{filename}")
    audio_dir = tmp_path / "main" / "Diary" / "audio"
    audio_dir.mkdir(parents=True)
    secret = tmp_path / "main" / "secret.webm"
    secret.write_bytes(b"secret")

    for bad in ("../secret.webm", "..\\secret.webm", "sub/secret.webm", "sub\\secret.webm", "..", ""):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(endpoint(filename=bad))
        assert exc.value.status_code == 400, bad


def test_audio_missing_file_returns_404(tmp_path):
    client = _make_client(tmp_path)

    resp = client.get("/plugins/diary/audio/2026-01-01-120000.webm")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /plugins/diary/timeline?offset=
# ---------------------------------------------------------------------------

def test_timeline_offset_pages_older_entries(tmp_path):
    client = _make_client(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    for i in range(1, 36):
        _write_entry(diary_dir, f"Diary 2026-01-{i:02d}-090000.md", f"Entry number {i}.\n")

    first = client.get("/plugins/diary/timeline").json()
    assert len(first) == 30
    assert first[0]["id"] == "Diary 2026-01-35-090000"
    assert first[-1]["id"] == "Diary 2026-01-06-090000"

    second = client.get("/plugins/diary/timeline?offset=30").json()
    assert len(second) == 5
    assert second[0]["id"] == "Diary 2026-01-05-090000"
    assert second[-1]["id"] == "Diary 2026-01-01-090000"

    first_ids = {e["id"] for e in first}
    assert all(e["id"] not in first_ids for e in second)


def test_timeline_offset_beyond_end_returns_empty(tmp_path):
    client = _make_client(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    _write_entry(diary_dir, "Diary 2026-01-01-090000.md", "Only entry.\n")

    resp = client.get("/plugins/diary/timeline?offset=10")

    assert resp.status_code == 200
    assert resp.json() == []


def test_timeline_rejects_negative_offset(tmp_path):
    client = _make_client(tmp_path)

    resp = client.get("/plugins/diary/timeline?offset=-1")

    assert resp.status_code == 422


def test_timeline_exposes_analyzing_and_audio_file(tmp_path):
    client = _make_client(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    _write_entry(
        diary_dir,
        "Diary 2026-01-01-090000.md",
        "---\naudio: Diary/audio/2026-01-01-090000.webm\n---\n## AI Analysis\n\n_(analyzing…)_\n",
    )
    _write_entry(
        diary_dir,
        "Diary 2026-01-02-090000.md",
        "---\nmood: happy\n---\nFinished a great book.\n",
    )

    entries = client.get("/plugins/diary/timeline").json()

    by_id = {e["id"]: e for e in entries}
    pending = by_id["Diary 2026-01-01-090000"]
    assert pending["analyzing"] is True
    assert pending["audio"] is True
    assert pending["audio_file"] == "2026-01-01-090000.webm"
    assert pending["mood"] is None

    done = by_id["Diary 2026-01-02-090000"]
    assert done["analyzing"] is False
    assert done["mood"] == "happy"


def test_timeline_mood_falls_back_to_analysis_body(tmp_path):
    client = _make_client(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    _write_entry(
        diary_dir,
        "Diary 2026-01-01-090000.md",
        "## AI Analysis\n\n**Mood:** calm\n\nA calm day.\n",
    )

    entries = client.get("/plugins/diary/timeline").json()

    assert entries[0]["mood"] == "calm"
