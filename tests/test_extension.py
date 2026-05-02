"""Tests for MemoryExtension — tools, slash commands, prompt injection."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAU_ROOT = ROOT.parent / "tau"
sys.path.insert(0, str(TAU_ROOT))

import importlib.util

_mod_name = "_tau_ext_memory_ext"
_spec = importlib.util.spec_from_file_location(
    _mod_name,
    str(ROOT / "extensions" / "memory" / "extension.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_mod_name] = _mod
_spec.loader.exec_module(_mod)

MemoryExtension = _mod.MemoryExtension
MemoryStore = _mod.MemoryStore
MEMORY_TYPES = _mod.MEMORY_TYPES
GLOBAL_MEMORY_TYPES = _mod.GLOBAL_MEMORY_TYPES
_build_memory_prompt = _mod._build_memory_prompt

from tau.core.types import Message, TokenUsage, ToolResult, ToolResultEvent, TurnComplete


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ext_with_store(tmp_path):
    """Extension with a real MemoryStore on a tmp workspace."""
    e = MemoryExtension()
    ctx = MagicMock()
    ctx.print = MagicMock()
    ctx.enqueue = MagicMock()
    ctx.inject_prompt_fragment = MagicMock()
    e._ext_context = ctx
    e._store = MemoryStore(str(tmp_path), global_root=str(tmp_path / ".tau" / "memory-global"))
    return e, ctx, tmp_path


@pytest.fixture
def ctx_mock():
    ctx = MagicMock()
    ctx.print = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_name(self):
        assert MemoryExtension.manifest.name == "memory"

    def test_version(self):
        assert MemoryExtension.manifest.version == "0.1.0"


# ---------------------------------------------------------------------------
# Tools registration
# ---------------------------------------------------------------------------

class TestToolsRegistration:
    def test_registers_tools(self, ext_with_store):
        ext, _, _ = ext_with_store
        tools = ext.tools()
        assert len(tools) >= 4

    def test_tool_names(self, ext_with_store):
        ext, _, _ = ext_with_store
        names = {t.name for t in ext.tools()}
        assert {"memory_save", "memory_read", "memory_query", "memory_extract_session", "memory_search"}.issubset(names)

    def test_memory_save_params(self, ext_with_store):
        ext, _, _ = ext_with_store
        save_tool = next(t for t in ext.tools() if t.name == "memory_save")
        assert "title" in save_tool.parameters
        assert "content" in save_tool.parameters
        assert "memory_type" in save_tool.parameters
        assert "confidence" in save_tool.parameters
        assert "source" in save_tool.parameters
        assert "explicitness" in save_tool.parameters

    def test_memory_read_params(self, ext_with_store):
        ext, _, _ = ext_with_store
        read_tool = next(t for t in ext.tools() if t.name == "memory_read")
        assert "topic" in read_tool.parameters


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

class TestSlashCommands:
    def test_registers_two_commands(self, ext_with_store):
        ext, _, _ = ext_with_store
        cmds = ext.slash_commands()
        assert len(cmds) == 2

    def test_command_names(self, ext_with_store):
        ext, _, _ = ext_with_store
        names = {c.name for c in ext.slash_commands()}
        assert names == {"memory", "dream"}

    def test_handle_memory(self, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        assert ext.handle_slash("memory", "", ctx_mock) is True

    def test_handle_dream(self, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        assert ext.handle_slash("dream", "", ctx_mock) is True

    def test_handle_unknown(self, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        assert ext.handle_slash("other", "", ctx_mock) is False


# ---------------------------------------------------------------------------
# memory_save handler
# ---------------------------------------------------------------------------

class TestMemorySaveHandler:
    def test_save_success(self, ext_with_store):
        ext, _, tmp_path = ext_with_store
        result = ext._handle_memory_save(
            title="User is a developer",
            content="Senior Python developer, 10 years experience.",
            memory_type="user",
        )
        assert "saved" in result.lower()
        assert (tmp_path / ".tau" / "memory-global" / "user.md").is_file()

    def test_save_invalid_type(self, ext_with_store):
        ext, _, _ = ext_with_store
        result = ext._handle_memory_save(
            title="Test", content="Data", memory_type="invalid"
        )
        assert "Error" in result
        assert "invalid" in result.lower()

    def test_save_all_types(self, ext_with_store):
        ext, _, tmp_path = ext_with_store
        for mtype in MEMORY_TYPES:
            result = ext._handle_memory_save(
                title=f"Test {mtype}", content="Content", memory_type=mtype
            )
            assert "saved" in result.lower()
            if mtype in GLOBAL_MEMORY_TYPES:
                assert (tmp_path / ".tau" / "memory-global" / f"{mtype}.md").is_file()
            else:
                assert (tmp_path / ".tau" / "memory" / f"{mtype}.md").is_file()

    def test_save_with_confidence_metadata(self, ext_with_store):
        ext, _, tmp_path = ext_with_store
        result = ext._handle_memory_save(
            title="Preference",
            content="User explicitly requested concise responses.",
            memory_type="user",
            confidence=0.95,
            source="user-explicit",
            explicitness="explicit",
        )
        assert "saved" in result.lower()
        text = (tmp_path / ".tau" / "memory-global" / "user.md").read_text(encoding="utf-8")
        assert "confidence: 0.95" in text
        assert "source: user-explicit" in text

    def test_save_with_session_and_tags_in_structured_log(self, ext_with_store):
        ext, _, tmp_path = ext_with_store
        result = ext._handle_memory_save(
            title="Session fact",
            content="User asked to keep responses concise.",
            memory_type="feedback",
            session_id="sess_1",
            tags=["style", "concise"],
        )
        assert "saved" in result.lower()
        log_path = tmp_path / ".tau" / "memory-global" / "memory_records.jsonl"
        assert log_path.is_file()
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(r.get("session_id") == "sess_1" for r in rows)
        assert any("concise" in r.get("tags", []) for r in rows)

    def test_save_no_store(self):
        ext = MemoryExtension()
        ext._store = None
        result = ext._handle_memory_save("T", "C", "user")
        assert "Error" in result

    def test_save_strict_policy_denies_missing_fields(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._write_policy_strict = True
        ext._require_source = True
        ext._require_confidence = True
        ext._require_why_saved = True
        result = ext._handle_memory_save(
            title="Denied",
            content="Should be denied by policy",
            memory_type="project",
        )
        assert "denied by policy" in result

    def test_save_policy_warning_when_not_strict(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._write_policy_strict = False
        ext._require_source = True
        ext._require_confidence = True
        ext._require_why_saved = True
        result = ext._handle_memory_save(
            title="Warned",
            content="Should save with warning",
            memory_type="project",
        )
        assert "saved with policy warnings" in result.lower()

    def test_save_audit_log_written(self, ext_with_store, tmp_path):
        ext, _, _ = ext_with_store
        ext._write_policy_strict = True
        ext._require_source = True
        ext._require_confidence = True
        ext._require_why_saved = True

        _ = ext._handle_memory_save(
            title="Denied audit",
            content="x",
            memory_type="project",
        )
        _ = ext._handle_memory_save(
            title="Allowed audit",
            content="y",
            memory_type="project",
            source="user-explicit",
            confidence=0.9,
            why_saved="user explicitly asked to remember this",
        )
        p = tmp_path / ".tau" / "memory" / "memory_audit.jsonl"
        assert p.is_file()
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        decisions = [r.get("decision") for r in rows]
        assert "deny" in decisions
        assert "allow" in decisions


# ---------------------------------------------------------------------------
# memory_read handler
# ---------------------------------------------------------------------------

class TestMemoryReadHandler:
    def test_read_list_empty(self, ext_with_store):
        ext, _, _ = ext_with_store
        result = ext._handle_memory_read(topic="list")
        assert "No memory files" in result

    def test_read_list_with_data(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("Test", "Data", "user")
        result = ext._handle_memory_read(topic="list")
        assert "user.md" in result
        assert "MEMORY.md" in result
        assert "global" in result

    def test_read_index(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("My Entry", "Data", "project")
        result = ext._handle_memory_read(topic="index")
        assert "My Entry" in result

    def test_read_index_empty(self, ext_with_store):
        ext, _, _ = ext_with_store
        result = ext._handle_memory_read(topic="index")
        assert "No workspace memory index" in result

    def test_read_topic(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("Preference", "Dark mode", "user")
        result = ext._handle_memory_read(topic="user")
        assert "Dark mode" in result

    def test_read_nonexistent_topic(self, ext_with_store):
        ext, _, _ = ext_with_store
        result = ext._handle_memory_read(topic="nonexistent")
        assert "No memory file" in result

    def test_read_no_store(self):
        ext = MemoryExtension()
        ext._store = None
        result = ext._handle_memory_read(topic="list")
        assert "Error" in result


class TestStructuredQueryAndExtraction:
    def test_memory_query_filters_by_session(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("A", "alpha", "project", session_id="s1", tags=["alpha"])
        ext._handle_memory_save("B", "beta", "project", session_id="s2", tags=["beta"])
        result = ext._handle_memory_query(session_id="s2", limit=5)
        assert "B" in result
        assert "A" not in result

    def test_memory_extract_session_creates_record(self, ext_with_store):
        ext, _, _ = ext_with_store
        result = ext._handle_memory_extract_session(
            session_id="sess_xyz",
            session_text="We decided to keep retry max attempts at 3 for stability.",
            memory_type="project",
            title="Retry policy decision",
            tags=["decision", "retries"],
        )
        assert "saved" in result.lower()
        queried = ext._handle_memory_query(session_id="sess_xyz", tags=["decision"])
        assert "Retry policy decision" in queried

    def test_upsert_supersedes_prior_active_record(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("Deployment Preference", "Use blue/green deploys.", "project")
        ext._handle_memory_save("Deployment Preference", "Use canary deploys.", "project")
        result = ext._handle_memory_query(topic="project", limit=10)
        assert "Use canary deploys." not in result  # table doesn't include content
        # Verify store-level active filtering
        rows = ext._store.query_records(topic="project", limit=10, active_only=True)
        matching = [r for r in rows if r.get("title") == "Deployment Preference"]
        assert len(matching) == 1
        assert matching[0].get("content") == "Use canary deploys."

    def test_dedupe_same_content_does_not_create_new_version(self, ext_with_store):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("Coding Style", "Prefer small pure functions.", "feedback", tags=["style"])
        ext._handle_memory_save("Coding Style", "Prefer small pure functions.", "feedback", tags=["python"])
        rows_all = ext._store.query_records(topic="feedback", limit=20, active_only=False)
        matching = [r for r in rows_all if r.get("title") == "Coding Style"]
        assert len(matching) == 1
        assert set(matching[0].get("tags", [])) >= {"style", "python"}

    def test_memory_search_returns_table(self, ext_with_store):
        ext, _, _ = ext_with_store
        fake_db = MagicMock()
        fake_db.search_messages.return_value = [
            {
                "session_id": "sess_1",
                "source": "cli",
                "role": "user",
                "snippet": ">>>scheduler<<< fairness tuning",
                "timestamp": 123.0,
            }
        ]
        with patch("tau.core.state.SessionDB", return_value=fake_db):
            out = ext._handle_memory_search("scheduler fairness")
        assert "session_id" in out
        assert "sess_1" in out
        assert "scheduler" in out

    def test_memory_search_session_filter(self, ext_with_store):
        ext, _, _ = ext_with_store
        fake_db = MagicMock()
        fake_db.search_messages.return_value = [
            {"session_id": "sess_a", "source": "cli", "role": "user", "snippet": "A", "timestamp": 1},
            {"session_id": "sess_b", "source": "cli", "role": "assistant", "snippet": "B", "timestamp": 2},
        ]
        with patch("tau.core.state.SessionDB", return_value=fake_db):
            out = ext._handle_memory_search("x", session_id="sess_b")
        assert "sess_b" in out
        assert "sess_a" not in out

    def test_memory_search_no_match(self, ext_with_store):
        ext, _, _ = ext_with_store
        fake_db = MagicMock()
        fake_db.search_messages.return_value = []
        with patch("tau.core.state.SessionDB", return_value=fake_db):
            out = ext._handle_memory_search("nope")
        assert "No session messages matched." in out


# ---------------------------------------------------------------------------
# System prompt injection
# ---------------------------------------------------------------------------

class TestPromptInjection:
    def test_build_memory_prompt_with_content(self):
        prompt = _build_memory_prompt("- [Pref](user.md) — likes dark mode", "/mem")
        assert "Persistent Memory" in prompt
        assert "dark mode" in prompt
        assert "/mem" in prompt

    def test_build_memory_prompt_empty(self):
        prompt = _build_memory_prompt("", "/mem")
        assert "No memories saved yet" in prompt

    def test_prompt_includes_type_descriptions(self):
        prompt = _build_memory_prompt("", "/mem")
        assert "user" in prompt
        assert "feedback" in prompt
        assert "project" in prompt
        assert "reference" in prompt

    def test_prompt_includes_exclusions(self):
        prompt = _build_memory_prompt("", "/mem")
        assert "NOT to save" in prompt or "What NOT" in prompt

    def test_prompt_includes_strict_discipline(self):
        prompt = _build_memory_prompt("", "/mem")
        assert "Strict Write Discipline" in prompt
        assert "hallucinated" in prompt


# ---------------------------------------------------------------------------
# /memory status display
# ---------------------------------------------------------------------------

class TestMemoryStatus:
    @patch("threading.Thread")
    def test_dream_trigger(self, mock_thread, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        ext._store = MagicMock()
        ext._store.get_dream_prompt.return_value = "merge stuff"
        mock_sub = MagicMock()
        mock_sub.__enter__ = MagicMock(return_value=mock_sub)
        mock_sub.__exit__ = MagicMock(return_value=False)
        from tau.core.types import TextDelta
        mock_sub.prompt_sync.return_value = [TextDelta(text="done dreaming")]
        ext._ext_context.create_sub_session.return_value = mock_sub

        ext.handle_slash("dream", "", ctx_mock)
        ext._ext_context.create_sub_session.assert_called_once()
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()
        
        args, kwargs = ctx_mock.print.call_args_list[0]
        assert "background" in args[0].lower()

    def test_status_no_memories(self, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        ext._show_memory_status(ctx_mock)
        output = ctx_mock.print.call_args[0][0]
        assert "No memories" in output

    def test_status_with_memories(self, ext_with_store, ctx_mock):
        ext, _, _ = ext_with_store
        ext._handle_memory_save("Test", "Data", "user")
        ext._show_memory_status(ctx_mock)
        output = ctx_mock.print.call_args[0][0]
        assert "Memory Status" in output
        assert "user.md" in output
        assert "global" in output.lower()


class _DummyContextManager:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self):
        return list(self._messages)


class TestAutoMemoryPhase1:
    def test_auto_memory_triggers_on_turn_complete(self, ext_with_store, tmp_path):
        ext, ctx, _ = ext_with_store
        ext._auto_enabled = True
        ext._auto_min_turns = 1
        ext._auto_min_tool_results = 0
        ext._auto_cooldown_seconds = 0
        ext._auto_max_updates = 5

        ctx._context = _DummyContextManager([
            Message(role="user", content="Please implement task event stream API."),
            Message(role="assistant", content="I will add structured events and tools."),
            Message(role="user", content="Also add tests and validate."),
            Message(role="assistant", content="Done with tests and validation."),
        ])

        ext.event_hook(TurnComplete(usage=TokenUsage()))

        session_file = tmp_path / ".tau" / "memory" / "session.md"
        assert session_file.is_file()
        content = session_file.read_text(encoding="utf-8")
        assert "Auto session snapshot" in content

    def test_auto_memory_disabled(self, ext_with_store, tmp_path):
        ext, ctx, _ = ext_with_store
        ext._auto_enabled = False
        ext._auto_min_turns = 1
        ext._auto_cooldown_seconds = 0

        ctx._context = _DummyContextManager([
            Message(role="user", content="A" * 80),
            Message(role="assistant", content="B" * 80),
            Message(role="user", content="C" * 80),
            Message(role="assistant", content="D" * 80),
        ])

        ext.event_hook(TurnComplete(usage=TokenUsage()))
        assert not (tmp_path / ".tau" / "memory" / "session.md").exists()

    def test_auto_memory_respects_cooldown(self, ext_with_store, tmp_path):
        ext, ctx, _ = ext_with_store
        ext._auto_enabled = True
        ext._auto_min_turns = 1
        ext._auto_cooldown_seconds = 999999
        ext._auto_max_updates = 5

        ctx._context = _DummyContextManager([
            Message(role="user", content="Need memory snapshot one."),
            Message(role="assistant", content="Snapshot one done."),
            Message(role="user", content="Need memory snapshot two."),
            Message(role="assistant", content="Snapshot two done."),
        ])

        ext.event_hook(TurnComplete(usage=TokenUsage()))
        ext.event_hook(ToolResultEvent(result=ToolResult(tool_call_id="x", content="ok")))
        ext.event_hook(TurnComplete(usage=TokenUsage()))

        session_file = tmp_path / ".tau" / "memory" / "session.md"
        assert session_file.is_file()
        content = session_file.read_text(encoding="utf-8")
        # With huge cooldown, only one auto-update block should be appended.
        assert content.count("Auto session snapshot") == 1


class TestTopKRetrieval:
    def test_before_turn_injects_retrieval_when_topk_enabled(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 2

        ext.on_load(ctx)
        ext._handle_memory_save("Prefs", "User prefers concise output and practical examples.", "user")
        ext._handle_memory_save("Project", "Working on scheduler fairness and event streams.", "project")

        ext.before_turn("Please keep output concise for scheduler fairness task")

        sys_msg = ctx._context._messages[0].content
        assert "TAU_MEMORY_RETRIEVAL_START" in sys_msg
        assert "Relevant memory for this turn" in sys_msg

    def test_before_turn_skips_retrieval_when_topk_disabled(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 0

        ext.on_load(ctx)
        ext._handle_memory_save("Prefs", "User prefers concise output and practical examples.", "user")

        ext.before_turn("please be concise")
        sys_msg = ctx._context._messages[0].content
        assert "TAU_MEMORY_RETRIEVAL_START" not in sys_msg

    def test_retrieval_prefers_title_and_overlap_quality(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 1

        ext.on_load(ctx)
        ext._handle_memory_save("Scheduler Fairness", "Tune scheduler fairness window.", "project")
        ext._handle_memory_save("Random", "Completely unrelated note content.", "project")

        block = ext._build_retrieval_block("scheduler fairness", topk=1)
        assert "Scheduler Fairness" in block
        assert "Random" not in block

    def test_retrieval_includes_explainability_reason(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 1

        ext.on_load(ctx)
        ext._handle_memory_save(
            "Comms Style",
            "User prefers concise updates and practical examples.",
            "user",
            confidence=0.9,
            source="user-explicit",
            explicitness="explicit",
        )

        block = ext._build_retrieval_block("concise practical examples", topk=1)
        assert "why: overlap=" in block
        assert "eff_conf=" in block
        assert "conflict_penalty=" in block
        assert "score=" in block

    def test_retrieval_cache_uses_cached_block_and_invalidates_on_file_change(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 2

        ext.on_load(ctx)
        ext._handle_memory_save("Prefs", "User likes concise updates.", "user")

        block1 = ext._build_retrieval_block("concise updates", topk=1)
        assert "Prefs" in block1

        # Same query + unchanged files should hit block cache (no recollect needed).
        with patch.object(ext, "_collect_memory_entries", side_effect=AssertionError("should not recollect")):
            block2 = ext._build_retrieval_block("concise updates", topk=1)
            assert block2 == block1

        # File change should invalidate cache and recollect fresh entries.
        ext._handle_memory_save("New Prefs", "User now wants detailed design notes.", "user")
        block3 = ext._build_retrieval_block("detailed design notes", topk=1)
        assert "New Prefs" in block3

    def test_hybrid_retrieval_combines_memory_and_session_hits(self, tmp_path):
        ext = MemoryExtension()

        class _Inner:
            def __init__(self):
                self._messages = [Message(role="system", content="base")]

        ctx = MagicMock()
        ctx.print = MagicMock()
        ctx.enqueue = MagicMock()
        ctx.inject_prompt_fragment = MagicMock()
        ctx._context = _Inner()
        ctx._agent_config = MagicMock()
        ctx._agent_config.workspace_root = str(tmp_path)
        ctx._agent_config.memory_topk = 3

        ext.on_load(ctx)
        ext._handle_memory_save("Scheduler Plan", "Keep scheduler fairness stable.", "project")

        with patch.object(
            ext,
            "_collect_session_hits",
            return_value=[
                {
                    "session_id": "sess_abc",
                    "source": "cli",
                    "role": "user",
                    "snippet": "please improve scheduler fairness",
                    "timestamp": 1,
                }
            ],
        ):
            block = ext._build_retrieval_block("scheduler fairness", topk=3)

        assert "[memory:" in block
        assert "[session:" in block
        assert "sess_abc" in block
