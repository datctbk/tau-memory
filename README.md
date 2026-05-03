# tau-memory

Persistent cross-session memory extension for [tau](https://github.com/datctbk/tau).

`tau-memory` stores long-term memory in topic files + structured JSONL records, and injects relevant memory into the prompt each turn.

## Install

```bash
tau install git:github.com/datctbk/tau-memory
```

## How It Works

1. **On session start**: Reads scoped memory index and injects memory guidance into system prompt
2. **During conversation**: LLM uses `memory_save` / `memory_query` / `memory_search`
3. **Before each turn (optional top-k)**: injects compact relevant memory block
4. **On demand**: `/dream` consolidates and cleans up accumulated memories

### Memory Layout

```
<workspace>/.tau/memory/
├── MEMORY.md          ← Index (injected into system prompt)
├── user.md            ← User preferences, role, knowledge
├── feedback.md        ← User corrections and confirmations
├── project.md         ← Project context, deadlines, decisions
├── reference.md       ← External system pointers
├── memory_records.jsonl   ← Structured memory records (upsert/dedupe)
└── memory_audit.jsonl     ← Policy/audit decision log
```

## Tools

| Tool | Description |
|------|-------------|
| `memory_save` | Persist memory with policy metadata (`source`, `confidence`, `why_saved`) |
| `memory_read` | Read memories by topic, or list all topics |
| `memory_query` | Query structured memory records by scope/type/topic/session/tags |
| `memory_extract_session` | Save a deterministic session-level memory extract |
| `memory_search` | FTS5-backed session history search via tau core SessionDB |

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

## Policy & Audit

Memory writes support configurable policy checks and audit logging.

Environment flags:

- `TAU_MEMORY_WRITE_POLICY_STRICT` (default `0`)
- `TAU_MEMORY_REQUIRE_SOURCE` (default `1`)
- `TAU_MEMORY_REQUIRE_CONFIDENCE` (default `1`)
- `TAU_MEMORY_REQUIRE_WHY_SAVED` (default `1`)
- `TAU_MEMORY_MIN_CONFIDENCE` (default `0.35`)

Hybrid session recall (off by default for performance):

- `TAU_MEMORY_HYBRID_SESSION` (default `0`)
- `TAU_MEMORY_HYBRID_SESSION_LIMIT` (default `2`)
