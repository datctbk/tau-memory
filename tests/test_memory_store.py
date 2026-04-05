"""Tests for the MemoryStore — file-based CRUD and truncation."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAU_ROOT = ROOT.parent / "tau"
sys.path.insert(0, str(TAU_ROOT))

import importlib.util

_mod_name = "_tau_ext_memory_store"
_spec = importlib.util.spec_from_file_location(
    _mod_name,
    str(ROOT / "extensions" / "memory" / "extension.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_mod_name] = _mod
_spec.loader.exec_module(_mod)

MemoryStore = _mod.MemoryStore
ENTRYPOINT_NAME = _mod.ENTRYPOINT_NAME
MAX_ENTRYPOINT_LINES = _mod.MAX_ENTRYPOINT_LINES
MAX_ENTRYPOINT_BYTES = _mod.MAX_ENTRYPOINT_BYTES


# ---------------------------------------------------------------------------
# Directory & initialization
# ---------------------------------------------------------------------------

class TestMemoryStoreInit:
    def test_root_path(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.root == tmp_path / ".tau" / "memory"

    def test_entrypoint_path(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.entrypoint == tmp_path / ".tau" / "memory" / ENTRYPOINT_NAME

    def test_ensure_dir_creates_directory(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert not store.root.is_dir()
        store.ensure_dir()
        assert store.root.is_dir()

    def test_exists_false_when_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.exists() is False

    def test_exists_true_when_memory_md(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.ensure_dir()
        store.entrypoint.write_text("# index", encoding="utf-8")
        assert store.exists() is True


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class TestMemoryStoreRead:
    def test_read_entrypoint_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.read_entrypoint() == ""

    def test_read_entrypoint_content(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.ensure_dir()
        store.entrypoint.write_text("# Memory\n- item 1\n- item 2\n", encoding="utf-8")
        content = store.read_entrypoint()
        assert "item 1" in content
        assert "item 2" in content

    def test_read_topic_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.read_topic("user") == ""

    def test_read_topic_content(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.ensure_dir()
        (store.root / "user.md").write_text("# User\n\nPrefers dark mode.", encoding="utf-8")
        content = store.read_topic("user")
        assert "dark mode" in content

    def test_list_topics_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.list_topics() == []

    def test_list_topics_returns_metadata(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.ensure_dir()
        (store.root / "user.md").write_text("# User\nLine 2\n", encoding="utf-8")
        (store.root / "project.md").write_text("# Project\n", encoding="utf-8")
        topics = store.list_topics()
        assert len(topics) == 2
        names = {t["name"] for t in topics}
        assert names == {"user", "project"}
        for t in topics:
            assert "size_bytes" in t
            assert "modified" in t
            assert "lines" in t


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

class TestMemoryStoreSave:
    def test_save_creates_topic_file(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        path = store.save_memory("Test", "Content here", "user")
        assert Path(path).is_file()
        content = Path(path).read_text(encoding="utf-8")
        assert "Test" in content
        assert "Content here" in content

    def test_save_creates_memory_md(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("My Note", "Details", "project")
        assert store.entrypoint.is_file()
        index = store.entrypoint.read_text(encoding="utf-8")
        assert "My Note" in index

    def test_save_appends_to_existing_topic(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("First", "AAA", "feedback")
        store.save_memory("Second", "BBB", "feedback")
        content = (store.root / "feedback.md").read_text(encoding="utf-8")
        assert "First" in content
        assert "Second" in content
        assert "AAA" in content
        assert "BBB" in content

    def test_save_updates_index(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("Entry 1", "data", "user")
        store.save_memory("Entry 2", "data", "project")
        index = store.entrypoint.read_text(encoding="utf-8")
        assert "Entry 1" in index
        assert "Entry 2" in index

    def test_save_deduplicates_index(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("Dupe", "data", "user")
        store.save_memory("Dupe", "data2", "user")
        index = store.entrypoint.read_text(encoding="utf-8")
        assert index.count("Dupe") == 1  # only one index entry

    def test_save_custom_topic(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("Auth info", "Use OAuth2", "reference", topic="auth")
        assert (store.root / "auth.md").is_file()

    def test_save_includes_date(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("Dated", "Content", "project")
        content = (store.root / "project.md").read_text(encoding="utf-8")
        assert "saved:" in content

    def test_save_includes_type(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.save_memory("Typed", "Content", "feedback")
        content = (store.root / "feedback.md").read_text(encoding="utf-8")
        assert "type: feedback" in content


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_no_truncation_for_small_content(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        result = store._truncate("Short content\nLine 2\n")
        assert "WARNING" not in result

    def test_truncate_by_lines(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        huge = "\n".join(f"Line {i}" for i in range(300))
        result = store._truncate(huge)
        assert "WARNING" in result
        assert "truncated" in result
        # Should have roughly MAX_ENTRYPOINT_LINES content lines
        content_before_warning = result.split("> WARNING")[0]
        assert content_before_warning.count("\n") <= MAX_ENTRYPOINT_LINES + 5

    def test_truncate_by_bytes(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        huge = "A" * (MAX_ENTRYPOINT_BYTES + 5000)
        result = store._truncate(huge)
        assert "WARNING" in result


# ---------------------------------------------------------------------------
# Dream prompt
# ---------------------------------------------------------------------------

class TestDreamPrompt:
    def test_dream_prompt_contains_memory_dir(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        prompt = store.get_dream_prompt()
        assert str(store.root) in prompt

    def test_dream_prompt_has_phases(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        prompt = store.get_dream_prompt()
        assert "Phase 1" in prompt or "Orient" in prompt
        assert "Consolidate" in prompt
