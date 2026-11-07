# Wave Dispatch

**A team of small local models that answers like one, inside Obsidian.** A tiny model splits your question into
parts, tiny yes/no gates send each part to the right specialist, Python checks the maths and the code, and a
Compiler puts the answers together. Every run leaves a note and a receipt you can recompute.

It runs on **one computer** with [Ollama](https://ollama.com): a 24 GB graphics card keeps the whole team loaded
(about 19 GB); 16 GB works with a smaller context.

You need [Ollama](https://ollama.com), [Obsidian](https://obsidian.md) and Python 3.10 or newer. (macOS comes with
Python 3.9, which is too old: get a newer one from [python.org](https://www.python.org/downloads/) or `brew install
python`. setup.sh tells you if yours is too old.)

```sh
git clone <this repo> wave-dispatch && cd wave-dispatch
# put the four tiny model files in models/ (see "The tiny models" below)
# start Ollama (open the app, or: ollama serve)
./setup.sh                          # makes a private .venv here, then runs wave_dispatch_setup.py
```
Windows (PowerShell): `py -m venv .venv`, then `.venv\Scripts\python -m pip install -r requirements.txt`, then
`.venv\Scripts\python wave_dispatch_setup.py`. The helper checks everything, installs the four tiny models into
Ollama, pulls the two worker models (llama3.1:8b and qwen2.5-coder:7b, about 10 GB) and runs a short "Hello
Dispatch". Then it checks whether Ollama keeps all the models loaded, and if not, prints the exact commands that make
it fast on your system. Then open `dispatch_vault/` in Obsidian ("Open folder as vault"), choose
**Trust author and enable plugins**, and click the wave icon in the left ribbon. Start at the note **00 - Start Here**.

---

## How a question flows

```
question
  → Splitter (wave-split)               splits it into parts
  → yes/no gates (wave-gate)            each part → the bots it needs
  → Graph Reader                        facts from the notes you linked (only if needed)
  → Math Bot · Code Bot · Writer        answer their parts at the same time
  → review loop (wave-review, Try Again Bot)
  → Compiler                            puts the answers together
  → answer + run note + receipt
```

1. **Split.** "Write is_prime(n), and how many primes are below 100?" becomes two parts. Python refuses a split that
   drops a number, a `[[link]]` or a function name; then the question goes on whole.
2. **Gates.** Every part goes through four yes/no gates ("does this part need the Math Bot?"). A part no gate takes
   goes to the Writer.
3. **Notes.** If a part links a note in your vault, the Graph Reader copies the sentences that matter, word for word
   (reworded summaries turned out to garble facts). The specialists get those and the note itself.
4. **Specialists** answer their own parts at the same time, each in its own kind of output (code, maths or text).
   Maths runs through an exact calculator, and Python recomputes every sum a bot writes. A wrong sum goes back once
   with the right number.
5. **Review loop.** A tiny model decides whether the code needs a thorough review. If it does, the Try Again Bot says
   what is wrong and the Code Bot rewrites it, up to 3 rounds.
6. **Compile.** The Compiler only puts the answers together. It never writes code: Python puts the bots' exact code
   blocks back in.

Every bot is a note in `dispatch_vault/Bots/`. Change a bot's `model`, `temperature`, a gate's question or the
Compiler's instructions there, and the next run uses the change.

## The tiny models

We trained four 0.5B models (LoRA on Qwen2.5-0.5B-Instruct, q8_0, about 0.5 GB each) for the jobs a big model is
wasted on. Their prompts live in `wave_dispatch/dispatch_core.py` exactly as they were trained: change one only
together with a retrain.

| Model | Job | Measured on held-out tests (through Ollama) |
|---|---|---|
| `wave-split` | split a message into parts | right number of parts 95.1% (the base model: 28.6%), ~0.13 s |
| `wave-gate` | yes/no: does this part need this bot? (Math, Writer and Notes gates) | 97.9% on 1,148 checks (llama3.1:8b: 84.2%), ~0.04 s |
| `wave-code-gate` | the same, retrained so "make me a game / app / website" counts as code (Code Gate) | catches 99% of 70 make/build requests (wave-gate: 87%), 100% right on 70 questions *about* games and apps |
| `wave-review` | yes/no: does this code need a thorough review? | 73.0%: the weakest link (see below) |

The weights are too big for git. Download `wave-gate-q8_0.gguf`, `wave-code-gate-q8_0.gguf`, `wave-split-q8_0.gguf` and
`wave-review-q8_0.gguf`
from the release page and put each in its folder under `models/` (`models/wave-gate/` and so on). Setup checks each
file's size and SHA-256 against `models/models.json` before it runs `ollama create`, and never installs a file that
doesn't match. `wave-split` needs `repeat_penalty 1.0` (its Modelfile sets it): with Ollama's default of 1.1 it
rewrote and over-split parts and scored 91.6%.

Why two gate models: retraining for build requests made the gate better for code but worse for maths (98.3% →
95.1%) and notes (99.7% → 93.4%), so each gate uses the model that is best for it.

## What the results show (and what they don't)

50 questions (maths, code, multi-part, questions about vault notes, explanations), each asked twice, on one 24 GB graphics
card. Maths, code and note facts are checked by Python (code against unit tests); explanations were graded
blind by another model, with all three systems' answers shuffled together. The baseline is one 30B model
(Qwen3-Coder-30B-A3B, 4-bit, on a separate machine). "Earlier Dispatcher" is the same pipeline in the multi-machine
project Wave Dispatch came from.

| | Wave Dispatch | Earlier Dispatcher | One 30B model |
|---|---|---|---|
| **All 100 answers** | **90%** | 82% | 74% |
| Maths (24) | **100%** | 83% | 75% |
| Code (28) | **100%** | 93% | 93% |
| Multi-part (16) | 88% | 75% | 88% |
| Questions about your notes (12) | 83% | 92% | 0% (it can't read them) |
| Explanations (20) | 70% | 65% | **80%** |
| Without the notes questions | **91%** | 81% | 84% |
| Median time per question | 1.8 s | 2.1 s | 2.3 s |

- **Maths and code: every answer right.** Every number comes from an exact calculator and Python rechecks every sum;
  code was checked against unit tests.
- **Explanations are the weak spot.** The Writer (llama3.1:8b) is correct but leaves out key points; the single 30B
  model explains better.
- **The two runs of each question gave the same result every time** (the earlier Dispatcher disagreed with itself
  on 6 questions).
- **The grading of explanations is noisy.** The same earlier answers scored 55% and 65% in different grading rounds.
  Treat differences of a few points as a tie.
- Speed assumes all six models stay loaded. With models swapping in and out, a question took 17–50 s.

Known weak spots:
- `wave-review` misses some bugs, and the Try Again Bot sometimes approves code that is wrong (seen in earlier runs: a
  `valid_parentheses` that failed on letters passed review). VERIFIED means Python's checks passed (every bot answered
  in full, the sums are right, the code parses, the review loop ended well). It does **not** prove the code is correct.
- The Python checks catch wrong arithmetic and broken syntax, not wrong reasoning.

## Run statuses and receipts

- **VERIFIED**: every Python check passed and the review loop was on. **ANSWERED**: review loop off, other checks pass.
- **UNVERIFIED**: a check failed (the run note says which). **FAILED**: an error stopped the run (still written down).

Every run writes `dispatch_vault/Runs/Run <id>.md` (each step, model and output) and `dispatch_vault/Logs/Receipt
<id>.md`: a SHA-256 over the question, the team's settings, each note it read and every output. Recompute one with:

```sh
python3 wave_dispatch_setup.py --verify-receipt "dispatch_vault/Logs/Receipt <id>.md"
```

**Compare with one model** (in the panel) asks a single model the same question with the same notes and writes
`Runs/Compare <id>.md` side by side, with a `winner` field for you to fill in.

## Settings

- `dispatch_config.example.yaml`: copy to `dispatch_config.yaml` to change the timeout, token limit or vault.
- `.env` (made from `.env.example` by setup): the Ollama address (`WAVE_DISPATCH_ENDPOINT`), and an optional second
  computer.
- `dispatch_vault/Machines/This Computer.md`: the Ollama settings that keep all six models loaded
  (`OLLAMA_MAX_LOADED_MODELS=6`, `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_KEEP_ALIVE=-1`).

## Safety

- Models never get tools, a shell or file access. They return text; Python decides what happens with it.
- Vault reads stay inside the vault (symlinks are not followed). Notes and receipts are written through a small Guard
  that logs every write before it happens.
- The panel runs the Python backend with an argument list (never a shell), sends the question on stdin, and shows
  model text as plain text (never rendered as HTML).
- Secrets that look like API keys are redacted before anything reaches a model or a note.

## Tests

```sh
python3 scripts/verify_wave_dispatch.py      # the engine, app, setup and panel with a fake model server
```

## How this was made

Wave Dispatch was designed by its author: the split, gates, specialists and Compiler pipeline, the bots, and the
idea of training tiny models for the routing jobs. Most of the code, the training scripts and the documentation were
written with AI coding assistants (Anthropic's Claude, and OpenAI's Codex for code reviews and training data), then
tested live on real hardware. The results above are what those tests showed, including where the team loses.

MIT License (see `LICENSE`).
