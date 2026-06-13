#!/usr/bin/env python3
"""
agent-memory: tiered long-term memory engine for local AI agents.

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

TYPES = ("fact", "preference", "decision", "thread", "episode", "skill")
HALF_LIFE_DAYS = {  # recency decay half-life per type
    "fact": 180, "preference": 365, "decision": 120,
    "thread": 30, "episode": 21, "skill": 365,
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
    """Build a token-budgeted context block: pinned + open threads + top-scored, newest episodes first."""
    rows = con.execute("SELECT * FROM memories WHERE status='active'").fetchall()
    pinned = [r for r in rows if r["importance"] >= 5]
    threads = [r for r in rows if r["type"] == "thread" and r["importance"] < 5]
    rest = [r for r in rows if r["importance"] < 5 and r["type"] != "thread"]
    rest.sort(key=lambda r: score(r), reverse=True)

    budget = a.budget * CHARS_PER_TOKEN
    lines, used = ["# MEMORY (long-term, loaded from disk)"], 0

    def emit(header, items):
        nonlocal used
        block = []
        for r in items:
            line = f"- [{r['type']}#{r['id']}] {r['content']}"
            if used + len(line) > budget:
                break
            block.append(line)
            used += len(line)
        if block:
            lines.append(f"\n## {header}")
            lines.extend(block)

    emit("Pinned (always true)", pinned)
    emit("Open threads (resume these)", threads)
    emit("Relevant context (decay-ranked)", rest)
    print("\n".join(lines))


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

    a = p.parse_args()
    con = db()
    init(con)
    {"init": lambda c, x: print(json.dumps({"db": str(DB_PATH), "status": "ready"})),
     "add": cmd_add, "search": cmd_search, "recall": cmd_recall, "compact": cmd_compact,
     "threads": cmd_threads, "close-thread": cmd_close_thread, "touch": cmd_touch,
     "prune": cmd_prune, "stats": cmd_stats, "export": cmd_export}[a.cmd](con, a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os._exit(0)  # output was piped to head/less and closed early — fine
