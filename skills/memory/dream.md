---
description: Memory consolidation prompt — reviews and cleans up memory files
---
# Dream: Memory Consolidation

You are performing a **dream** — a reflective pass over your memory files. Synthesize what you've learned recently into durable, well-organized memories so that future sessions can orient quickly.

Memory directory: `{{MEMORY_DIR}}`

---

## Phase 1 — Orient

- List the memory directory to see what already exists
- Read `MEMORY.md` to understand the current index
- Skim existing topic files so you improve them rather than creating duplicates

## Phase 2 — Gather recent signal

Look for information worth persisting or updating:

1. **Existing memories that drifted** — facts that contradict what you see now
2. **Duplicate entries** — same fact recorded multiple times across files
3. **Stale entries** — things with relative dates ("yesterday", "last week") that should be absolute or removed

## Phase 3 — Consolidate

For each thing worth keeping, update the appropriate topic file:

- **Merge** new signal into existing topics rather than creating near-duplicates
- **Convert** relative dates to absolute dates (e.g., "yesterday" → "2026-03-05")
- **Delete** contradicted facts — if new information disproves an old memory, fix it
- **Structure** feedback/project entries as: rule/fact, then **Why:** and **How to apply:**

### Memory Types

| Type | What to store |
|------|--------------|
| `user` | User's role, goals, preferences, knowledge level |
| `feedback` | User corrections AND confirmations (both matter) |
| `project` | Ongoing work, goals, deadlines, decisions not in code |
| `reference` | Pointers to external systems (URLs, dashboards, channels) |

### What NOT to save
- Code patterns or architecture (derivable from the codebase)
- Git history (`git log` is authoritative)
- Debugging solutions (the fix is in the code)
- Ephemeral task details

## Phase 4 — Prune and index

Update `MEMORY.md` so it stays under 200 lines and under ~25KB:

- Each entry should be one line under ~150 chars: `- [Title](file.md) — one-line hook`
- Never write memory content directly into the index
- Remove pointers to stale or superseded memories
- Add pointers to newly important memories
- Shorten any line over ~200 chars — move detail to the topic file

---

Return a brief summary of what you consolidated, updated, or pruned. If nothing changed, say so.
