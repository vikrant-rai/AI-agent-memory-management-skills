# agent-memory

**Tiered, self-managed long-term memory for local AI agents.**
Zero dependencies. Pure Python 3 + SQLite FTS5. Built for agents running on
local models (Ollama, LM Studio, llama.cpp) where context windows are tight
and conversations need to survive across sessions.

## The problem

Long-running local agents fill their context window fast — verbatim history
piles up, quality degrades past ~70% usage, and everything is lost when the
session ends.

## The fix

A write-behind memory cache with three tiers:

- **HOT** — the context window. Kept lean: current task + a small recall block.
- **WARM** — a token-budgeted digest (~1500 tokens) loaded from disk at every
  session start: pinned facts, open threads, top-scored memories.
- **COLD** — a full SQLite FTS5 archive on disk. Unlimited, searchable,
  never loaded wholesale.

The agent follows a simple lifecycle: **recall at start → capture durable
facts as they appear → checkpoint at 50% context → aggressive compact at
70% → flush at session end.** Stale memories fade via importance-weighted
exponential decay; frequently-used ones survive; pinned ones never die.

## Install

```bash
git clone https://github.com/YOUR_USERNAME/agent-memory
python3 agent-memory/scripts/memory.py init
```

Requires Python 3.8+ (ships with macOS). No pip installs.

## Quick start

```bash
# store a memory
python3 scripts/memory.py add "User prefers concise answers" --type preference --importance 4

# load the session-start digest
python3 scripts/memory.py recall --budget 1500

# search the archive
python3 scripts/memory.py search "deployment target"

# end-of-session flush with unfinished work
python3 scripts/memory.py compact \
  --summary "Refactored auth module, tests passing" \
  --thread "Add rate limiting to login endpoint"

# maintenance
python3 scripts/memory.py prune --dry-run
python3 scripts/memory.py stats
python3 scripts/memory.py export --out backup.md
```

## Wiring it into your agent

Drop the folder into your agent's skills directory. `SKILL.md` contains the
full operating instructions the agent follows — when to recall, when to
checkpoint, what to store, what never to store. Works with any agent
framework that can read a markdown skill file and shell out to Python
(Claude-style skills, the agent, OpenHands, custom loops).

Memory types: `fact`, `preference`, `decision`, `thread`, `episode`, `skill` —
each with its own decay half-life. See `references/architecture.md` for the
scoring math, tuning tables, launchd automation, and the embedding-retrieval
upgrade path.

## Design principles

1. Verbatim history is disposable; distilled memory is not.
2. Decay over deletion — soft-prune by score, never hard-delete automatically.
3. Threads are first-class — unfinished work surfaces every session until closed.
4. No secrets in the store, ever.
5. Boring storage wins — SQLite survives crashes, needs no server, and your
   memories outlive any framework.

## License

MIT © 2026 
[vikrant-rai](https://x.com/vikrai101) · 
X [@vikrai101](https://x.com/vikrai101)
LinkedIn: https://www.linkedin.com/in/vikrantr1
