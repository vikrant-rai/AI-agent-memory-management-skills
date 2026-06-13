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

### 3. Before executing any incoming task — the pre-flight gate
This is the fix for two failure modes: redoing already-completed work, and a
big task landing when the window is already ~65% full and blowing out
mid-execution. Run `precheck` BEFORE you start the task — it answers three
questions in one call without interrupting your flow:

```bash
python3 scripts/memory.py precheck \
  --task "refactor the auth module and add tests" \
  --used 52000 --window 80000 --est-tokens 4000 --complex --session 2026-06-12-a
```

It returns JSON telling you:

- **`already_completed`** — a matching task is in the done ledger. **SKIP it**
  (confirm with the user first). This is what stops the agent re-running
  finished work after a compaction wiped the verbatim history.
- **`compact_first: true`** — projected usage (current + task estimate +
  recall block) crosses the ceiling (default 80%). **Compact FIRST, start a
  clean window with the recall block, THEN execute.** Because the completed
  ledger and open tasks survive compaction, nothing gets repeated.
- **`compact_first: false`** — fits; proceed normally.

Pass `--complex` for multi-step work; it reserves 3× headroom so the task
won't run out of room halfway. Tune `--est-tokens` to your typical task cost.

**Where `--used` comes from (it's dynamic — sample it every turn):** you never
hardcode it. After each model response, record the server's token counts once;
then call `precheck` with NO `--used`/`--window` and it auto-reads the latest
from `runtime.json`:

```bash
# Ollama: prompt_eval_count + eval_count from the response JSON
python3 scripts/memory.py usage-update --prompt-tokens 48000 --completion-tokens 4000 --window 80000
python3 scripts/memory.py precheck --task "..." --complex   # used/window auto-read
```

See `scripts/ollama_adapter.sh` for llama.cpp and OpenAI-compatible
(vLLM/LM Studio) extraction. **gpt-oss is a reasoning model — its
chain-of-thought tokens count against the window, so always use the server's
reported counts (which include generated/reasoning tokens), never an estimate
from visible text.**

### Performance & the optional gate
`precheck` is local-only — it never calls the model. Measured cost is ~38ms
(mostly Python process startup), under 1% of a multi-second gpt-oss turn, so it
will not slow your loop. You do **not** need to restrict it to high-usage turns
for speed.

That said, below the ceiling the check is a logical no-op (history is intact,
nothing to compact, completed work is still visible in-context). If you prefer
to skip it there, pass `--gate 0.70` and it early-exits with a fast PROCEED when
usage is under 70%. **Complex tasks always run the full check — they bypass the
gate** — because a large task slipping through is exactly what would push usage
past the ceiling unchecked.

**Why a quality-first ceiling (85%, not 95–100%).** Model output quality
degrades as the context window fills ("lost in the middle" / context rot), often
well before the window is physically full — frequently noticeable around
65–70% on a mid-size window, and worse for retrieval-heavy or multi-step tasks.
So the goal is not to maximize window *utilization*; it's to keep the working
window in its high-quality zone. Running to 95–100% yields more tokens of
*worse* output, which means errors and redone work — negative throughput.
Recommended default: **`--gate 0.70 --ceiling 0.85`** on an 80K window. This
keeps the working set mostly under the degradation zone, leaves a 12K-token
overflow buffer, and (because compaction trigger points cluster within ~3K
tokens regardless of ceiling) costs essentially no throughput versus running
hotter. The exact degradation onset is model- and task-dependent — watch your
own outputs; drop the ceiling to 0.80 if quality slips, raise toward 0.88 if you
find compaction is firing too often and losing useful detail.


- `--window 80000`, `--recall-budget 2000` (2.5% of window for the digest).
- Ceiling 80% = **64K tokens**. With ~52K in use, a complex task
  (`est 4000 × 3 = 12K` + 2K recall) projects to 66K → **COMPACT FIRST** — the
  exact "task at 65%" case that used to redo work.
- A bigger window would not fix re-execution; only the ledger does. Even at
  gpt-oss's full 131K you'd want this gate, because verbose reasoning degrades
  "lost in the middle" well before the window is physically full.

**Track tasks as first-class objects so the ledger stays accurate:**

```bash
python3 scripts/memory.py task-add "Refactor auth module" --session 2026-06-12-a
python3 scripts/memory.py task-set 7 doing      # when you start
python3 scripts/memory.py task-set 7 done       # when finished
python3 scripts/memory.py task-list --session 2026-06-12-a --state done
```

Completed and open tasks appear in every `recall` block (scope with
`--session`) under **"Completed this session — DO NOT REDO"** and **"Open
tasks"**, and are never trimmed by the token budget — so even after compaction
the agent always knows what's done and what's still open.

### 4. Context pressure thresholds — the compaction loop
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

### 5. Session end
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
