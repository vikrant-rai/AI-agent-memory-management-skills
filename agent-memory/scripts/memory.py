#!/usr/bin/env python3
"""
agent-memory: tiered long-term memory engine for local AI agents.

Author:  vikrant-rai  (X: @vikrai101)
License: MIT

Pure stdlib + SQLite FTS5. No network, no embeddings required.
Store: ~/.agent-memory/memory.db (override with AGENT_MEMORY_DIR).

Commands:
  init                          Create the store
  add                           Add a memory item
  search QUERY                  Full-text search the archive
  recall                        Token-budgeted context block for session start
  compact                       Store an episode summary + roll open threads
  threads                       List open threads
  close-thread ID               Mark a thread resolved
  touch ID [ID...]              Bump access count (call when a memory is used)
  prune                         Score-based decay pruning (use --dry-run first)
  stats                         Store health summary
  export                        Human-readable markdown dump
"""

import argparse, json, math, os, re, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

MEM_DIR = Path(os.environ.get("AGENT_MEMORY_DIR", Path.home() / ".agent-memory"))
DB_PATH = MEM_DIR / "memory.db"
RUNTIME_PATH = MEM_DIR / "runtime.json"  # live token telemetry, updated each turn

TYPES = ("fact", "preference", "decision", "thread", "episode", "skill", "task")
HALF_LIFE_DAYS = {  # recency decay half-life per type
    "fact": 180, "preference": 365, "decision": 120,
    "thread": 30, "episode": 21, "skill": 365, "task": 30,
}
PRUNE_FLOOR = 0.35  # items scoring below this are prune candidates
CHARS_PER_TOKEN = 4  # rough heuristic for budgeting


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db():
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS memories(
      id INTEGER PRIMARY KEY,
      type TEXT NOT NULL,
      content TEXT NOT NULL,
      tags TEXT DEFAULT '',
      importance INTEGER DEFAULT 3,      -- 1..5; 5 = pinned, never pruned
      status TEXT DEFAULT 'active',      -- active | resolved | pruned
      task_state TEXT DEFAULT NULL,      -- for type='task': pending | doing | done
      created_at TEXT NOT NULL,
      last_access TEXT NOT NULL,
      access_count INTEGER DEFAULT 0,
      session_id TEXT DEFAULT ''
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
      content, tags, content='memories', content_rowid='id'
    );
    CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
      INSERT INTO mem_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF content, tags ON memories BEGIN
      INSERT INTO mem_fts(mem_fts, rowid, content, tags) VALUES('delete', old.id, old.content, old.tags);
      INSERT INTO mem_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
      INSERT INTO mem_fts(mem_fts, rowid, content, tags) VALUES('delete', old.id, old.content, old.tags);
    END;
    """)
    # Migration: add task_state to pre-existing DBs that lack it.
    cols = [r["name"] for r in con.execute("PRAGMA table_info(memories)").fetchall()]
    if "task_state" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN task_state TEXT DEFAULT NULL")
    con.commit()


def score(row, ref_ts=None):
    """importance x recency-decay x usage boost. Pinned (importance 5) -> inf."""
    if row["importance"] >= 5:
        return float("inf")
    ref = ref_ts or time.time()
    last = datetime.strptime(row["last_access"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    age_days = max(0.0, (ref - last) / 86400)
    hl = HALF_LIFE_DAYS.get(row["type"], 90)
    decay = math.pow(0.5, age_days / hl)
    usage = 1 + math.log1p(row["access_count"]) * 0.4
    return row["importance"] * decay * usage


def fts_escape(q):
    """Quote each term so user punctuation can't break FTS5 syntax."""
    terms = re.findall(r"\w+", q)
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "at",
    "by", "from", "is", "are", "be", "this", "that", "it", "as", "into", "my",
    "your", "i", "we", "add", "build", "make", "do", "fix", "update", "create",
    "set", "get", "run", "new", "some", "all", "any", "then", "out",
}


def sig_terms(text):
    """Significant (non-stopword) lowercased word set for overlap comparison."""
    return {w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def resolve_usage(used_arg, window_arg):
    """Return (used, window, source). Explicit args win; otherwise read the last
    turn's telemetry from runtime.json; otherwise fall back to safe defaults."""
    if used_arg is not None and window_arg is not None:
        return used_arg, window_arg, "explicit-args"
    rt = {}
    if RUNTIME_PATH.exists():
        try:
            rt = json.loads(RUNTIME_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            rt = {}
    used = used_arg if used_arg is not None else rt.get("used", 0)
    window = window_arg if window_arg is not None else rt.get("window", 0)
    src = "runtime.json" if rt else "default-fallback"
    if not window:  # never divide by zero; default to a conservative 32k
        window, src = 32768, "default-fallback (set --window or run usage-update)"
    return used, window, src


def cmd_usage_update(con, a):
    """Record the latest turn's token telemetry. The agent calls this after every
    model response, feeding it the numbers its inference server already returned:
      Ollama:        prompt_eval_count + eval_count
      llama.cpp:     tokens_evaluated (+ generated)  ; n_ctx from /props
      OpenAI-compat: usage.prompt_tokens + usage.completion_tokens ; total = used
    `used` should include reasoning/CoT tokens (gpt-oss is verbose) — the server's
    completion count already does, so prefer server numbers over text estimates."""
    used = a.used
    if used is None and (a.prompt_tokens is not None or a.completion_tokens is not None):
        used = (a.prompt_tokens or 0) + (a.completion_tokens or 0)
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    state = {"used": used or 0, "window": a.window or 0, "updated_at": now_iso()}
    RUNTIME_PATH.write_text(json.dumps(state, indent=2))
    pct = round(100 * state["used"] / state["window"], 1) if state["window"] else None
    print(json.dumps({**state, "pct": pct}))


def cmd_add(con, a):
    if a.type not in TYPES:
        sys.exit(f"type must be one of {TYPES}")
    ts = now_iso()
    cur = con.execute(
        "INSERT INTO memories(type, content, tags, importance, created_at, last_access, session_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (a.type, a.content.strip(), a.tags or "", a.importance, ts, ts, a.session or ""))
    con.commit()
    print(json.dumps({"id": cur.lastrowid, "type": a.type, "importance": a.importance}))


def cmd_search(con, a):
    rows = con.execute(
        "SELECT m.* FROM mem_fts f JOIN memories m ON m.id = f.rowid"
        " WHERE mem_fts MATCH ? AND m.status='active'"
        + (" AND m.type=?" if a.type else "") +
        " ORDER BY rank LIMIT ?",
        ([fts_escape(a.query)] + ([a.type] if a.type else []) + [a.limit])).fetchall()
    out = [{"id": r["id"], "type": r["type"], "importance": r["importance"],
            "tags": r["tags"], "content": r["content"], "score": round(min(score(r), 999), 3)}
           for r in rows]
    print(json.dumps(out, indent=2))


def cmd_recall(con, a):
    """Token-budgeted context block. Order of priority (never trimmed before lower tiers):
    pinned -> completed tasks (DO NOT REDO) -> open tasks -> open threads -> decay-ranked rest.
    Completed/open tasks are scoped to --session when given, so the ledger that prevents
    re-execution survives compaction and is always visible to the agent."""
    rows = con.execute("SELECT * FROM memories WHERE status='active'").fetchall()
    sess = a.session
    in_sess = lambda r: (not sess) or r["session_id"] == sess

    pinned = [r for r in rows if r["importance"] >= 5]
    done = [r for r in rows if r["type"] == "task" and r["task_state"] == "done" and in_sess(r)]
    open_tasks = [r for r in rows if r["type"] == "task" and r["task_state"] in ("pending", "doing") and in_sess(r)]
    threads = [r for r in rows if r["type"] == "thread" and r["importance"] < 5]
    rest = [r for r in rows if r["importance"] < 5 and r["type"] not in ("thread", "task")]
    rest.sort(key=lambda r: score(r), reverse=True)
    done.sort(key=lambda r: r["created_at"])
    open_tasks.sort(key=lambda r: r["created_at"])

    budget = a.budget * CHARS_PER_TOKEN
    lines, used = ["# MEMORY (long-term, loaded from disk)"], 0

    def emit(header, items, protected=False):
        """protected sections always render (the ledger must never be silently dropped)."""
        nonlocal used
        block = []
        for r in items:
            mark = ""
            if r["type"] == "task" and r["task_state"] == "doing":
                mark = " (IN PROGRESS)"
            line = f"- [{r['type']}#{r['id']}]{mark} {r['content']}"
            if not protected and used + len(line) > budget:
                break
            block.append(line)
            used += len(line)
        if block:
            lines.append(f"\n## {header}")
            lines.extend(block)

    emit("Pinned (always true)", pinned, protected=True)
    emit("Completed this session — DO NOT REDO", done, protected=True)
    emit("Open tasks (continue, do not restart)", open_tasks, protected=True)
    emit("Open threads (resume these)", threads)
    emit("Relevant context (decay-ranked)", rest)
    print("\n".join(lines))


def cmd_task_add(con, a):
    ts = now_iso()
    cur = con.execute(
        "INSERT INTO memories(type, content, tags, importance, task_state, created_at, last_access, session_id)"
        " VALUES ('task',?,?,?, 'pending',?,?,?)",
        (a.content.strip(), a.tags or "", a.importance, ts, ts, a.session or ""))
    con.commit()
    print(json.dumps({"task_id": cur.lastrowid, "state": "pending"}))


def cmd_task_set(con, a):
    if a.state not in ("pending", "doing", "done"):
        sys.exit("state must be pending|doing|done")
    ts = now_iso()
    con.execute("UPDATE memories SET task_state=?, last_access=?, access_count=access_count+1"
                " WHERE id=? AND type='task'", (a.state, ts, a.id))
    con.commit()
    print(json.dumps({"task_id": a.id, "state": a.state}))


def cmd_task_list(con, a):
    q = "SELECT * FROM memories WHERE type='task' AND status='active'"
    params = []
    if a.session:
        q += " AND session_id=?"; params.append(a.session)
    if a.state:
        q += " AND task_state=?"; params.append(a.state)
    rows = con.execute(q + " ORDER BY created_at", params).fetchall()
    print(json.dumps([{"id": r["id"], "state": r["task_state"], "content": r["content"]} for r in rows], indent=2))


def cmd_precheck(con, a):
    """Pre-flight gate. Run BEFORE executing an incoming task. Answers three questions:
      1. Is this task already done?  -> skip (prevents re-execution after compaction)
      2. Will it fit in the window?  -> compact_first if projected usage crosses the ceiling
      3. Is it flagged complex?      -> compact_first proactively to give it headroom
    Returns a JSON recommendation the agent acts on without interrupting flow."""
    rec = {"task": a.task}

    # Optional gate: below this usage fraction, skip the full check entirely.
    # Rationale: under the ceiling, no compaction has happened, verbatim history
    # is intact, and the agent can see its own completed work — so neither the
    # capacity projection nor the duplicate ledger can change the outcome.
    # EXCEPTION: complex tasks always run the full check (never gated). They are
    # the overflow/quality risk — a big task slipping under the gate is exactly
    # what would push usage past the ceiling unchecked.
    if a.gate and a.gate > 0 and not a.complex:
        used, window, src = resolve_usage(a.used, a.window)
        pct = used / window if window else 0
        if pct < a.gate:
            print(json.dumps({
                "task": a.task, "gated": True, "compact_first": False,
                "usage_source": src, "current_pct": round(pct * 100, 1),
                "gate_pct": round(a.gate * 100, 1),
                "recommendation": f"PROCEED — usage {round(pct*100,1)}% is below the "
                                  f"{round(a.gate*100,1)}% gate; full check skipped.",
            }, indent=2))
            return

    # 1. Duplicate / already-completed detection against the done ledger.
    #    FTS gives candidates; we confirm with significant-term overlap so shared
    #    stopwords ("the", "a", "in") can't trigger a false SKIP.
    cand = con.execute(
        "SELECT m.id, m.content FROM mem_fts f JOIN memories m ON m.id=f.rowid"
        " WHERE mem_fts MATCH ? AND m.type='task' AND m.task_state='done' AND m.status='active'"
        " ORDER BY rank LIMIT 5", [fts_escape(a.task)]).fetchall()
    q_terms = sig_terms(a.task)
    best, best_overlap = None, 0.0
    for d in cand:
        d_terms = sig_terms(d["content"])
        if not d_terms or not q_terms:
            continue
        overlap = len(q_terms & d_terms) / len(q_terms | d_terms)  # Jaccard
        shared = len(q_terms & d_terms)
        if (overlap >= 0.4 or shared >= 3) and overlap > best_overlap:
            best, best_overlap = d, overlap
    if best:
        rec["already_completed"] = {"task_id": best["id"], "content": best["content"],
                                    "overlap": round(best_overlap, 2)}
        rec["recommendation"] = "SKIP — a matching task is already marked done. Confirm with the user before redoing."

    # 2 & 3. Capacity projection.
    # used/window are dynamic. If not passed explicitly, read the most recent
    # turn's telemetry that the agent recorded via `usage-update` (runtime.json).
    used, window, src = resolve_usage(a.used, a.window)
    rec["usage_source"] = src
    est = a.est_tokens
    if a.complex:
        est = max(est, 1) * 3  # complex work fans out; reserve 3x headroom
    recall_overhead = a.recall_budget
    projected = used + est + recall_overhead
    ratio = projected / window if window else 0
    rec["capacity"] = {
        "window": window, "used_now": used, "est_task_tokens": est,
        "projected_used": projected, "projected_pct": round(ratio * 100, 1),
        "ceiling_pct": round(a.ceiling * 100, 1),
    }
    if ratio >= a.ceiling:
        rec["compact_first"] = True
        rec.setdefault("recommendation",
            "COMPACT FIRST — projected usage exceeds the ceiling. Compact, start a clean "
            "window with the recall block, THEN execute. The completed-task ledger will "
            "carry forward so finished work is not repeated.")
    else:
        rec["compact_first"] = False
        rec.setdefault("recommendation", "PROCEED — fits within the window; no compaction needed.")
    print(json.dumps(rec, indent=2))


def cmd_compact(con, a):
    """End-of-session flush: store episode summary, optionally open threads, return confirmation."""
    ts = now_iso()
    cur = con.execute(
        "INSERT INTO memories(type, content, tags, importance, created_at, last_access, session_id)"
        " VALUES ('episode',?,?,?,?,?,?)",
        (a.summary.strip(), a.tags or "", a.importance, ts, ts, a.session or ""))
    eid = cur.lastrowid
    tids = []
    for t in (a.thread or []):
        c = con.execute(
            "INSERT INTO memories(type, content, tags, importance, created_at, last_access, session_id)"
            " VALUES ('thread',?,?,4,?,?,?)", (t.strip(), a.tags or "", ts, ts, a.session or ""))
        tids.append(c.lastrowid)
    con.commit()
    print(json.dumps({"episode_id": eid, "thread_ids": tids}))


def cmd_threads(con, a):
    rows = con.execute(
        "SELECT * FROM memories WHERE type='thread' AND status='active' ORDER BY created_at DESC").fetchall()
    print(json.dumps([{"id": r["id"], "content": r["content"], "created": r["created_at"]} for r in rows], indent=2))


def cmd_close_thread(con, a):
    con.execute("UPDATE memories SET status='resolved' WHERE id=? AND type='thread'", (a.id,))
    con.commit()
    print(json.dumps({"closed": a.id}))


def cmd_touch(con, a):
    ts = now_iso()
    for i in a.ids:
        con.execute("UPDATE memories SET access_count=access_count+1, last_access=? WHERE id=?", (ts, i))
    con.commit()
    print(json.dumps({"touched": a.ids}))


def cmd_prune(con, a):
    rows = con.execute("SELECT * FROM memories WHERE status='active'").fetchall()
    victims = [r for r in rows if score(r) < a.floor]
    if a.dry_run:
        print(json.dumps([{"id": r["id"], "type": r["type"], "score": round(score(r), 3),
                           "content": r["content"][:80]} for r in victims], indent=2))
        return
    for r in victims:
        con.execute("UPDATE memories SET status='pruned' WHERE id=?", (r["id"],))
    con.commit()
    print(json.dumps({"pruned": len(victims), "remaining_active": len(rows) - len(victims)}))


def cmd_stats(con, a):
    rows = con.execute(
        "SELECT type, status, COUNT(*) n FROM memories GROUP BY type, status").fetchall()
    total = con.execute("SELECT COUNT(*) n, COALESCE(SUM(LENGTH(content)),0) c FROM memories WHERE status='active'").fetchone()
    print(json.dumps({
        "db": str(DB_PATH),
        "active_items": total["n"],
        "active_est_tokens": total["c"] // CHARS_PER_TOKEN,
        "by_type_status": [{"type": r["type"], "status": r["status"], "count": r["n"]} for r in rows],
    }, indent=2))


def cmd_export(con, a):
    rows = con.execute("SELECT * FROM memories WHERE status='active' ORDER BY type, created_at").fetchall()
    out = ["# Agent Memory Export", f"_Exported {now_iso()}_", ""]
    cur_type = None
    for r in rows:
        if r["type"] != cur_type:
            cur_type = r["type"]
            out.append(f"\n## {cur_type.title()}s\n")
        pin = " 📌" if r["importance"] >= 5 else ""
        out.append(f"- **#{r['id']}**{pin} {r['content']}  _(imp {r['importance']}, {r['created_at'][:10]})_")
    text = "\n".join(out)
    if a.out:
        Path(a.out).write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text)


def main():
    p = argparse.ArgumentParser(prog="memory.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    sp = sub.add_parser("add")
    sp.add_argument("content")
    sp.add_argument("--type", default="fact")
    sp.add_argument("--tags", default="")
    sp.add_argument("--importance", type=int, default=3, choices=range(1, 6))
    sp.add_argument("--session", default="")

    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--type", default=None)
    sp.add_argument("--limit", type=int, default=8)

    sp = sub.add_parser("recall")
    sp.add_argument("--budget", type=int, default=1500, help="max tokens for the recall block")
    sp.add_argument("--session", default="", help="scope task ledger to this session")

    sp = sub.add_parser("compact")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--thread", action="append", help="open thread to carry forward (repeatable)")
    sp.add_argument("--tags", default="")
    sp.add_argument("--importance", type=int, default=3, choices=range(1, 6))
    sp.add_argument("--session", default="")

    sub.add_parser("threads")

    sp = sub.add_parser("close-thread")
    sp.add_argument("id", type=int)

    sp = sub.add_parser("touch")
    sp.add_argument("ids", type=int, nargs="+")

    sp = sub.add_parser("prune")
    sp.add_argument("--floor", type=float, default=PRUNE_FLOOR)
    sp.add_argument("--dry-run", action="store_true")

    sub.add_parser("stats")

    sp = sub.add_parser("export")
    sp.add_argument("--out", default=None)

    sp = sub.add_parser("task-add")
    sp.add_argument("content")
    sp.add_argument("--tags", default="")
    sp.add_argument("--importance", type=int, default=3, choices=range(1, 6))
    sp.add_argument("--session", default="")

    sp = sub.add_parser("task-set", help="set a task's state: pending|doing|done")
    sp.add_argument("id", type=int)
    sp.add_argument("state")

    sp = sub.add_parser("task-list")
    sp.add_argument("--session", default="")
    sp.add_argument("--state", default=None)

    sp = sub.add_parser("precheck", help="pre-flight gate: run BEFORE executing an incoming task")
    sp.add_argument("--task", required=True, help="the incoming task description")
    sp.add_argument("--used", type=int, default=None, help="current context tokens (default: read runtime.json)")
    sp.add_argument("--window", type=int, default=None, help="context window size (default: read runtime.json)")
    sp.add_argument("--est-tokens", dest="est_tokens", type=int, default=4000,
                    help="estimated tokens to handle this task")
    sp.add_argument("--complex", action="store_true", help="flag complex/multi-step work (reserves 3x)")
    sp.add_argument("--ceiling", type=float, default=0.80, help="compact-first threshold (0..1)")
    sp.add_argument("--gate", type=float, default=0.0,
                    help="skip the full check below this usage fraction (e.g. 0.6). 0 = always run.")
    sp.add_argument("--recall-budget", dest="recall_budget", type=int, default=1500)

    sp = sub.add_parser("usage-update", help="record the latest turn's token telemetry (call every turn)")
    sp.add_argument("--used", type=int, default=None, help="total context tokens in use now")
    sp.add_argument("--prompt-tokens", dest="prompt_tokens", type=int, default=None)
    sp.add_argument("--completion-tokens", dest="completion_tokens", type=int, default=None)
    sp.add_argument("--window", type=int, default=None, help="configured context window (n_ctx)")

    a = p.parse_args()
    con = db()
    init(con)
    {"init": lambda c, x: print(json.dumps({"db": str(DB_PATH), "status": "ready"})),
     "add": cmd_add, "search": cmd_search, "recall": cmd_recall, "compact": cmd_compact,
     "threads": cmd_threads, "close-thread": cmd_close_thread, "touch": cmd_touch,
     "prune": cmd_prune, "stats": cmd_stats, "export": cmd_export,
     "task-add": cmd_task_add, "task-set": cmd_task_set, "task-list": cmd_task_list,
     "precheck": cmd_precheck, "usage-update": cmd_usage_update}[a.cmd](con, a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os._exit(0)  # output was piped to head/less and closed early — fine
