---
name: agent-memory
description: >
  Tiered long-term memory management for local AI agents. Use this skill at the
  START of every session (to load the recall block), whenever context usage
  crosses 50% (checkpoint) or 70% (aggressive compact), at the END of every
  session (to flush summaries to disk), and whenever the user states a durable
  fact, preference, or decision worth remembering. Also use when the user asks
  "what do you remember", "save this", "forget that", or complains about
  context filling up. Persists memory to disk via SQLite FTS5 — no network, no
  dependencies beyond Python 3.
---

# Agent Memory

Self-managed, tiered memory for long-running local agents. The goal: keep the
context window small and relevant, while nothing important is ever lost —
because it lives on disk, not in the window.

All commands use the bundled engine:

```bash
python3 scripts/memory.py <command> ...
```

Store location: `~/.agent-memory/memory.db` (override with `AGENT_MEMORY_DIR`).
Run `python3 scripts/memory.py init` once on first use.

## The three tiers

1. **HOT** — what's in the context window right now. Keep it lean: current task,
   the recall block, last few turns.
2. **WARM** — the recall block: a token-budgeted digest loaded from disk at
   session start (`recall --budget 1500`). Pinned facts, open threads, and the
   highest-scoring memories.
3. **COLD** — the full archive on disk, searchable any time with `search`.
   Unlimited size; never loaded wholesale into context.

The discipline that makes this work: **verbatim conversation history is
disposable; distilled memory is not.** Old turns get summarized into episodes
and dropped from context. The archive remembers; the window forgets.

## Memory types

| type | use for | example |
|---|---|---|
| `fact` | durable truths about the user/world | "User's primary machine is an M5 Mac, 128GB" |
| `preference` | how the user likes things done | "Prefers concise answers, no filler" |
| `decision` | choices made, with rationale | "Chose SQLite over Postgres for portability" |
| `thread` | unfinished work to resume | "Refactor of auth module half done; next: tests" |
| `episode` | summary of a past session | "2026-06-12: built memory skill, tested pruning" |
| `skill` | learned how-tos | "User's build script needs `--legacy-peer-deps`" |

Importance 1–5. **5 = pinned, never pruned, always in the recall block.** Use 5
sparingly — identity-level facts only. Default is 3.

## The session lifecycle (follow this every session)

### 1. Session start
Load the recall block and inject it into working context:

```bash
python3 scripts/memory.py recall --budget 1500
```

If the user's first message relates to past work, also run a targeted search
and `touch` any memories you actually use (touching feeds the decay scoring):

```bash
python3 scripts/memory.py search "auth refactor" --limit 5
python3 scripts/memory.py touch 12 47
```

### 2. During the session — capture as you go
When the user states something durable, save it immediately. Don't wait for
session end; sessions crash and context truncates.

```bash
python3 scripts/memory.py add "Deploy target is Azure, not AWS" --type decision --importance 4 --tags "infra,azure"
```

Write memories as **self-contained, third-person, specific** statements. Bad:
"he liked option 2". Good: "For the scanner UI, user chose the dark
precision-instrument aesthetic (option 2) over the light dashboard style."

### 3. Context pressure thresholds — this is the core loop
Monitor context usage. Act at these thresholds:

- **~50% — checkpoint.** Summarize everything settled so far into one episode,
  store it, then mentally release the verbatim detail. You may now respond
  based on the summary instead of re-reading old turns.

  ```bash
  python3 scripts/memory.py compact --summary "Designed memory schema: SQLite FTS5, decay scoring, 3 tiers. Settled on half-life per type." --tags "memory-skill" --session "2026-06-12-a"
  ```

- **~70% — aggressive compact.** Store an episode AND open threads for anything
  unfinished, then tell the user: *"Context is getting full — I've checkpointed
  to long-term memory. We can continue here or start a fresh session; I'll pick
  up exactly where we left off."* A fresh session + recall block beats a
  degraded full window every time.

  ```bash
  python3 scripts/memory.py compact \
    --summary "Built memory.py CLI; all commands tested except export" \
    --thread "Test export command and fix any formatting issues" \
    --thread "Write GitHub README" \
    --session "2026-06-12-a"
  ```

### 4. Session end
Always flush before the session closes: one `compact` with a summary of what
happened and `--thread` flags for anything unfinished. Close threads that got
resolved:

```bash
python3 scripts/memory.py close-thread 12
```

## Maintenance (weekly or when stats look bloated)

```bash
python3 scripts/memory.py stats
python3 scripts/memory.py prune --dry-run   # review candidates first
python3 scripts/memory.py prune             # then commit
python3 scripts/memory.py export --out ~/agent-memory-backup.md
```

Pruning uses decay scoring: `importance × 0.5^(age/half-life) × usage-boost`.
Memories the agent keeps touching survive; stale low-importance ones fade.
Pruned items are soft-deleted (status flag), so nothing is destroyed until you
vacuum the DB yourself.

## What NOT to store

- Secrets, API keys, passwords, tokens — never.
- Verbatim conversation transcripts — store distilled episodes instead.
- Anything the user asks you to forget — find it with `search`, then mark it
  pruned, and confirm to the user.
- Speculation about the user's mental state or unverified inferences.

## Rules of thumb

- Recall budget 1500 tokens is right for a 65k window (~2-3%). Scale
  proportionally: ~1000 for 32k, ~4000 for 200k.
- Prefer many small atomic memories over one giant blob — FTS retrieval and
  pruning both work at item granularity.
- When in doubt about importance, use 3. Promote later with repeated `touch`.
- If `search` returns nothing relevant, say so honestly — don't fabricate
  remembered context.

For design rationale, tuning parameters, and integration patterns (launchd
scheduled pruning, embedding-based retrieval upgrade path, Mnemosyne/MemOS
interop), read `references/architecture.md`.
