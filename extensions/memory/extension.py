"""tau-memory: Persistent memory extension for tau.

Gives tau cross-session memory via a file-based MEMORY.md index and topic files.
Inspired by Claude Code's memdir/autoDream architecture, adapted for tau's
lightweight extension system.

Architecture:
  .tau/memory/              ← memory root (per-workspace)
  ├── MEMORY.md             ← index file (injected into system prompt)
  ├── user.md               ← topic file: user preferences
  ├── feedback.md           ← topic file: user corrections & confirmations
  ├── project.md            ← topic file: project context
  └── reference.md          ← topic file: external resource pointers

How it works:
  1. on_load:  Reads MEMORY.md and injects it into the system prompt
  2. tools:   Provides memory_save / memory_read for the LLM to use
  3. /dream:  Slash command to trigger manual consolidation
  4. /memory: Slash command to show current memory status
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tau.core.extension import Extension, ExtensionContext
from tau.core.types import (
    ErrorEvent,
    ExtensionManifest,
    SlashCommand,
    ToolDefinition,
    ToolParameter,
    ToolResultEvent,
    TurnComplete,
)

if TYPE_CHECKING:
    from tau.core.types import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
MEMORY_TYPES = ("user", "feedback", "project", "reference")
GLOBAL_MEMORY_TYPES = {"user", "feedback"}
STRUCTURED_LOG_NAME = "memory_records.jsonl"


# ---------------------------------------------------------------------------
# Memory store — file-based operations
# ---------------------------------------------------------------------------

class MemoryStore:
    """File-based memory store anchored at a workspace root.

    Layout:
        <workspace>/.tau/memory/MEMORY.md
        <workspace>/.tau/memory/<topic>.md
    """

    def __init__(self, workspace: str, global_root: str | None = None) -> None:
        ws_root = Path(workspace).resolve()
        self._root = ws_root / ".tau" / "memory"
        if global_root is None:
            global_root = os.getenv("TAU_MEMORY_GLOBAL_DIR", "~/.tau/memory")
        candidate = Path(global_root).expanduser()
        # Sandbox-safe fallback: if the default global dir is not writable,
        # keep global memory in workspace-local storage.
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".tau_memory_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            self._global_root = candidate
        except Exception:  # noqa: BLE001
            self._global_root = ws_root / ".tau" / "memory-global"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def global_root(self) -> Path:
        return self._global_root

    @property
    def entrypoint(self) -> Path:
        return self._root / ENTRYPOINT_NAME

    @property
    def global_entrypoint(self) -> Path:
        return self._global_root / ENTRYPOINT_NAME

    def _scope_root(self, scope: str) -> Path:
        return self._global_root if scope == "global" else self._root

    def _scope_for_memory_type(self, memory_type: str) -> str:
        return "global" if memory_type in GLOBAL_MEMORY_TYPES else "local"

    def _scope_for_topic(self, topic: str) -> str:
        return "global" if topic in GLOBAL_MEMORY_TYPES else "local"

    def ensure_dir(self) -> None:
        """Create memory directory if it doesn't exist."""
        self._root.mkdir(parents=True, exist_ok=True)
        self._global_root.mkdir(parents=True, exist_ok=True)

    def structured_log_path(self, scope: str = "local") -> Path:
        return self._scope_root(scope) / STRUCTURED_LOG_NAME

    def exists(self, scope: str = "local") -> bool:
        path = self.global_entrypoint if scope == "global" else self.entrypoint
        return path.is_file()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_entrypoint(self, scope: str = "local") -> str:
        """Read MEMORY.md, truncating if too large."""
        path = self.global_entrypoint if scope == "global" else self.entrypoint
        if not path.is_file():
            return ""
        raw = path.read_text(encoding="utf-8")
        return self._truncate(raw)

    def read_topic(self, name: str, scope: str | None = None) -> str:
        """Read a specific topic file."""
        resolved_scope = scope or self._scope_for_topic(name)
        topic_file = self._scope_root(resolved_scope) / f"{name}.md"
        if not topic_file.is_file():
            return ""
        return topic_file.read_text(encoding="utf-8")

    def list_topics(self) -> list[dict[str, Any]]:
        """List all memory files with metadata."""
        entries = []
        for scope, root in (("local", self._root), ("global", self._global_root)):
            if not root.is_dir():
                continue
            for f in sorted(root.glob("*.md")):
                stat = f.stat()
                entries.append({
                    "scope": scope,
                    "name": f.stem,
                    "file": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "lines": f.read_text(encoding="utf-8").count("\n") + 1,
                })
        return entries

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_memory(
        self,
        title: str,
        content: str,
        memory_type: str = "project",
        topic: str | None = None,
        scope: str | None = None,
        confidence: float | None = None,
        source: str | None = None,
        explicitness: str = "explicit",
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Save a memory entry to the appropriate topic file and update the index.

        Returns the path of the written topic file.
        """
        self.ensure_dir()

        # Determine topic file
        if topic is None:
            topic = memory_type if memory_type in MEMORY_TYPES else "project"
        resolved_scope = scope or self._scope_for_memory_type(memory_type)
        topic_file = self._scope_root(resolved_scope) / f"{topic}.md"

        # Normalize optional metadata
        confidence_str = ""
        if confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
                confidence_str = f" | confidence: {confidence:.2f}"
            except Exception:  # noqa: BLE001
                confidence_str = ""

        source_str = f" | source: {source.strip()}" if source and source.strip() else ""
        exp = explicitness.strip().lower() if explicitness else "explicit"
        if exp not in {"explicit", "inferred"}:
            exp = "explicit"
        explicitness_str = f" | explicitness: {exp}"

        # Detect same-title conflict in target topic file.
        conflict = False
        previous_count = 0
        if topic_file.is_file():
            existing = topic_file.read_text(encoding="utf-8")
            previous_count = len(re.findall(rf"^##\s+{re.escape(title)}\s*$", existing, flags=re.M))
            conflict = previous_count > 0

        # Build entry
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conflict_str = f" | conflict: supersedes-{previous_count}" if conflict else ""
        entry = (
            f"\n\n## {title}\n"
            f"*type: {memory_type} | saved: {now}{explicitness_str}{confidence_str}{source_str}{conflict_str}*\n\n"
            f"{content.strip()}\n"
        )

        # Append to topic file
        if topic_file.is_file():
            existing = topic_file.read_text(encoding="utf-8")
            topic_file.write_text(existing + entry, encoding="utf-8")
        else:
            header = f"# {topic.title()} Memories\n"
            topic_file.write_text(header + entry, encoding="utf-8")

        # Update index
        self._update_index(title, f"{topic}.md", memory_type, resolved_scope)
        self._append_structured_record(
            title=title,
            content=content,
            memory_type=memory_type,
            topic=topic,
            scope=resolved_scope,
            confidence=confidence,
            source=source,
            explicitness=exp,
            session_id=session_id,
            tags=tags or [],
        )

        return str(topic_file)

    def _append_structured_record(
        self,
        *,
        title: str,
        content: str,
        memory_type: str,
        topic: str,
        scope: str,
        confidence: float | None,
        source: str | None,
        explicitness: str,
        session_id: str | None,
        tags: list[str],
    ) -> None:
        self.ensure_dir()
        log_path = self.structured_log_path(scope)
        now = datetime.now(timezone.utc).isoformat()
        clean_title = title.strip()
        clean_content = content.strip()
        key = self._record_key(memory_type=memory_type, topic=topic, title=clean_title)
        digest = hashlib.sha256(f"{clean_title}\n{clean_content}".encode("utf-8")).hexdigest()[:16]
        rows = self._read_structured_rows(scope)

        # Upsert semantics:
        # - same key + same content => update metadata in-place (dedupe)
        # - same key + different content => supersede previous active record
        prev_active_idx = -1
        prev_active_row: dict[str, Any] | None = None
        for i in range(len(rows) - 1, -1, -1):
            row = rows[i]
            if row.get("key") == key and bool(row.get("active", True)):
                prev_active_idx = i
                prev_active_row = row
                break

        if prev_active_row and (prev_active_row.get("content", "").strip() == clean_content):
            merged_tags = sorted(set(prev_active_row.get("tags", [])) | {t.strip().lower() for t in tags if t and t.strip()})
            prev_active_row["updated_at"] = now
            prev_active_row["tags"] = merged_tags
            if source and source.strip():
                prev_active_row["source"] = source.strip()
            if session_id:
                prev_active_row["session_id"] = session_id
            if confidence is not None:
                try:
                    prev_active_row["confidence"] = max(
                        float(prev_active_row.get("confidence", 0.0) or 0.0),
                        max(0.0, min(1.0, float(confidence))),
                    )
                except Exception:  # noqa: BLE001
                    pass
            rows[prev_active_idx] = prev_active_row
            self._write_structured_rows(scope, rows)
            return

        record_id = f"mem_{digest}_{int(time.time() * 1000)}"
        version = 1
        if prev_active_row is not None:
            prev_active_row["active"] = False
            prev_active_row["superseded_by"] = record_id
            prev_active_row["updated_at"] = now
            rows[prev_active_idx] = prev_active_row
            version = int(prev_active_row.get("version", 1) or 1) + 1

        record = {
            "id": record_id,
            "key": key,
            "version": version,
            "active": True,
            "title": clean_title,
            "content": clean_content,
            "memory_type": memory_type,
            "topic": topic,
            "scope": scope,
            "confidence": confidence,
            "source": (source or "").strip(),
            "explicitness": explicitness,
            "session_id": session_id or "",
            "tags": [t.strip().lower() for t in tags if t and t.strip()],
            "created_at": now,
            "updated_at": now,
        }
        rows.append(record)
        self._write_structured_rows(scope, rows)

    @staticmethod
    def _record_key(*, memory_type: str, topic: str, title: str) -> str:
        norm_title = re.sub(r"\s+", " ", title.strip().lower())
        return f"{memory_type}:{topic}:{norm_title}"

    def _read_structured_rows(self, scope: str) -> list[dict[str, Any]]:
        p = self.structured_log_path(scope)
        if not p.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _write_structured_rows(self, scope: str, rows: list[dict[str, Any]]) -> None:
        self.ensure_dir()
        p = self.structured_log_path(scope)
        with p.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def query_records(
        self,
        *,
        scope: str | None = None,
        memory_type: str | None = None,
        topic: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        scopes = [scope] if scope in {"local", "global"} else ["local", "global"]
        rows: list[dict[str, Any]] = []
        wanted_tags = {t.strip().lower() for t in (tags or []) if t and t.strip()}
        for sc in scopes:
            for row in self._read_structured_rows(sc):
                if active_only and not bool(row.get("active", True)):
                    continue
                if memory_type and row.get("memory_type") != memory_type:
                    continue
                if topic and row.get("topic") != topic:
                    continue
                if session_id and row.get("session_id") != session_id:
                    continue
                if wanted_tags and not wanted_tags.issubset(set(row.get("tags", []))):
                    continue
                rows.append(row)
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[: max(1, int(limit))]

    def _update_index(self, title: str, filename: str, memory_type: str, scope: str) -> None:
        """Add or update an entry in MEMORY.md."""
        index_line = f"- [{title}]({filename}) — {memory_type}\n"
        entrypoint = self.global_entrypoint if scope == "global" else self.entrypoint

        if entrypoint.is_file():
            existing = entrypoint.read_text(encoding="utf-8")
            # Check for duplicate
            if title in existing:
                return  # already indexed
            entrypoint.write_text(existing + index_line, encoding="utf-8")
        else:
            header = (
                f"# Memory Index\n\n"
                f"*Auto-maintained by tau-memory. Do not edit manually.*\n"
                f"*Each entry is one line — detail lives in topic files.*\n\n"
            )
            entrypoint.write_text(header + index_line, encoding="utf-8")

    # ------------------------------------------------------------------
    # Truncation
    # ------------------------------------------------------------------

    def _truncate(self, raw: str) -> str:
        """Truncate to MAX_ENTRYPOINT_LINES / MAX_ENTRYPOINT_BYTES."""
        trimmed = raw.strip()
        lines = trimmed.split("\n")
        byte_count = len(trimmed)
        was_truncated = False

        if len(lines) > MAX_ENTRYPOINT_LINES:
            lines = lines[:MAX_ENTRYPOINT_LINES]
            was_truncated = True
            trimmed = "\n".join(lines)

        if len(trimmed) > MAX_ENTRYPOINT_BYTES:
            cut_at = trimmed.rfind("\n", 0, MAX_ENTRYPOINT_BYTES)
            trimmed = trimmed[: cut_at if cut_at > 0 else MAX_ENTRYPOINT_BYTES]
            was_truncated = True

        if was_truncated:
            trimmed += (
                f"\n\n> WARNING: {ENTRYPOINT_NAME} was truncated "
                f"({len(lines)} lines, {byte_count} bytes). "
                f"Keep entries to one line under ~150 chars; move detail into topic files."
            )
        return trimmed

    # ------------------------------------------------------------------
    # Dream (consolidation)
    # ------------------------------------------------------------------

    def get_dream_prompt(self) -> str:
        """Load the consolidation prompt from the companion skill file."""
        prompt_file = (
            Path(__file__).resolve().parent.parent.parent
            / "skills" / "memory" / "dream.md"
        )
        if prompt_file.is_file():
            text = prompt_file.read_text(encoding="utf-8")
            # Strip frontmatter
            body_lines: list[str] = []
            in_fm = False
            past_fm = False
            for line in text.splitlines():
                if line.strip() == "---" and not past_fm:
                    if in_fm:
                        past_fm = True
                    in_fm = not in_fm
                    continue
                if not in_fm:
                    body_lines.append(line)
            template = "\n".join(body_lines).strip()
        else:
            template = self._default_dream_prompt()

        return template.replace("{{MEMORY_DIR}}", str(self._root))

    def _default_dream_prompt(self) -> str:
        return (
            "# Dream: Memory Consolidation\n\n"
            "Review memory files in `{{MEMORY_DIR}}` and consolidate:\n"
            "1. Read MEMORY.md and all topic files\n"
            "2. Merge duplicates, remove stale entries\n"
            "3. Update the index\n"
            "4. Report what changed"
        )


# ---------------------------------------------------------------------------
# System prompt fragment — injected into every session
# ---------------------------------------------------------------------------

def _build_memory_prompt(memory_content: str, memory_root: str) -> str:
    """Build the system prompt fragment that teaches the agent about memory."""
    return f"""
## Persistent Memory

You have a persistent memory system stored at `{memory_root}`.
When you learn important information about the user, their preferences,
project context, or external references — save it using the `memory_save` tool.

### Memory Types
- **user**: User's role, goals, preferences, knowledge level
- **feedback**: User's corrections and confirmations about how to work
- **project**: Ongoing work, goals, deadlines, decisions (NOT derivable from code)
- **reference**: Pointers to external systems (Linear, Grafana, Slack, etc.)

### What NOT to save
- Code patterns or architecture (derivable by reading the code)
- Git history (use `git log`)
- Debugging solutions (the fix is in the code)
- Ephemeral task details

### Strict Write Discipline
**NEVER** save memory about an intended code change or shell command until AFTER you have received the tool result confirming that the action was completely successful. This prevents hallucinated states where you misremember broken logic as fixed.

### Structured Query First
When recalling specific prior decisions/preferences, prefer metadata-aware lookup via `memory_query` (e.g. by `session_id`, `memory_type`, or `tags`) before broad topic reads.

### Current Memory Index
{memory_content if memory_content else "(No memories saved yet.)"}
"""


def _build_scoped_memory_prompt(
    local_index: str,
    local_root: str,
    global_index: str,
    global_root: str,
) -> str:
    combined = (
        "### Workspace Memory Index\n"
        + (local_index if local_index else "(No workspace memories saved yet.)")
        + "\n\n### Global Memory Index\n"
        + (global_index if global_index else "(No global memories saved yet.)")
    )
    return _build_memory_prompt(combined, f"local={local_root}, global={global_root}")


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

class MemoryExtension(Extension):
    manifest = ExtensionManifest(
        name="memory",
        version="0.1.0",
        description="Persistent cross-session memory via MEMORY.md index and topic files.",
        author="datctbk",
    )

    def __init__(self) -> None:
        self._ext_context: ExtensionContext | None = None
        self._store: MemoryStore | None = None
        def _env_int(name: str, default: int, min_value: int = 0) -> int:
            raw = os.getenv(name, str(default)).strip()
            try:
                val = int(raw)
            except ValueError:
                return default
            return max(min_value, val)

        self._auto_enabled = os.getenv("TAU_MEMORY_AUTO", "1").strip().lower() not in {"0", "false", "no", "off"}
        self._auto_min_turns = _env_int("TAU_MEMORY_AUTO_MIN_TURNS", 6, 1)
        self._auto_min_tool_results = _env_int("TAU_MEMORY_AUTO_MIN_TOOL_RESULTS", 3, 0)
        self._auto_cooldown_seconds = _env_int("TAU_MEMORY_AUTO_COOLDOWN_SECONDS", 60, 0)
        self._auto_max_updates = _env_int("TAU_MEMORY_AUTO_MAX_UPDATES", 5, 1)
        self._auto_recent_messages = _env_int("TAU_MEMORY_AUTO_RECENT_MESSAGES", 12, 4)
        self._turns_since_auto = 0
        self._tool_results_since_auto = 0
        self._auto_updates_done = 0
        self._last_auto_ts = 0.0
        self._last_auto_digest: str | None = None
        self._topk = 0
        self._retrieval_token_budget = 420
        self._entries_cache_key: tuple[tuple[str, int, int], ...] = ()
        self._entries_cache_rows: list[dict[str, Any]] = []
        self._block_cache: dict[tuple[str, int, tuple[tuple[str, int, int], ...]], str] = {}

    _RETRIEVAL_START = "<!-- TAU_MEMORY_RETRIEVAL_START -->"
    _RETRIEVAL_END = "<!-- TAU_MEMORY_RETRIEVAL_END -->"

    @staticmethod
    def _inject_fragment(context: ExtensionContext, fragment: str) -> None:
        """Inject prompt fragment across ExtensionContext API variants."""
        if hasattr(context, "inject_prompt_fragment"):
            context.inject_prompt_fragment(fragment)  # type: ignore[attr-defined]
            return
        inner = getattr(context, "_context", None)
        if inner is not None and hasattr(inner, "inject_prompt_fragment"):
            inner.inject_prompt_fragment(fragment)
            return
        raise AttributeError("No inject_prompt_fragment API available on ExtensionContext")

    def on_load(self, context: ExtensionContext) -> None:
        self._ext_context = context

        # Determine workspace root
        workspace = "."
        if hasattr(context, "_agent_config") and context._agent_config:
            workspace = getattr(context._agent_config, "workspace_root", ".") or "."

        self._store = MemoryStore(workspace)
        self._entries_cache_key = ()
        self._entries_cache_rows = []
        self._block_cache = {}
        agent_cfg = getattr(context, "_agent_config", None)
        self._topk = max(0, int(getattr(agent_cfg, "memory_topk", 0) or 0))
        self._retrieval_token_budget = max(
            300,
            min(500, int(os.getenv("TAU_MEMORY_RETRIEVAL_MAX_TOKENS", "420"))),
        )

        if self._topk > 0:
            fragment = _build_memory_prompt(
                "Top-k retrieval is enabled for memory. Use retrieved memory block below when present.",
                f"local={self._store.root}, global={self._store.global_root}",
            )
        else:
            local_index = self._store.read_entrypoint(scope="local")
            global_index = self._store.read_entrypoint(scope="global")
            fragment = _build_scoped_memory_prompt(
                local_index=local_index,
                local_root=str(self._store.root),
                global_index=global_index,
                global_root=str(self._store.global_root),
            )
        self._inject_fragment(context, fragment)
        logger.debug("Memory: topk=%d, retrieval_budget=%d", self._topk, self._retrieval_token_budget)

    def before_turn(self, user_input: str) -> None:
        if self._store is None or self._topk <= 0:
            return
        block = self._build_retrieval_block(user_input, topk=self._topk)
        self._upsert_retrieval_fragment(block)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    @staticmethod
    def _tokenize_query(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())}

    @staticmethod
    def _extract_meta_value(chunk: str, key: str) -> str:
        m = re.search(rf"{re.escape(key)}:\s*([^|*]+)", chunk, flags=re.I)
        return (m.group(1).strip() if m else "")

    def _current_snapshot_key(self) -> tuple[tuple[str, int, int], ...]:
        if self._store is None:
            return ()
        snapshot: list[tuple[str, int, int]] = []
        for _, root in (("local", self._store.root), ("global", self._store.global_root)):
            if not root.is_dir():
                continue
            for f in sorted(root.glob("*")):
                if f.suffix not in {".md", ".jsonl"}:
                    continue
                if f.name == ENTRYPOINT_NAME:
                    continue
                try:
                    st = f.stat()
                    snapshot.append((str(f), int(st.st_mtime_ns), int(st.st_size)))
                except Exception:  # noqa: BLE001
                    snapshot.append((str(f), 0, 0))
        return tuple(snapshot)

    def _collect_structured_entries(self) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        rows = self._store.query_records(limit=500, active_only=True)
        entries: list[dict[str, Any]] = []
        for row in rows:
            created = str(row.get("created_at", ""))
            saved_ordinal = 0
            if created:
                try:
                    saved_ordinal = datetime.fromisoformat(created.replace("Z", "+00:00")).date().toordinal()
                except Exception:  # noqa: BLE001
                    saved_ordinal = 0
            entries.append(
                {
                    "scope": row.get("scope", "local"),
                    "title": row.get("title", ""),
                    "body": str(row.get("content", ""))[:500],
                    "saved_ordinal": saved_ordinal,
                    "confidence": float(row.get("confidence", 0.70) or 0.70),
                    "conflict_penalty": 0.0,
                    "explicitness": row.get("explicitness", "explicit"),
                    "source": row.get("source", ""),
                    "tags": row.get("tags", []),
                }
            )
        return entries

    def _collect_memory_entries(self) -> list[dict[str, Any]]:
        """Collect memory entries with retrieval metadata.

        Prefer structured records (memory_query path) and fall back to topic markdown.
        """
        if self._store is None:
            return []
        snapshot_key = self._current_snapshot_key()
        if snapshot_key == self._entries_cache_key and self._entries_cache_rows:
            return list(self._entries_cache_rows)

        entries: list[dict[str, Any]] = []
        structured = self._collect_structured_entries()
        if structured:
            entries.extend(structured)
            self._entries_cache_key = snapshot_key
            self._entries_cache_rows = list(entries)
            self._block_cache = {}
            return entries

        # Fallback path for legacy memory files without structured records.
        for scope, root in (("local", self._store.root), ("global", self._store.global_root)):
            if not root.is_dir():
                continue
            for f in sorted(root.glob("*.md")):
                if f.name == ENTRYPOINT_NAME:
                    continue
                text = f.read_text(encoding="utf-8")
                chunks = re.split(r"\n##\s+", text)
                for i, ch in enumerate(chunks):
                    chunk = ch.strip()
                    if not chunk:
                        continue
                    if i == 0 and chunk.startswith("#"):
                        # file header section
                        continue
                    first_line = chunk.splitlines()[0].strip()
                    title = first_line if first_line else f.stem
                    body = chunk[:500].strip()
                    saved_ordinal = 0
                    m = re.search(r"saved:\s*(\d{4}-\d{2}-\d{2})", chunk)
                    if m:
                        try:
                            saved_ordinal = date.fromisoformat(m.group(1)).toordinal()
                        except Exception:  # noqa: BLE001
                            saved_ordinal = 0
                    confidence = 0.70
                    confidence_raw = self._extract_meta_value(chunk, "confidence")
                    if confidence_raw:
                        try:
                            confidence = max(0.0, min(1.0, float(confidence_raw)))
                        except Exception:  # noqa: BLE001
                            confidence = 0.70
                    conflict_raw = self._extract_meta_value(chunk, "conflict").lower()
                    conflict_penalty = 0.0
                    if conflict_raw.startswith("supersedes-"):
                        try:
                            supersedes_n = int(conflict_raw.split("-", 1)[1])
                            conflict_penalty = min(0.35, 0.08 * supersedes_n)
                        except Exception:  # noqa: BLE001
                            conflict_penalty = 0.10
                    explicitness = self._extract_meta_value(chunk, "explicitness").lower() or "explicit"
                    source = self._extract_meta_value(chunk, "source")
                    entries.append(
                        {
                            "scope": scope,
                            "title": title,
                            "body": body,
                            "saved_ordinal": saved_ordinal,
                            "confidence": confidence,
                            "conflict_penalty": conflict_penalty,
                            "explicitness": explicitness,
                            "source": source,
                        }
                    )
        self._entries_cache_key = snapshot_key
        self._entries_cache_rows = list(entries)
        # Retrieval blocks depend on parsed rows, so invalidate block cache on source change.
        self._block_cache = {}
        return entries

    def _score_retrieval_entry(
        self,
        *,
        query_tokens: set[str],
        scope: str,
        title: str,
        body: str,
        saved_ordinal: int,
        confidence: float,
        conflict_penalty: float,
        explicitness: str,
    ) -> tuple[float, dict[str, Any]]:
        hay = f"{title}\n{body}".lower()
        entry_tokens = self._tokenize_query(hay)
        overlap = len(query_tokens & entry_tokens)
        if overlap == 0:
            return 0.0, {}

        coverage = overlap / max(1, len(query_tokens))
        title_tokens = self._tokenize_query(title)
        title_hit = len(query_tokens & title_tokens)

        recency_boost = 0.0
        if saved_ordinal > 0:
            age_days = max(0, date.today().toordinal() - saved_ordinal)
            if age_days <= 7:
                recency_boost = 0.30
            elif age_days <= 30:
                recency_boost = 0.15

        scope_boost = 0.10 if scope == "global" else 0.0
        explicit_boost = 0.05 if explicitness == "explicit" else 0.0

        age_days = max(0, date.today().toordinal() - saved_ordinal) if saved_ordinal > 0 else 365
        decay_half_life_days = max(1.0, float(os.getenv("TAU_MEMORY_CONFIDENCE_HALF_LIFE_DAYS", "45")))
        decay = math.exp(-math.log(2.0) * (age_days / decay_half_life_days))
        effective_confidence = max(0.05, min(1.0, confidence * decay))

        raw_score = overlap + coverage + (title_hit * 0.50) + recency_boost + scope_boost + explicit_boost
        final_score = (raw_score * (0.5 + effective_confidence)) - conflict_penalty
        why = {
            "overlap": overlap,
            "coverage": round(coverage, 3),
            "title_hit": title_hit,
            "age_days": age_days,
            "decay": round(decay, 3),
            "confidence": round(confidence, 3),
            "effective_confidence": round(effective_confidence, 3),
            "recency_boost": round(recency_boost, 3),
            "scope_boost": round(scope_boost, 3),
            "explicit_boost": round(explicit_boost, 3),
            "conflict_penalty": round(conflict_penalty, 3),
            "final_score": round(final_score, 3),
        }
        return final_score, why

    def _build_retrieval_block(self, query: str, topk: int) -> str:
        q = self._tokenize_query(query)
        if not q:
            return ""
        current_snapshot: tuple[tuple[str, int, int], ...] = ()
        if self._store is not None:
            current_snapshot = self._current_snapshot_key()
            if current_snapshot != self._entries_cache_key:
                self._entries_cache_key = ()
                self._entries_cache_rows = []
                self._block_cache = {}
        cache_key = (query.strip().lower(), int(topk), current_snapshot)
        cached = self._block_cache.get(cache_key)
        if cached is not None:
            return cached
        min_score = float(os.getenv("TAU_MEMORY_RETRIEVAL_MIN_SCORE", "1.2"))
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for row in self._collect_memory_entries():
            score, why = self._score_retrieval_entry(
                query_tokens=q,
                scope=str(row.get("scope", "")),
                title=str(row.get("title", "")),
                body=str(row.get("body", "")),
                saved_ordinal=int(row.get("saved_ordinal", 0) or 0),
                confidence=float(row.get("confidence", 0.7) or 0.7),
                conflict_penalty=float(row.get("conflict_penalty", 0.0) or 0.0),
                explicitness=str(row.get("explicitness", "explicit")),
            )
            if score < min_score:
                continue
            scope = str(row.get("scope", "local"))
            title = str(row.get("title", ""))
            body = str(row.get("body", ""))
            explain = (
                f"why: overlap={why.get('overlap', 0)}, "
                f"eff_conf={why.get('effective_confidence', 0)}, "
                f"conflict_penalty={why.get('conflict_penalty', 0)}, "
                f"score={why.get('final_score', 0)}"
            )
            scored.append((score, f"- ({scope}) {title}: {body}\n  [{explain}]", why))
        if not scored:
            self._block_cache[cache_key] = ""
            return ""
        scored.sort(key=lambda x: x[0], reverse=True)
        selected: list[str] = []
        budget = self._retrieval_token_budget
        used = self._estimate_tokens("Relevant memory for this turn:\n")
        for _, line, _ in scored[: max(1, topk * 4)]:
            cost = self._estimate_tokens(line)
            if len(selected) >= topk:
                break
            if used + cost > budget:
                continue
            selected.append(line)
            used += cost
        if not selected:
            self._block_cache[cache_key] = ""
            return ""
        block = "Relevant memory for this turn:\n" + "\n".join(selected)
        self._block_cache[cache_key] = block
        return block

    def _upsert_retrieval_fragment(self, block: str) -> None:
        if self._ext_context is None:
            return
        inner = getattr(self._ext_context, "_context", None)
        if inner is None:
            return
        messages = getattr(inner, "_messages", None)
        if not isinstance(messages, list):
            return
        fragment = (
            f"{self._RETRIEVAL_START}\n{block}\n{self._RETRIEVAL_END}"
            if block
            else ""
        )
        pattern = re.compile(
            re.escape(self._RETRIEVAL_START) + r".*?" + re.escape(self._RETRIEVAL_END),
            re.S,
        )
        for m in messages:
            if getattr(m, "role", None) != "system":
                continue
            content = getattr(m, "content", "") or ""
            content = pattern.sub("", content).rstrip()
            if fragment:
                content = f"{content}\n\n{fragment}".strip()
            m.content = content
            break

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="memory_save",
                description=(
                    "Save information to persistent memory. Use this when you learn "
                    "important facts about the user, their preferences, project context, "
                    "or external references. Memories persist across sessions.\n\n"
                    "Types: user, feedback, project, reference\n\n"
                    "Strict Write Discipline: NEVER save memory about an intended code change "
                    "or command until AFTER you have received the tool result confirming it succeeded.\n\n"
                    "Do NOT save: code patterns (derivable), git history, debugging solutions, "
                    "or ephemeral task details."
                ),
                parameters={
                    "title": ToolParameter(
                        type="string",
                        description="Short descriptive title for the memory (e.g. 'User prefers concise responses').",
                    ),
                    "content": ToolParameter(
                        type="string",
                        description=(
                            "The memory content. For feedback/project types, structure as: "
                            "rule/fact, then Why: and How to apply: lines."
                        ),
                    ),
                    "memory_type": ToolParameter(
                        type="string",
                        description="One of: user, feedback, project, reference.",
                    ),
                    "topic": ToolParameter(
                        type="string",
                        description="Optional topic file name (defaults to memory_type). Use for custom groupings.",
                        required=False,
                    ),
                    "confidence": ToolParameter(
                        type="number",
                        description="Optional confidence score in [0,1]. Higher means stronger confidence in this memory.",
                        required=False,
                    ),
                    "source": ToolParameter(
                        type="string",
                        description="Optional source for the memory (e.g., user-explicit, tool-result, inference).",
                        required=False,
                    ),
                    "explicitness": ToolParameter(
                        type="string",
                        description="Whether the fact is explicit or inferred. One of: explicit, inferred.",
                        required=False,
                    ),
                    "session_id": ToolParameter(
                        type="string",
                        description="Optional session ID this memory came from.",
                        required=False,
                    ),
                    "tags": ToolParameter(
                        type="array",
                        description="Optional tag list for structured memory retrieval.",
                        required=False,
                    ),
                },
                handler=self._handle_memory_save,
            ),
            ToolDefinition(
                name="memory_read",
                description=(
                    "Read memories from a specific topic file or list all available memories. "
                    "Use this when the user references prior work, asks you to recall something, "
                    "or when context from past sessions would be helpful."
                ),
                parameters={
                    "topic": ToolParameter(
                        type="string",
                        description=(
                            "Topic to read (e.g. 'user', 'feedback', 'project'). "
                            "Use 'index' to read the MEMORY.md index, or 'list' to see all topics."
                        ),
                    ),
                },
                handler=self._handle_memory_read,
            ),
            ToolDefinition(
                name="memory_query",
                description=(
                    "Query structured memory records by scope/type/topic/session/tags. "
                    "Use this for session-level or metadata-based recall."
                ),
                parameters={
                    "scope": ToolParameter(
                        type="string",
                        description="Optional scope filter: local or global.",
                        required=False,
                    ),
                    "memory_type": ToolParameter(
                        type="string",
                        description="Optional memory type filter.",
                        required=False,
                    ),
                    "topic": ToolParameter(
                        type="string",
                        description="Optional topic filter.",
                        required=False,
                    ),
                    "session_id": ToolParameter(
                        type="string",
                        description="Optional session ID filter.",
                        required=False,
                    ),
                    "tags": ToolParameter(
                        type="array",
                        description="Optional tag filters; all provided tags must match.",
                        required=False,
                    ),
                    "limit": ToolParameter(
                        type="integer",
                        description="Maximum number of records to return (default 10).",
                        required=False,
                    ),
                },
                handler=self._handle_memory_query,
            ),
            ToolDefinition(
                name="memory_extract_session",
                description=(
                    "Extract a structured memory candidate from a session snippet and save it. "
                    "Minimal deterministic extractor for session-level memory capture."
                ),
                parameters={
                    "session_id": ToolParameter(
                        type="string",
                        description="Source session ID for provenance.",
                    ),
                    "session_text": ToolParameter(
                        type="string",
                        description="Session text snippet to extract from.",
                    ),
                    "memory_type": ToolParameter(
                        type="string",
                        description="Target memory type: user, feedback, project, reference.",
                    ),
                    "title": ToolParameter(
                        type="string",
                        description="Short memory title.",
                    ),
                    "tags": ToolParameter(
                        type="array",
                        description="Optional tags for structured query later.",
                        required=False,
                    ),
                },
                handler=self._handle_memory_extract_session,
            ),
        ]

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def slash_commands(self) -> list[SlashCommand]:
        return [
            SlashCommand(
                name="memory",
                description="Show memory status and statistics.",
                usage="/memory",
            ),
            SlashCommand(
                name="dream",
                description="Trigger memory consolidation — review and clean up memories.",
                usage="/dream",
            ),
        ]

    def handle_slash(self, command: str, args: str, context: ExtensionContext) -> bool:
        if command == "memory":
            self._show_memory_status(context)
            return True
        if command == "dream":
            self._trigger_dream(context)
            return True
        return False

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_memory_save(
        self,
        title: str,
        content: str,
        memory_type: str,
        topic: str | None = None,
        confidence: float | None = None,
        source: str | None = None,
        explicitness: str = "explicit",
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        if self._store is None:
            return "Error: Memory store not initialized."

        if memory_type not in MEMORY_TYPES:
            return f"Error: Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}"

        try:
            path = self._store.save_memory(
                title=title,
                content=content,
                memory_type=memory_type,
                topic=topic,
                confidence=confidence,
                source=source,
                explicitness=explicitness,
                session_id=session_id,
                tags=tags,
            )
            return f"Memory saved: '{title}' → {path}"
        except Exception as e:
            return f"Error saving memory: {e}"

    def _handle_memory_query(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        topic: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> str:
        if self._store is None:
            return "Error: Memory store not initialized."
        rows = self._store.query_records(
            scope=scope,
            memory_type=memory_type,
            topic=topic,
            session_id=session_id,
            tags=tags,
            limit=limit,
        )
        if not rows:
            return "No structured memory records matched."
        lines = [
            "| id | scope | type | topic | session | title | tags |",
            "|----|-------|------|-------|---------|-------|------|",
        ]
        for r in rows:
            lines.append(
                f"| {r.get('id','')} | {r.get('scope','')} | {r.get('memory_type','')} | "
                f"{r.get('topic','')} | {r.get('session_id','')} | {str(r.get('title',''))[:60]} | "
                f"{', '.join(r.get('tags', []))} |"
            )
        return "\n".join(lines)

    def _handle_memory_extract_session(
        self,
        session_id: str,
        session_text: str,
        memory_type: str,
        title: str,
        tags: list[str] | None = None,
    ) -> str:
        if self._store is None:
            return "Error: Memory store not initialized."
        if memory_type not in MEMORY_TYPES:
            return f"Error: Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}"
        clean = " ".join((session_text or "").split())
        if not clean:
            return "Error: session_text is empty."
        content = clean[:700]
        return self._handle_memory_save(
            title=title,
            content=content,
            memory_type=memory_type,
            source="session-extract",
            explicitness="inferred",
            session_id=session_id,
            tags=tags or ["session"],
        )

    def _handle_memory_read(self, topic: str) -> str:
        if self._store is None:
            return "Error: Memory store not initialized."

        if topic == "list":
            topics = self._store.list_topics()
            if not topics:
                return "No memory files found."
            lines = ["| Scope | File | Lines | Size | Modified |", "|-------|------|-------|------|----------|"]
            for t in topics:
                lines.append(f"| {t['scope']} | {t['file']} | {t['lines']} | {t['size_bytes']}B | {t['modified'][:10]} |")
            return "\n".join(lines)

        if topic == "index":
            local_index = self._store.read_entrypoint(scope="local")
            global_index = self._store.read_entrypoint(scope="global")
            return (
                "# Workspace Memory Index\n\n"
                + (local_index if local_index else "(No workspace memory index found.)")
                + "\n\n# Global Memory Index\n\n"
                + (global_index if global_index else "(No global memory index found.)")
            )

        content = self._store.read_topic(topic)
        if not content:
            return f"No memory file found for topic '{topic}'."
        return content

    # ------------------------------------------------------------------
    # Slash command display
    # ------------------------------------------------------------------

    def _show_memory_status(self, context: ExtensionContext) -> None:
        if self._store is None:
            context.print("[dim]Memory store not initialized.[/dim]")
            return

        topics = self._store.list_topics()
        if not topics:
            context.print(f"[dim]No memories saved yet. Memory root: {self._store.root}[/dim]")
            return

        lines = [
            "[bold cyan]Memory Status[/bold cyan]",
            f"[dim]Workspace root: {self._store.root}[/dim]",
            f"[dim]Global root: {self._store.global_root}[/dim]",
            "",
        ]
        total_bytes = 0
        total_lines = 0
        for t in topics:
            icon = "📋" if t["name"] == "MEMORY" else "📝"
            lines.append(f"  {icon} [bold]{t['file']}[/bold] ({t['scope']}) — {t['lines']} lines, {t['size_bytes']}B")
            total_bytes += t["size_bytes"]
            total_lines += t["lines"]

        lines.append("")
        lines.append(f"[dim]Total: {len(topics)} files, {total_lines} lines, {total_bytes}B[/dim]")
        context.print("\n".join(lines))

    def _trigger_dream(self, context: ExtensionContext) -> None:
        if self._store is None:
            context.print("[dim]Memory store not initialized.[/dim]")
            return

        prompt = self._store.get_dream_prompt()

        # Try to use create_sub_session if available (tau-agents installed)
        if self._ext_context is not None and hasattr(self._ext_context, "create_sub_session"):
            try:
                context.print("[cyan]Memory dream started in background...[/cyan]")
                sub = self._ext_context.create_sub_session(
                    system_prompt=prompt,
                    max_turns=8,
                    session_name="dream-agent",
                )
                
                # The task to run
                task_content = (
                    f"Consolidate the memory files in {self._store.root}. "
                    f"Read the index and all topic files, merge duplicates, "
                    f"remove stale entries, and update the index."
                )

                def _run_dream():
                    try:
                        with sub:
                            events = sub.prompt_sync(task_content)
                        # Collect result
                        from tau.core.types import TextDelta
                        text = "".join(
                            e.text for e in events
                            if isinstance(e, TextDelta) and not getattr(e, "is_thinking", False)
                        )
                        context.print(f"\n[green]Memory dream fully consolidated.[/green]\n{text[:500]}")
                    except Exception as e:
                        logger.warning("Dream sub-agent execution failed: %s", e)
                        context.print(f"\n[red]Background memory dream failed:[/red] {e}")

                import threading
                t = threading.Thread(target=_run_dream, name="DreamAgentThread")
                t.daemon = True
                t.start()
                return

            except Exception as e:
                logger.warning("Dream via sub-agent failed to start (%s), showing prompt instead", e)

        # Fallback: just show the prompt for the user to paste
        context.print(
            "[bold cyan]Dream Prompt[/bold cyan]\n\n"
            "[dim]Paste the following as your next message to trigger consolidation:[/dim]\n\n"
            + prompt[:2000]
        )

    # ------------------------------------------------------------------
    # Auto memory (phase 1)
    # ------------------------------------------------------------------

    def event_hook(self, event: "Event") -> None:
        if not self._auto_enabled or self._store is None:
            return
        if isinstance(event, ToolResultEvent):
            self._tool_results_since_auto += 1
            return
        if isinstance(event, ErrorEvent):
            return
        if isinstance(event, TurnComplete):
            self._turns_since_auto += 1
            self._maybe_auto_update()

    def _maybe_auto_update(self) -> None:
        if self._auto_updates_done >= self._auto_max_updates:
            return
        if self._turns_since_auto < self._auto_min_turns and self._tool_results_since_auto < self._auto_min_tool_results:
            return
        now = time.time()
        if now - self._last_auto_ts < self._auto_cooldown_seconds:
            return
        candidate = self._build_auto_memory_candidate()
        if candidate is None:
            return
        title, content = candidate
        digest = hashlib.sha1((title + "\n" + content).encode("utf-8")).hexdigest()
        if digest == self._last_auto_digest:
            return
        try:
            self._store.save_memory(
                title=title,
                content=content,
                memory_type="project",
                topic="session",
            )
            self._last_auto_digest = digest
            self._last_auto_ts = now
            self._auto_updates_done += 1
            self._turns_since_auto = 0
            self._tool_results_since_auto = 0
            if self._ext_context is not None:
                self._ext_context.print("[dim]Auto memory updated.[/dim]")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto memory update failed: %s", exc)

    def _build_auto_memory_candidate(self) -> tuple[str, str] | None:
        if self._ext_context is None:
            return None
        ctx = getattr(self._ext_context, "_context", None)
        if ctx is None or not hasattr(ctx, "get_messages"):
            return None
        try:
            messages = ctx.get_messages()
        except Exception:  # noqa: BLE001
            return None

        relevant = [m for m in messages if getattr(m, "role", None) in ("user", "assistant")]
        if len(relevant) < 4:
            return None
        recent = relevant[-self._auto_recent_messages :]

        user_lines: list[str] = []
        assistant_lines: list[str] = []
        for m in recent:
            text = (getattr(m, "content", "") or "").strip().replace("\n", " ")
            if not text:
                continue
            text = " ".join(text.split())
            preview = text[:160] + ("..." if len(text) > 160 else "")
            if m.role == "user":
                user_lines.append(preview)
            elif m.role == "assistant":
                assistant_lines.append(preview)

        if not user_lines and not assistant_lines:
            return None

        # Keep concise summaries to avoid noisy auto-memory spam.
        user_part = "\n".join(f"- {u}" for u in user_lines[-3:])
        assistant_part = "\n".join(f"- {a}" for a in assistant_lines[-2:])
        lines = [
            "Auto session snapshot:",
            "Recent user intents:",
            user_part or "- (none)",
            "Recent assistant outcomes:",
            assistant_part or "- (none)",
        ]
        content = "\n".join(lines).strip()
        if len(content) < 40:
            return None

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        title = f"Session update {ts}"
        return (title, content)


# Module-level instance
EXTENSION = MemoryExtension()
