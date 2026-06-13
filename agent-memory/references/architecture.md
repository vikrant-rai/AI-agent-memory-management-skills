# Architecture & Tuning Reference

## Why this design

The failure mode this skill fixes: a local agent with a ~65k context window
accumulates verbatim history and hits 30–55% usage within one or two chats.
Beyond ~70%, three things degrade at once — retrieval quality inside the
window ("lost in the middle"), generation latency, and the agent's ability to
follow its own earlier instructions. The fix is not a bigger window; it's a
**write-behind cache**: the window holds only the working set, and everything
durable is written through to disk.

Key choices and why:

- **SQLite FTS5, not a vector DB.** Zero dependencies, ships with macOS
  Python, survives crashes (WAL mode), and BM25 full-text ranking is genuinely
  good for memory recall where queries share vocabulary with stored items
  (they usually do — both came from the same conversations). Embeddings are an
  upgrade path, not a prerequisite (see below).
- **Decay scoring, not FIFO eviction.** `importance × 0.5^(age_days/half_life)
  × (1 + 0.4·ln(1+access_count))`. This mirrors how useful memory actually
  behaves: things you keep using stay vivid; things you never touch fade. Each
  type gets its own half-life because a preference ("concise answers") should
  outlive an episode ("what we did Tuesday") by an order of magnitude.
- **Soft deletes.** `prune` flips a status flag. Nothing is destroyed until
  the operator vacuums. This makes aggressive pruning safe to automate.
- **Threads as first-class objects.** The single biggest quality win for
  multi-session agents is reliably resuming unfinished work. Threads are
  surfaced in every recall block until explicitly closed.

## Half-life table (tune in `memory.py`)

| type | half-life (days) | rationale |
|---|---|---|
| preference | 365 | how the user works changes slowly |
| skill | 365 | learned how-tos stay valid until tooling changes |
| fact | 180 | durable but worth re-verifying twice a year |
| decision | 120 | decisions get superseded by newer decisions |
| thread | 30 | a thread untouched for a month is probably dead |
| episode | 21 | session summaries are mostly short-term scaffolding |

If your agent runs daily, these defaults are good. If it runs weekly, double
the thread/episode half-lives.

## Context thresholds — why 50/70

- Below 50%: full-fidelity reasoning, no action needed.
- 50%: cheap insurance. One episode write costs ~10 seconds and means a crash
  or truncation loses nothing settled.
- 70%: quality has measurably started to degrade for most local models in the
  30–70B class. Compact + fresh session with a 1500-token recall block almost
  always outperforms grinding on at 85%.
- Never let the window hit 90% on a working session — by then the model is
  paraphrasing its own summaries of summaries.

How the agent knows its usage: most local runners (Ollama, LM Studio, llama.cpp
server) report prompt token counts per request. Track cumulative prompt tokens
against the model's context length. If the runner doesn't expose it, estimate
with `total_chars / 4`.

## Recall block sizing

The recall block is the only memory cost paid every session. Budget guidance:

| window | recall budget | % of window |
|---|---|---|
| 32k | 1000 | 3% |
| 65k | 1500 | 2.3% |
| 128k | 2500 | 2% |
| 200k+ | 4000 | 2% |

Pinned items are emitted first and never trimmed; threads next; decay-ranked
context fills the remainder. If pinned items alone blow the budget, you have
too many importance-5 memories — demote some.

## Automation on macOS (launchd)

Weekly prune + backup export, no cron needed. Save as
`~/Library/LaunchAgents/com.agent.memory-maintenance.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agent.memory-maintenance</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-c</string>
    <string>python3 ~/skills/agent-memory/scripts/memory.py prune &amp;&amp; python3 ~/skills/agent-memory/scripts/memory.py export --out ~/agent-memory-backup.md</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>9</integer>
  </dict>
</dict></plist>
```

Load with `launchctl load ~/Library/LaunchAgents/com.agent.memory-maintenance.plist`.

## Upgrade path: embedding retrieval

FTS5 misses paraphrase recall ("the database thing we picked" won't match
"chose SQLite"). When that starts to hurt:

1. Add an `embedding BLOB` column to `memories`.
2. On `add`, call a local embedding model (e.g. `nomic-embed-text` via
   Ollama's `/api/embeddings`) and store the vector.
3. On `search`, run FTS5 AND cosine-similarity, merge with reciprocal rank
   fusion (RRF, k=60).

Keep FTS5 as the fallback — hybrid beats either alone, and the skill still
works on machines without an embedding model.

## Interop notes

- **Mnemosyne / MemOS**: this store can sit underneath either. Both expect a
  KV-or-document backend; point their persistence adapter at functions
  wrapping `add`/`search`/`recall`. Or run this standalone and treat
  Mnemosyne as a future replacement for the WARM tier only — the COLD archive
  format (plain SQLite) migrates trivially.
- **Multiple agents**: give each agent its own `AGENT_MEMORY_DIR`, or share
  one DB and namespace with `--tags "agent:myagent"`.
- **Git-friendly backups**: `export --out` produces deterministic-ordered
  markdown — commit it to a private repo for versioned, human-auditable
  memory history. Do not commit `memory.db` itself (binary, churns on every
  touch).

## Failure modes to watch

- **Memory spam**: an over-eager agent storing every turn. Symptom: stats
  show hundreds of importance-3 facts. Fix: raise the bar in the agent prompt
  ("store only what would matter in a week"), prune with `--floor 0.6` once.
- **Pin inflation**: everything becomes importance 5. Recall block fills with
  pins, ranked context gets crowded out. Audit with
  `search "*" --limit 50` and demote.
- **Stale threads**: threads that will never be resumed. The 30-day half-life
  handles it, but a monthly `threads` review is healthier.
