---
type: machine
enabled: false
provider: ollama
# Link Another Computer: 1) on that computer run Ollama reachable from this one (OLLAMA_HOST=0.0.0.0:11434 and a
# firewall rule that admits ONLY this computer, or better, a TLS proxy with a key), 2) set WAVE_DISPATCH_NODE2_URL in .env,
# 3) change enabled to true, 4) set a bot's machine to "[[Second Computer]]".
endpoint: "${WAVE_DISPATCH_NODE2_URL:-http://127.0.0.1:11434}"
api_key: "${WAVE_DISPATCH_NODE2_API_KEY:-}"
vram_gb: 16
max_parallel_models: 1
tags: [wave-dispatch/machine]
---
# Second Computer (optional)

Off by default. Start with [[This Computer]]; when you add a second machine, a worker such as the [[Code Bot]] can
use a bigger model there while [[This Computer]] keeps the small bots. Set `machine: "[[Second Computer]]"` in that
bot's note. Back to [[AI Bots]].
