#!/usr/bin/env bash
# Example: extract live token telemetry from common local servers and feed it
# to agent-memory each turn. Adapt the jq paths to your runner.
# Network calls below are illustrative — run on the host where the server lives.
MEM="python3 $(dirname "$0")/memory.py"

# --- OLLAMA (/api/chat returns prompt_eval_count + eval_count per response) ---
# After your agent gets a chat response saved to resp.json:
#   prompt=$(jq .prompt_eval_count resp.json); gen=$(jq .eval_count resp.json)
#   $MEM usage-update --prompt-tokens "$prompt" --completion-tokens "$gen" --window "$NUM_CTX"
#
# IMPORTANT: Ollama defaults num_ctx to 4096 unless you set it. For gpt-oss-120b
# pass options.num_ctx explicitly (e.g. 65536) in your request, or the model is
# silently truncated to 4k and "compression produces odd responses" — that 4k
# default is a very common cause of the exact symptom you described.

# --- llama.cpp / llama-server ---
#   NUM_CTX=$(curl -s localhost:8080/props | jq .default_generation_settings.n_ctx)
#   used=$(jq .tokens_evaluated resp.json)
#   $MEM usage-update --used "$used" --window "$NUM_CTX"

# --- OpenAI-compatible (vLLM, LM Studio, llama-server OAI endpoint) ---
#   pt=$(jq .usage.prompt_tokens resp.json); ct=$(jq .usage.completion_tokens resp.json)
#   $MEM usage-update --prompt-tokens "$pt" --completion-tokens "$ct" --window "$NUM_CTX"

# Then, before executing any incoming task:
#   $MEM precheck --task "$INCOMING_TASK" --complex   # used/window auto-read
