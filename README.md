# tau-memory

Persistent cross-session memory for [tau](https://github.com/datctbk/tau).

Saves and recalls learnings across sessions via a file-based MEMORY.md index and topic files — inspired by Claude Code's memdir/autoDream architecture.

## Install

```bash
tau install git:github.com/datctbk/tau-memory
```

## How It Works

1. **On session start**: Reads `MEMORY.md` and injects it into the system prompt
2. **During conversation**: LLM uses `memory_save` to persist important learnings
3. **On demand**: `/dream` consolidates and cleans up accumulated memories

### Memory Layout

```
<workspace>/.tau/memory/
├── MEMORY.md          ← Index (injected into system prompt)
├── user.md            ← User preferences, role, knowledge
├── feedback.md        ← User corrections and confirmations
├── project.md         ← Project context, deadlines, decisions
└── reference.md       ← External system pointers
```

## Tools

| Tool | Description |
|------|-------------|
| `memory_save` | Persist a memory with title, content, and type |
| `memory_read` | Read memories by topic, or list all topics |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/memory` | Show memory status and statistics |
| `/dream` | Trigger memory consolidation |

## Memory Types

| Type | What to save |
|------|-------------|
| `user` | Role, goals, preferences, knowledge level |
| `feedback` | Corrections AND confirmations about how to work |
| `project` | Ongoing work, goals, deadlines, decisions not in code |
| `reference` | Pointers to external systems (URLs, dashboards, etc.) |

## Testing

```bash
cd tau-memory && python -m pytest tests/ -v
```
