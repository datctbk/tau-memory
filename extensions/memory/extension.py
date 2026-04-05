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
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tau.core.extension import Extension, ExtensionContext
from tau.core.types import (
    ExtensionManifest,
    SlashCommand,
    ToolDefinition,
    ToolParameter,
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


# ---------------------------------------------------------------------------
# Memory store — file-based operations
# ---------------------------------------------------------------------------

class MemoryStore:
    """File-based memory store anchored at a workspace root.

    Layout:
        <workspace>/.tau/memory/MEMORY.md
        <workspace>/.tau/memory/<topic>.md
    """

    def __init__(self, workspace: str) -> None:
        self._root = Path(workspace) / ".tau" / "memory"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def entrypoint(self) -> Path:
        return self._root / ENTRYPOINT_NAME

    def ensure_dir(self) -> None:
        """Create memory directory if it doesn't exist."""
        self._root.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.entrypoint.is_file()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_entrypoint(self) -> str:
        """Read MEMORY.md, truncating if too large."""
        if not self.entrypoint.is_file():
            return ""
        raw = self.entrypoint.read_text(encoding="utf-8")
        return self._truncate(raw)

    def read_topic(self, name: str) -> str:
        """Read a specific topic file."""
        topic_file = self._root / f"{name}.md"
        if not topic_file.is_file():
            return ""
        return topic_file.read_text(encoding="utf-8")

    def list_topics(self) -> list[dict[str, Any]]:
        """List all memory files with metadata."""
        if not self._root.is_dir():
            return []
        entries = []
        for f in sorted(self._root.glob("*.md")):
            stat = f.stat()
            entries.append({
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
    ) -> str:
        """Save a memory entry to the appropriate topic file and update the index.

        Returns the path of the written topic file.
        """
        self.ensure_dir()

        # Determine topic file
        if topic is None:
            topic = memory_type if memory_type in MEMORY_TYPES else "project"
        topic_file = self._root / f"{topic}.md"

        # Build frontmatter entry
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = f"\n\n## {title}\n*type: {memory_type} | saved: {now}*\n\n{content.strip()}\n"

        # Append to topic file
        if topic_file.is_file():
            existing = topic_file.read_text(encoding="utf-8")
            topic_file.write_text(existing + entry, encoding="utf-8")
        else:
            header = f"# {topic.title()} Memories\n"
            topic_file.write_text(header + entry, encoding="utf-8")

        # Update index
        self._update_index(title, f"{topic}.md", memory_type)

        return str(topic_file)

    def _update_index(self, title: str, filename: str, memory_type: str) -> None:
        """Add or update an entry in MEMORY.md."""
        index_line = f"- [{title}]({filename}) — {memory_type}\n"

        if self.entrypoint.is_file():
            existing = self.entrypoint.read_text(encoding="utf-8")
            # Check for duplicate
            if title in existing:
                return  # already indexed
            self.entrypoint.write_text(existing + index_line, encoding="utf-8")
        else:
            header = (
                f"# Memory Index\n\n"
                f"*Auto-maintained by tau-memory. Do not edit manually.*\n"
                f"*Each entry is one line — detail lives in topic files.*\n\n"
            )
            self.entrypoint.write_text(header + index_line, encoding="utf-8")

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

### Current Memory Index
{memory_content if memory_content else "(No memories saved yet.)"}
"""


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

    def on_load(self, context: ExtensionContext) -> None:
        self._ext_context = context

        # Determine workspace root
        workspace = "."
        if hasattr(context, "_agent_config") and context._agent_config:
            workspace = getattr(context._agent_config, "workspace_root", ".") or "."

        self._store = MemoryStore(workspace)

        # Inject memory into system prompt if it exists
        if self._store.exists():
            content = self._store.read_entrypoint()
            fragment = _build_memory_prompt(content, str(self._store.root))
            context.inject_prompt_fragment(fragment)
            logger.debug("Memory: injected %d chars from %s", len(content), self._store.entrypoint)
        else:
            # Inject minimal prompt so agent knows memory is available
            fragment = _build_memory_prompt("", str(self._store.root))
            context.inject_prompt_fragment(fragment)

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
            )
            return f"Memory saved: '{title}' → {path}"
        except Exception as e:
            return f"Error saving memory: {e}"

    def _handle_memory_read(self, topic: str) -> str:
        if self._store is None:
            return "Error: Memory store not initialized."

        if topic == "list":
            topics = self._store.list_topics()
            if not topics:
                return "No memory files found."
            lines = ["| File | Lines | Size | Modified |", "|------|-------|------|----------|"]
            for t in topics:
                lines.append(f"| {t['file']} | {t['lines']} | {t['size_bytes']}B | {t['modified'][:10]} |")
            return "\n".join(lines)

        if topic == "index":
            content = self._store.read_entrypoint()
            return content if content else "(No memory index found.)"

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
            f"[dim]Root: {self._store.root}[/dim]",
            "",
        ]
        total_bytes = 0
        total_lines = 0
        for t in topics:
            icon = "📋" if t["name"] == "MEMORY" else "📝"
            lines.append(f"  {icon} [bold]{t['file']}[/bold] — {t['lines']} lines, {t['size_bytes']}B")
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
                context.print("[cyan]Starting memory dream via sub-agent...[/cyan]")
                sub = self._ext_context.create_sub_session(
                    system_prompt=prompt,
                    max_turns=8,
                    session_name="dream-agent",
                )
                with sub:
                    events = sub.prompt_sync(
                        f"Consolidate the memory files in {self._store.root}. "
                        f"Read the index and all topic files, merge duplicates, "
                        f"remove stale entries, and update the index."
                    )
                # Collect result
                from tau.core.types import TextDelta
                text = "".join(
                    e.text for e in events
                    if isinstance(e, TextDelta) and not getattr(e, "is_thinking", False)
                )
                context.print(f"[green]Dream complete.[/green]\n{text[:500]}")
                return
            except Exception as e:
                logger.warning("Dream via sub-agent failed (%s), showing prompt instead", e)

        # Fallback: just show the prompt for the user to paste
        context.print(
            "[bold cyan]Dream Prompt[/bold cyan]\n\n"
            "[dim]Paste the following as your next message to trigger consolidation:[/dim]\n\n"
            + prompt[:2000]
        )


# Module-level instance
EXTENSION = MemoryExtension()
