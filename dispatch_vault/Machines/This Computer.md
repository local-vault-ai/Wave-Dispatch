---
type: machine
enabled: true
provider: ollama
# Default: the standard local Ollama port. Override with WAVE_DISPATCH_ENDPOINT in .env (e.g. http://127.0.0.1:8003
# for an Ollama on another port, or provider: openai + http://127.0.0.1:8080/v1 for llama.cpp / LM Studio / MLX).
endpoint: "${WAVE_DISPATCH_ENDPOINT:-http://127.0.0.1:11434}"
vram_gb: 24
max_parallel_models: 1
tags: [wave-dispatch/machine]
---
# This Computer

The one computer Wave Dispatch uses out of the box. Every bot in [[AI Bots]] runs here.

The team uses six models: two workers ([[qwen2.5-coder 7b]], [[llama3.1 8b]]) and four tiny ones of our own
([[wave-gate]], [[wave-gate2]], [[wave-split]], [[wave-review]], about 0.7 GB each). Keep them all loaded, or every question waits
for models to swap in (measured: 17-50 s instead of 1-6 s per question):

- Ollama: set `OLLAMA_MAX_LOADED_MODELS=6`, `OLLAMA_CONTEXT_LENGTH=16384` and `OLLAMA_KEEP_ALIVE=-1`, then restart
  Ollama. All six fit in about 19 GB, so a 24 GB graphics card holds them all.
- 16 GB: use `OLLAMA_CONTEXT_LENGTH=8192`.

Want more power? See [[Second Computer]].
