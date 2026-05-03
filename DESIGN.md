# tau-memory — Design Document

## 1. Purpose

`tau-memory` adds persistent memory behavior to Tau without expanding Tau core complexity.

It provides:
- scoped long-term memory (`local` + `global`)
- structured memory records with upsert/dedupe
- retrieval injection before turns
- policy + audit for memory writes

## 2. Storage Model

Workspace-local:
- `.tau/memory/MEMORY.md`
- `.tau/memory/<topic>.md`
- `.tau/memory/memory_records.jsonl`
- `.tau/memory/memory_audit.jsonl`

Global:
- `~/.tau/memory/*` (or workspace fallback when home dir is not writable)

## 3. Retrieval Model

Default retrieval is lightweight and deterministic:
- metadata/structured records (`memory_query`)
- lexical scoring over memory entries (confidence, recency, conflict penalty)

Optional hybrid (performance-gated):
- FTS session snippet recall through Tau core `SessionDB.search_messages()`
- disabled by default (`TAU_MEMORY_HYBRID_SESSION=0`)
- strict hit cap + tiny cache to avoid turn latency spikes

## 4. Tool Surface

- `memory_save`
- `memory_read`
- `memory_query`
- `memory_extract_session`
- `memory_search` (FTS5-backed session lookup via Tau core state DB)

Slash commands:
- `/memory`
- `/dream`

## 5. Write Policy & Audit

Policy checks (configurable):
- required `source`
- required `confidence` with minimum threshold
- required `why_saved`

Modes:
- strict deny (`TAU_MEMORY_WRITE_POLICY_STRICT=1`)
- allow-with-warnings (`0`)

All decisions are written to `memory_audit.jsonl` with:
- decision (`allow`, `allow_with_warnings`, `deny`)
- violations
- metadata (title/type/session/source/confidence/tags/why_saved)

## 6. Prompt Injection Flow

1. `on_load`: inject memory guidance/index
2. `before_turn` (when top-k enabled): build compact relevant block
3. upsert block between retrieval markers in system prompt

This keeps memory context explicit and bounded by token budget.

## 7. Non-goals

- Not a vector DB system by default
- Not a global enterprise knowledge graph
- Not a replacement for Tau core session storage

## 8. Evolution Path

Near-term:
- stronger hybrid ranking controls
- configurable cross-source blending
- optional provider abstraction for external memory backends

Long-term:
- pluggable semantic/vector rerank as opt-in backend
