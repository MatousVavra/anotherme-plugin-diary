"""Unit tests for the diary analysis pipeline (explicit note path targeting
and defensive coercion of LLM analysis fields)."""
from unittest.mock import MagicMock

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


class _FakeVault:
    def __init__(self, root):
        self._root = root

    def vault_path(self, name):
        return self._root / name


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
    return plugin


def test_analysis_targets_explicit_note_path(tmp_path):
    plugin = _make_plugin(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    diary_dir.mkdir(parents=True)
    first = diary_dir / "Diary 2026-01-01-090000-000000.md"
    second = diary_dir / "Diary 2026-01-02-090000-000000.md"
    first.write_text("## AI Analysis\n\n_(analyzing…)_", encoding="utf-8")
    second.write_text("## AI Analysis\n\n_(analyzing…)_", encoding="utf-8")

    plugin._update_latest_diary_markdown(
        "main",
        {"mood": "calm", "tags": ["focus"], "summary": "A calm day."},
        "Diary/Diary 2026-01-01-090000-000000.md",
    )

    first_text = first.read_text(encoding="utf-8")
    assert "**Mood:** calm" in first_text
    assert "A calm day." in first_text
    assert "_(analyzing…)_" not in first_text
    assert "_(analyzing…)_" in second.read_text(encoding="utf-8")


def test_clear_placeholder_targets_explicit_note_path(tmp_path):
    plugin = _make_plugin(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    diary_dir.mkdir(parents=True)
    first = diary_dir / "Diary 2026-01-01-090000-000000.md"
    second = diary_dir / "Diary 2026-01-02-090000-000000.md"
    first.write_text("_(analyzing…)_", encoding="utf-8")
    second.write_text("_(analyzing…)_", encoding="utf-8")

    plugin._clear_analysis_placeholder("main", "Diary/Diary 2026-01-01-090000-000000.md")

    assert "_Analysis unavailable._" in first.read_text(encoding="utf-8")
    assert "_(analyzing…)_" in second.read_text(encoding="utf-8")


def test_update_markdown_coerces_malformed_analysis_fields(tmp_path):
    plugin = _make_plugin(tmp_path)
    diary_dir = tmp_path / "main" / "Diary"
    diary_dir.mkdir(parents=True)
    entry = diary_dir / "Diary 2026-01-01-090000-000000.md"
    entry.write_text("_(analyzing…)_", encoding="utf-8")

    plugin._update_latest_diary_markdown(
        "main",
        {"mood": 42, "tags": "notalist", "summary": None},
        "Diary/Diary 2026-01-01-090000-000000.md",
    )

    text = entry.read_text(encoding="utf-8")
    assert "_No summary extracted._" in text
    assert "**Mood:**" not in text
    assert "**Tags:**" not in text


def test_apply_analysis_skips_malformed_people_and_facts(tmp_path):
    plugin = _make_plugin(tmp_path)
    memory = MagicMock()
    plugin._ctx.registry.apis["memory"] = memory

    plugin._apply_analysis("main", {"people": ["just a string"], "facts": "notadict"}, "raw")

    memory.save_person.assert_not_called()
    memory.save_fact.assert_not_called()
    memory.update_identity_facts.assert_not_called()
