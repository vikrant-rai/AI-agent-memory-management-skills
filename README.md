# AI-agent-memory-management-skills
Memory Skill — SQLite FTS5 persistent memory for multi-session AI agents. Features decay scoring, thread tracking, session checkpointing, and auto-prune. Includes SKILL.md (behavioral spec), memory.py (zero-dependency engine), and architecture notes with Ollama + MemOS upgrade paths.

Important Disclaimer:

Memory Skill — SQLite FTS5 persistent memory for multi-session AI agents. Features decay scoring, thread tracking, session checkpointing, and auto-prune. Includes SKILL.md (behavioral spec), memory.py (zero-dependency engine), and architecture notes with Ollama + MemOS upgrade paths.

This skill and accompanying scripts are provided as-is under the MIT License for personal and experimental use. The memory engine writes to `~/.agent/memory/` on your local filesystem — review the storage path and access permissions before deploying in shared or production environments.

Decay scoring, compaction thresholds, and recall behavior are tuned for single-user, multi-session workflows. Results will vary depending on your model, session length, and the volume and quality of facts captured. The "store only what matters in a week" heuristic in SKILL.md is a starting point, not a guarantee — monitor `stats` output after a few days of real use and adjust thresholds to your needs before relying on this in critical workflows.

The `.db` file may contain sensitive personal data, decisions, or preferences accumulated over time. If you branch a copy of this file **DO NOT / Never commit the `.db` to version control.** The `.gitignore` included in the zip excludes it by default — verify this is intact if you restructure the repo.

MemOS and Mnemosyne interop notes in `architecture.md` describe a planned upgrade path, not a tested integration. The Ollama embedding-retrieval path requires a separately installed and running Ollama instance with `nomic-embed-text` pulled — it is not bundled here.

No warranty is made regarding data integrity across SQLite versions, OS-level file locking, or concurrent access scenarios. The launchd plist for weekly auto-prune is macOS-specific; Linux users will need an equivalent systemd timer.

Contributions and issue reports are welcome. If you publish a fork or derivative, please retain attribution and update the disclaimer to reflect your changes.
