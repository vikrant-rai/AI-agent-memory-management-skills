#!/usr/bin/env python3
"""
agent_loop_example.py — reference wiring for agent-memory.

Author:  vikrant-rai  (X: @vikrai101)
License: MIT

A minimal, framework-agnostic skeleton showing how a local agent should drive
the memory governance every turn. Swap `call_model` and `read_usage` for your
real inference client (Ollama, llama.cpp, vLLM, LM Studio). Everything else is
copy-paste ready.

The per-turn contract:

  SESSION START
    -> recall()                     load the disk-backed digest into context

  EACH INCOMING TASK
    -> precheck()                   BEFORE doing anything:
         already_completed?  -> confirm + skip (no re-execution)
         compact_first?      -> compact(), then recall() on a clean window
    -> task_add() + task_set(doing)
    -> ... do the work via the model ...
    -> task_set(done)               write to the non-decaying ledger
    -> usage_update()               record live token telemetry for next precheck

  SESSION END
    -> compact() with open threads  flush summary, carry unfinished work forward

Run `python3 agent_loop_example.py` to see a simulated 3-task session print the
exact CLI calls and decisions, using a fake model so it needs no server.
"""

import json
import subprocess
import sys
from pathlib import Path

MEM = [sys.executable, str(Path(__file__).with_name("memory.py"))]
SESSION = "demo-session-001"
WINDOW = 80000          # your configured num_ctx
RECALL_BUDGET = 2000    # 2.5% of an 80K window


# --------------------------------------------------------------------------
# Thin wrappers around the memory CLI. Each returns parsed JSON where useful.
# --------------------------------------------------------------------------
def mem(*args, capture=True):
    """Invoke the memory CLI. Returns parsed JSON when the command emits JSON."""
    res = subprocess.run(MEM + list(args), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"memory.py {args[0]} failed: {res.stderr.strip()}")
    out = res.stdout.strip()
    if not capture or not out:
        return out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out  # recall returns a markdown block, not JSON


def recall():
    return mem("recall", "--session", SESSION, "--budget", str(RECALL_BUDGET), capture=False)


def precheck(task, complex_task=False):
    # Quality-first config: --gate 0.70 --ceiling 0.85. Model quality degrades as
    # the window fills (lost-in-the-middle / context rot), so we keep the working
    # window mostly under ~85% rather than chasing raw window utilization. Below
    # 70% the check is skipped; complex tasks ALWAYS run the check (they bypass
    # the gate) so a big task can't push past the 85% quality ceiling unchecked.
    args = ["precheck", "--task", task, "--recall-budget", str(RECALL_BUDGET),
            "--gate", "0.70", "--ceiling", "0.85"]
    if complex_task:
        args.append("--complex")
    return mem(*args)  # used/window auto-read from runtime.json


def task_add(desc):
    return mem("task-add", desc, "--session", SESSION)["task_id"]


def task_set(task_id, state):
    return mem("task-set", str(task_id), state)


def compact(summary, threads=None):
    args = ["compact", "--summary", summary, "--session", SESSION]
    for t in (threads or []):
        args += ["--thread", t]
    return mem(*args)


def usage_update(prompt_tokens, completion_tokens):
    return mem("usage-update", "--prompt-tokens", str(prompt_tokens),
               "--completion-tokens", str(completion_tokens), "--window", str(WINDOW))


# --------------------------------------------------------------------------
# REPLACE THESE TWO with your real inference client.
# --------------------------------------------------------------------------
def call_model(prompt, context_block):
    """Stub. Your real call sends context_block + prompt to gpt-oss-120b and
    returns (text, prompt_tokens, completion_tokens) from the response usage."""
    # e.g. Ollama: r = requests.post(".../api/chat", json=...); j = r.json()
    #   return j["message"]["content"], j["prompt_eval_count"], j["eval_count"]
    fake_text = f"(model output for: {prompt[:40]}...)"
    return fake_text, 1200, 800


def read_usage_after_turn(turn_index):
    """Stub returning simulated growing occupancy so the demo crosses the ceiling.
    Your real code reads this from the last response's usage object."""
    table = [(34000, 4000), (44000, 5000), (54000, 6000)]
    return table[min(turn_index, len(table) - 1)]


# --------------------------------------------------------------------------
# The loop.
# --------------------------------------------------------------------------
def handle_task(task, complex_task, turn_index):
    print(f"\n=== INCOMING TASK: {task!r} (complex={complex_task}) ===")

    # 1. PRE-FLIGHT GATE — before touching the model.
    check = precheck(task, complex_task)
    if check.get("gated"):
        print(f"  precheck: GATED at {check['current_pct']}% (< {check['gate_pct']}%), full check skipped")
    else:
        print(f"  precheck: source={check['usage_source']} "
              f"projected={check['capacity']['projected_pct']}% "
              f"compact_first={check['compact_first']}")

    if "already_completed" in check:
        print(f"  -> SKIP: matches done task #{check['already_completed']['task_id']} "
              f"({check['already_completed']['content']!r}). Confirm with user before redoing.")
        return

    if check["compact_first"]:
        print("  -> COMPACT FIRST: window would overflow. Flushing + clean recall.")
        compact(f"Auto-checkpoint before task: {task}")
        context = recall()  # fresh, small window carrying the ledger forward
    else:
        context = recall()
        print("  -> PROCEED: fits in window.")

    # 2. EXECUTE — track the task across its lifecycle so the ledger stays true.
    tid = task_add(task)
    task_set(tid, "doing")
    text, p_tok, c_tok = call_model(task, context)
    print(f"  model: {text}")
    task_set(tid, "done")                 # -> "DO NOT REDO" ledger, survives compaction

    # 3. RECORD TELEMETRY — so the next precheck reads accurate occupancy.
    p_tok, c_tok = read_usage_after_turn(turn_index)
    u = usage_update(p_tok, c_tok)
    print(f"  usage-update: {u['used']}/{u['window']} tokens ({u['pct']}%)")


def main():
    mem("init")
    print(f"Session {SESSION} | window {WINDOW} | recall budget {RECALL_BUDGET}")
    print("\n--- SESSION START: loading recall block ---")
    print(recall())

    handle_task("Summarize today's threat-intel feeds", complex_task=False, turn_index=0)
    handle_task("Build the full exec briefing deck with charts", complex_task=True, turn_index=2)
    handle_task("Summarize today's threat-intel feeds", complex_task=False, turn_index=2)  # dup

    print("\n--- SESSION END: flushing summary + open threads ---")
    end = compact("Demo session: ran intel summary + briefing deck",
                  threads=["Wire real Ollama usage into read_usage_after_turn"])
    print(f"  compact: episode #{end['episode_id']}, threads {end['thread_ids']}")


if __name__ == "__main__":
    main()
