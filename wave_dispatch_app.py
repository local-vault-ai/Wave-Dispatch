#!/usr/bin/env python3
"""Wave Dispatch app backend: what the Obsidian panel (dispatch_vault/.obsidian/plugins/wave-dispatch) runs.

Every command prints ONE JSON object per line on stdout (events), so the panel can show progress live:

  python3 wave_dispatch_app.py status        is the team runnable? (machines reachable, every bot's model installed)
  python3 wave_dispatch_app.py models        installed models per enabled machine (for the panel's compare picker)
  python3 wave_dispatch_app.py run           ask the team once; the QUESTION is read from stdin (never argv or a shell)
  python3 wave_dispatch_app.py run --solo-model ID   also ask ONE model the same question with the same notes, and
                                                     write Runs/Compare <run id>.md (the team vs one model)
  run switches:  --no-notes    don't read the vault (the Graph Reader sits out)
                 --no-split    send the question through the gates whole (no Splitter)
                 --no-review   skip the review loop; a run whose other checks pass is ANSWERED (not VERIFIED)

  --vault PATH  --endpoint URL  --provider ollama|openai  --config FILE  --env-file FILE   (same as the setup helper)

Events: {"event": "step", "stage", "bot", "model", "machine"} · {"event": "result", "status", "answer", "seconds", ...}
        {"event": "solo_start" | "solo", ...} · {"event": "compare_note", "path"} · {"event": "error", "message", "hint"}
Exit code: 0 when the command ran (a run may still be UNVERIFIED: see its status), 1 on an error event.
Statuses: VERIFIED (every check + review loop) · ANSWERED (review off, other checks pass) · UNVERIFIED · FAILED.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if sys.version_info < (3, 10):          # tell the panel exactly what is wrong (it then tries the next Python)
    print(json.dumps({"event": "error", "message": f"Python 3.10+ is needed (this is {sys.version.split()[0]})",
                      "hint": "install Python 3.10 or newer, or set the Python command in Settings -> Wave Dispatch"}),
          flush=True)
    sys.exit(1)

MAX_PROMPT = 8000                       # characters read from stdin

for _stream in (sys.stdin, sys.stdout, sys.stderr):     # UTF-8 pipes on every OS (Windows ANSI code pages)
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def fail(message: str, hint: str = "") -> int:
    emit({"event": "error", "message": message, "hint": hint})
    return 1


def load(args):
    """(team, vault, dispatch_cfg, installed, problems, machines): the same loading the setup helper does, read-only."""
    from wave_dispatch import dispatch_engine as de
    from wave_dispatch import wavecore as sw
    from wave_dispatch_setup import load_config
    cfg, _ = load_config(args.config)
    dcfg = cfg.get("dispatch") or {}
    defaults = {k: dict(v or {}) for k, v in (cfg.get("machines") or {}).items()}
    overrides: dict[str, dict] = {}
    if args.endpoint or args.provider:
        o = overrides.setdefault("This Computer", {})
        if args.endpoint:
            o["endpoint"] = args.endpoint
        if args.provider:
            o["provider"] = args.provider
    env = sw.load_env(args.env_file)
    vault = (args.vault or ROOT / str(cfg.get("vault", "dispatch_vault"))).resolve()
    team = de.load_team(vault, env, defaults, overrides)
    team.validate()
    installed, problems, machines = {}, [], []
    used = {b.machine for b in team.active()}
    for name, m in sorted(team.machines.items()):
        if not m.enabled:
            continue
        row = {"name": name, "provider": m.provider, "endpoint": sw.safe_url(m.base_url), "reachable": False, "models": 0}
        try:
            installed[name] = sw.list_models(m)
            row.update(reachable=True, models=len(installed[name]))
        except sw.WaveError as e:
            if name in used:
                problems.append(f"{name} is not reachable: {sw.safe_text(e)}")
        machines.append(row)
    if not problems:
        problems += sw.resolve_models(team, {k: [x["name"] for x in v] for k, v in installed.items()})
    return team, vault, dcfg, installed, problems, machines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave Dispatch app backend (JSON lines)")
    ap.add_argument("command", choices=["status", "models", "run"])
    ap.add_argument("--json", action="store_true", help="accepted for clarity; output is always JSON lines")
    ap.add_argument("--solo-model", help="run: also ask this one model (exact installed id) and write a Compare note")
    ap.add_argument("--solo-machine", default="This Computer")
    ap.add_argument("--no-notes", action="store_true", help="run: don't read the vault (the Graph Reader sits out)")
    ap.add_argument("--no-split", action="store_true", help="run: no Splitter; the question goes through the gates whole")
    ap.add_argument("--no-review", action="store_true", help="run: skip the review loop (ANSWERED, not VERIFIED)")
    ap.add_argument("--endpoint")
    ap.add_argument("--provider", choices=["ollama", "openai"])
    ap.add_argument("--config", type=Path)
    ap.add_argument("--vault", type=Path)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = ap.parse_args(argv)
    try:
        import yaml  # noqa: F401
    except ImportError:
        return fail("pyyaml is not installed", "run ./setup.sh (or: python3 -m pip install -r requirements.txt)")
    from wave_dispatch import dispatch_engine as de
    from wave_dispatch import wavecore as sw
    try:
        team, vault, dcfg, installed, problems, machines = load(args)
    except sw.WaveError as e:
        return fail(sw.safe_text(e), "fix the vault or config (python3 wave_dispatch_setup.py --check shows details)")
    except (Exception, SystemExit) as e:                # bad YAML, unreadable config, ...: an event, never a traceback
        return fail(sw.safe_text(f"{type(e).__name__}: {e}"), "check dispatch_config.yaml and .env")
    hint = "run python3 wave_dispatch_setup.py (it installs the tiny models and pulls the others)" if problems else ""

    if args.command == "status":
        emit({"event": "status", "ready": not problems, "problems": [sw.safe_text(p) for p in problems], "hint": hint,
              "vault": vault.name, "machines": machines,
              "bots": [{"name": b.name, "duty": b.duty, "model": b.model, "send_model": b.send_model,
                        "machine": b.machine, "output": b.output, "for": b.target} for b in team.active()]})
        return 0
    if args.command == "models":
        emit({"event": "models", "machines": {k: [{"name": x["name"], "size_gb": x.get("size_gb")} for x in v]
                                               for k, v in installed.items()}})
        return 0

    # run ------------------------------------------------------------------------------------------------------
    prompt = sys.stdin.read(MAX_PROMPT + 1)
    if len(prompt) > MAX_PROMPT:
        return fail(f"the question is longer than {MAX_PROMPT} characters")
    prompt = prompt.strip()
    if not prompt:
        return fail("the question is empty")
    if problems:
        return fail("; ".join(sw.safe_text(p) for p in problems), hint)
    solo_id = None
    if args.solo_model:
        names = [x["name"] for x in installed.get(args.solo_machine, [])]
        if args.solo_model not in names:          # only an id the machine reported: never a free-form string
            return fail(f"{args.solo_model!r} is not installed on {args.solo_machine}", "pick a model from the list")
        solo_id = args.solo_model

    def step(stage, bot, m):
        emit({"event": "step", "stage": stage, "bot": bot.name, "model": bot.send_model, "machine": m.name})

    try:
        writer = sw.VaultWriter(vault)
    except sw.WaveError as e:
        return fail(sw.safe_text(e), "the Guard could not start; nothing was written")
    max_tokens = int(dcfg.get("max_tokens", 2000))
    timeout = float(dcfg.get("timeout_s", 120))
    res = de.run_dispatch(prompt, team=team, writer=writer, label="compare" if solo_id else "run",
                          max_tokens=max_tokens, timeout=timeout, graph_depth=int(dcfg.get("graph_depth", 1)),
                          on_step=step, use_notes=not args.no_notes, split=not args.no_split,
                          review=not args.no_review)
    emit({"event": "result", "run_id": res.run_id, "status": res.status, "answer": res.answer, "seconds": res.seconds,
          "checks": res.checks, "error": res.error, "run_note": res.run_note, "receipt_note": res.receipt_note,
          "guard": writer.kind, "mode": res.mode, "parts": res.parts, "routes": res.routes,
          "split_refused": res.split_refused,
          "reviews": [{"bot": r.get("bot"), "round": r.get("round"), "result": r.get("result")} for r in res.reviews]})
    if solo_id:
        emit({"event": "solo_start", "model": solo_id, "machine": args.solo_machine})
        solo = de.run_solo(prompt, res, team=team, model=solo_id, machine=args.solo_machine, max_tokens=max_tokens,
                           timeout=timeout)
        emit({"event": "solo", "model": sw.clean_model_id(solo.served_model) or solo_id, "answer": solo.answer,
              "seconds": solo.seconds, "truncated": solo.truncated, "error": solo.error})
        try:
            emit({"event": "compare_note", "path": de.write_compare_note(writer, res, solo, prompt)})
        except sw.WaveError as e:
            return fail(sw.safe_text(e), "the comparison note could not be written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
