---
type: hub
tags: [wave-dispatch/hub]
---
# 00 - Start Here

Wave Dispatch answers your questions with a team of small local models instead of one big one. A tiny model splits
the question into parts, tiny yes/no gates send each part to the right specialist, and Python checks the maths and
the code before the [[Compiler]] puts the answers together. How a run flows: [[How Wave Dispatch Works]].

## 1. Set it up (once)

1. Install [Ollama](https://ollama.com) and start it.
2. Put the four tiny model files in `models/` (see the README, "The tiny models").
3. In the Wave Dispatch folder run `./setup.sh` (Windows: `py wave_dispatch_setup.py`). It checks everything,
   installs the tiny models into Ollama, pulls the two worker models and runs a short "Hello Dispatch".
4. For speed, keep all six models loaded: see [[This Computer]].

## 2. Ask a question

Open this folder as a vault in Obsidian, choose **Trust author and enable plugins**, and click the Wave Dispatch icon
in the left ribbon. Type a question and press **Ask the team** (or Cmd/Ctrl+Enter). Try:

- `Write a Python function is_prime(n), and how many primes are there below 100?`
- `A car uses 27 litres for 342 km. How much per 100 km, and for 500 km?`
- `According to [[Example Setup]], how much VRAM is left if the two models use 9.2 GB and 6.6 GB?` (the
  [[Graph Reader]] reads that note, the [[Math Bot]] does the sum)

The panel shows each step as it runs. **Compare with one model** asks a single model the same question so you can
judge the team against it (`Runs/Compare ...` notes; fill in `winner`).

## 3. Switches

- **Use my notes**: the [[Graph Reader]] may read the notes your question `[[links]]`. Off: no vault reads.
- **Split the question**: the [[Splitter]] runs first. Off: the whole question goes through the gates.
- **Review loop**: the [[Code Checker]] and [[Try Again Bot]] check and fix code. Off: the result is ANSWERED
  instead of VERIFIED.

## 4. Read the result

- **VERIFIED**: every Python check passed (all bots answered in full, sums right, code parses, review passed).
- **ANSWERED**: review loop off; the other checks passed.
- **UNVERIFIED**: a check failed (the run note says which). **FAILED**: an error stopped the run.

Every run writes a note to [[Runs]] and a receipt to [[Logs]]. The team: [[AI Bots]].
