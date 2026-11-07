#!/usr/bin/env python3
"""Wave Dispatch setup helper: one command from clone to a verified "Hello Dispatch".

  python3 wave_dispatch_setup.py            interactive setup (asks before every change)
  python3 wave_dispatch_setup.py --check    read-only health check: writes nothing (not even __pycache__), exit 0 = READY
  python3 wave_dispatch_setup.py --yes      setup without questions (install the tiny models, pull the others, smoke test)
  python3 wave_dispatch_setup.py --smoke    run the "Hello Dispatch" smoke test
  python3 wave_dispatch_setup.py --run "PROMPT"   ask the team one question (name a [[Note]] to let it read that note)
  python3 wave_dispatch_setup.py --verify-receipt dispatch_vault/Logs/"Receipt <id>.md"   recompute a receipt (read-only)

  --map-models   point a WORKER bot whose model is missing at the best installed model (rewrites the bot note).
                 The four tiny models (wave-gate, wave-gate2, wave-split, wave-review) are never remapped: their prompts were
                 trained for them, so setup installs them from models/<name>/ (checked by sha256) instead.
  --endpoint URL / --provider ollama|openai   use another endpoint for This Computer (this invocation only)
"""
from __future__ import annotations

import sys

if "--check" in sys.argv[1:] or "--verify-receipt" in sys.argv[1:]:
    sys.dont_write_bytecode = True        # before ANY local import: read-only commands write nothing

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "\033[32mPASS\033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    OK, WARN, FAIL = "PASS", "WARN", "FAIL"
HELLO = ("Hello Dispatch: what is 12 * 7, and in one sentence, what does the Splitter do according to "
         "[[How Wave Dispatch Works]]?")
MODELS_DIR = ROOT / "models"
TINY = ("wave-gate", "wave-gate2", "wave-split", "wave-review")       # our own models: installed from models/<name>/, never pulled
CONFIG_NAMES = ("dispatch_config.yaml", "dispatch_config.example.yaml")


class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def line(self, level: str, what: str, detail: str = "") -> None:
        self.fails += level == FAIL
        self.warns += level == WARN
        print(f"  {level}  {what}" + (f"  — {detail}" if detail else ""))


def ask(question: str, args) -> bool:
    if args.check:
        return False
    if args.yes:
        print(f"  ? {question} [auto-yes]")
        return True
    try:
        return input(f"  ? {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def load_config(path: Path | None) -> tuple[dict, str]:
    import yaml
    for p in ([path] if path else [ROOT / n for n in CONFIG_NAMES]):
        if p and p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise SystemExit(f"{p.name}: must be a YAML mapping")
            return data, p.name
    return {}, "(defaults)"


def tiny_name(model: str) -> str:
    """'wave-gate:latest' -> 'wave-gate'; '' for any other model."""
    base = model.split(":", 1)[0].strip().lower()
    return base if base in TINY and model.split(":", 1)[-1] in (base, "latest") else ""


TEMPLATE = ('TEMPLATE """{{- range .Messages }}<|im_start|>{{ .Role }}\n{{ .Content }}<|im_end|>\n{{ end }}'
            '<|im_start|>assistant\n"""')
PARAMS = {    # written by setup itself (never taken from a file): exactly the settings the models were measured with
    "wave-gate": {"num_ctx": 2048, "num_predict": 2},
    "wave-gate2": {"num_ctx": 2048, "num_predict": 2},
    "wave-split": {"num_ctx": 4096, "num_predict": 400, "repeat_penalty": "1.0"},   # 1.1 (Ollama's default): 91.6%
    "wave-review": {"num_ctx": 4096, "num_predict": 2},
}


def modelfile(name: str) -> str:
    lines = [f"FROM ./{name}-q8_0.gguf", TEMPLATE, "PARAMETER temperature 0"]
    lines += [f"PARAMETER {k} {v}" for k, v in PARAMS[name].items() if k != "repeat_penalty"]
    lines += ['PARAMETER stop "<|im_end|>"', 'PARAMETER stop "<|endoftext|>"']
    lines += [f"PARAMETER repeat_penalty {PARAMS[name]['repeat_penalty']}"] if "repeat_penalty" in PARAMS[name] else []
    return "\n".join(lines) + "\n"


def tiny_files(name: str, models_dir: Path = MODELS_DIR) -> tuple[Path | None, str]:
    """(path to the .gguf, "") when models/<name>/<name>-q8_0.gguf is a real file with the size + sha256 written in
    models.json; (None, why) otherwise. The file name is FIXED (models.json can't point anywhere else) and no folder on
    the way may be a symlink (Astra security review 2026-11-06). Hashing reads the file (~0.5 GB) once."""
    want = f"{name}-q8_0.gguf"
    try:
        meta = json.loads((models_dir / "models.json").read_text(encoding="utf-8"))[name]
    except (OSError, ValueError, KeyError):
        return None, f"models/models.json has no entry for {name}"
    if name not in PARAMS or meta.get("file") != want:
        return None, f"models/models.json: {name} must name the file {want}"
    folder, gguf = models_dir / name, models_dir / name / want
    if models_dir.is_symlink() or folder.is_symlink() or gguf.is_symlink() or not gguf.is_file():
        return None, (f"models/{name}/{want} is missing: download it from the release page (see README, "
                      "'The tiny models') and put it in that folder")
    if gguf.stat().st_size != meta.get("bytes"):
        return None, f"models/{name}/{want} has the wrong size (an unfinished download?)"
    if _sha256_copy(gguf, None) != meta.get("sha256"):
        return None, f"models/{name}/{want}: sha256 does not match models.json (a damaged or different file)"
    return gguf, ""


def _sha256_copy(src: Path, dst: Path | None) -> str:
    """sha256 of src, written to dst at the same time (so what was checked is exactly what gets installed)."""
    h = hashlib.sha256()
    with src.open("rb") as f, (dst.open("xb") if dst else open(os.devnull, "wb")) as out:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def install_tiny(name: str, base_url: str, models_dir: Path = MODELS_DIR) -> tuple[bool, str]:
    """Copy the .gguf into a private temporary folder while hashing it, write the Modelfile there, then
    `ollama create <name>` from that folder against that machine's Ollama (an argument list, no shell)."""
    gguf, why = tiny_files(name, models_dir)
    if not gguf:
        return False, why
    meta = json.loads((models_dir / "models.json").read_text(encoding="utf-8"))[name]
    with tempfile.TemporaryDirectory(prefix="wave-dispatch-model-") as td:
        copy = Path(td) / gguf.name
        if _sha256_copy(gguf, copy) != meta["sha256"]:
            return False, f"models/{name}/{gguf.name} changed while it was being installed"
        (Path(td) / "Modelfile").write_text(modelfile(name), encoding="utf-8")
        p = subprocess.run(["ollama", "create", name, "-f", "Modelfile"], cwd=td, capture_output=True, text=True,
                           env={**os.environ, "OLLAMA_HOST": base_url}, timeout=900)
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", p.stderr or p.stdout or "")    # the CLI's terminal codes
    tail = text.strip().splitlines()[-1:] or [""]
    return p.returncode == 0, tail[0][:200]


OLLAMA_ENV = (("OLLAMA_MAX_LOADED_MODELS", "6"), ("OLLAMA_CONTEXT_LENGTH", "16384"), ("OLLAMA_KEEP_ALIVE", "-1"))


def ollama_commands(platform: str = sys.platform) -> list[str]:
    """The exact steps that keep the whole team loaded, for the computer Ollama runs on."""
    if platform == "win32":
        return [f"setx {k} {v}" for k, v in OLLAMA_ENV] + ["then quit Ollama (tray icon -> Quit) and start it again"]
    if platform == "darwin":
        return ([f"launchctl setenv {k} {v}" for k, v in OLLAMA_ENV]
                + ["then quit Ollama (menu bar icon -> Quit Ollama) and open it again",
                   "(launchctl settings last until a reboot: run the three lines again after restarting the Mac)"])
    return (["sudo systemctl edit ollama.service      and add these lines:", "[Service]"]
            + [f'Environment="{k}={v}"' for k, v in OLLAMA_ENV]
            + ["then: sudo systemctl daemon-reload && sudo systemctl restart ollama",
               "(Ollama started by hand instead: export the three variables before `ollama serve`)"])


def loaded_check(sw, m, used: list[str], now_year: int) -> tuple[bool, str]:
    """After a run: are the run's models all still loaded, and set to stay loaded? (ok, detail). Ollama only."""
    try:
        data = sw._http("GET", sw._api_root(m) + "/api/ps", None, 5, m.api_key)
    except sw.WaveError as e:
        return True, f"could not ask Ollama what is loaded ({sw.safe_text(e)})"
    rows = [x for x in (data.get("models") or []) if isinstance(x, dict)]
    loaded = {str(x.get("name") or x.get("model") or "") for x in rows}
    gone = sorted(u for u in set(used) if u not in loaded)
    short = sorted(str(x.get("name")) for x in rows if str(x.get("name")) in used
                   and str(x.get("expires_at") or "")[:4].isdigit() and int(str(x["expires_at"])[:4]) <= now_year + 1)
    if gone:
        return False, (f"{len(gone)} of the {len(set(used))} models this run used were already unloaded ({', '.join(gone)}): "
                       "every question will wait for models to swap in (about 17-50 s instead of 1-6 s)")
    if short:
        return False, (f"{', '.join(short)} will be unloaded after a few idle minutes: the next question after a "
                       "pause waits for them to load again")
    return True, f"all {len(set(used))} models of this run stay loaded"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wave Dispatch setup helper")
    ap.add_argument("--check", action="store_true", help="read-only health check")
    ap.add_argument("--yes", action="store_true", help="answer yes to every question")
    ap.add_argument("--smoke", action="store_true", help="run the Hello Dispatch smoke test")
    ap.add_argument("--run", metavar="PROMPT", help="ask the team one question")
    ap.add_argument("--map-models", action="store_true", help="map worker bots to installed models instead of pulling")
    ap.add_argument("--endpoint", help="override This Computer's endpoint")
    ap.add_argument("--provider", choices=["ollama", "openai"], help="override This Computer's provider")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--vault", type=Path)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env", help="where .env lives (default: next to this file)")
    ap.add_argument("--verify-receipt", type=Path, metavar="RECEIPT.md",
                    help="recompute a receipt's sha256 from its stored manifest and the vault on disk (read-only)")
    args = ap.parse_args(argv)
    if sys.version_info < (3, 10):
        print(f"Python 3.10+ is needed (this is {sys.version.split()[0]})")
        return 1
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("pyyaml is missing: pip install -r requirements.txt   (or: ./setup.sh)")
        return 1
    from wave_dispatch import dispatch_engine as de
    from wave_dispatch import wavecore as sw

    cfg, cfg_name = load_config(args.config)
    vault = (args.vault or ROOT / str(cfg.get("vault", "dispatch_vault"))).resolve()
    if args.verify_receipt:
        v = sw.verify_receipt(args.verify_receipt.resolve(), vault, min_version=de.MANIFEST_VERSION)
        print(f"hash {'OK' if v['hash_ok'] else 'MISMATCH'} · context notes {'unchanged' if v['notes_ok'] else 'CHANGED'}"
              + f" · final answer {'matches the run note' if v.get('answer_ok', True) else 'DIFFERS from the run note'}"
              + (f" ({', '.join(v['changed'])})" if v["changed"] else "") + (f" · {v['error']}" if v["error"] else ""))
        return 0 if v["hash_ok"] and v["notes_ok"] and v.get("answer_ok", True) else 1
    if args.check and (args.run or args.smoke or args.map_models or args.yes):
        print("--check is read-only: it can't be combined with --run / --smoke / --map-models / --yes")
        return 2
    r = Report()
    print("\nWave Dispatch setup helper" + ("  (read-only check)" if args.check else ""))

    # 1. Python + dependencies -------------------------------------------------------------------------
    print("\n[1/6] Python and dependencies")
    r.line(OK, "Python", sys.version.split()[0])
    r.line(OK, "pyyaml")
    r.line(OK, "config", cfg_name)
    dcfg = cfg.get("dispatch") or {}
    defaults = {k: dict(v or {}) for k, v in (cfg.get("machines") or {}).items()}
    overrides: dict[str, dict] = {}
    if args.endpoint or args.provider:
        o = overrides.setdefault("This Computer", {})
        if args.endpoint:
            o["endpoint"] = args.endpoint
        if args.provider:
            o["provider"] = args.provider

    # 2. .env --------------------------------------------------------------------------------------------
    print("\n[2/6] Environment file")
    env_file, example = args.env_file, ROOT / ".env.example"
    if env_file.exists():
        r.line(OK, ".env present")
    elif not example.exists():
        r.line(WARN, ".env.example missing", "defaults are used")
    elif args.check:
        r.line(WARN, ".env not created yet", "setup copies .env.example -> .env (defaults work without it)")
    else:
        shutil.copyfile(example, env_file)
        os.chmod(env_file, 0o600)
        r.line(OK, "copied .env.example -> .env", "edit it to change the endpoint or link another computer")
    env = sw.load_env(env_file)

    # 3. Vault ------------------------------------------------------------------------------------------
    print("\n[3/6] Starter vault")
    try:
        team = de.load_team(vault, env, defaults, overrides)
        team.validate()
    except sw.WaveError as e:
        r.line(FAIL, "vault / team is not runnable", sw.safe_text(e))
        print("\nNOT READY: fix the vault first.")
        return 1
    for b in team.bots:
        extra = (f" · for [[{b.target}]]" if b.target else "") + (f" · output {b.output}" if b.output else "")
        r.line(OK if b.active else WARN, f"bot [[{b.name}]]" + ("" if b.active else " (inactive)"),
               f"{b.duty} · {b.model} · on [[{b.machine}]]{extra}")
    g = sw.graph_report(vault)
    prompt_text = " ".join(b.gate_question + " " + " ".join(b.gate_examples) for b in team.bots)
    g["broken"] = [(n, t) for n, t in g["broken"] if f"[[{t}]]" not in prompt_text]   # a gate's example text, not a link
    r.line(OK if not g["broken"] else FAIL, f"{g['links']} wikilinks, {len(g['broken'])} broken",
           "; ".join(f"{n} -> [[{t}]]" for n, t in g["broken"][:5]))
    r.line(OK if not g["ambiguous"] else FAIL, f"{len(g['ambiguous'])} ambiguous links",
           "; ".join(f"{n} -> [[{t}]] (use the full path)" for n, t in g["ambiguous"][:5]))
    r.line(OK if not g["orphans"] else WARN, f"{g['notes']} notes, {len(g['orphans'])} orphans", ", ".join(g["orphans"][:5]))

    # 4. Endpoints --------------------------------------------------------------------------------------
    print("\n[4/6] Machines (endpoints)")
    installed: dict[str, list[dict]] = {}
    used = {b.machine for b in team.active()}
    for name, m in sorted(team.machines.items()):
        if not m.enabled:
            r.line(OK, f"[[{name}]] is disabled", "optional; see its note in Machines/")
            continue
        if name not in used:                      # never contact a machine no active bot uses (security review)
            r.line(OK, f"[[{name}]] is enabled but no bot uses it", "not contacted")
            continue
        try:
            installed[name] = sw.list_models(m)
            r.line(OK, f"[[{name}]] reachable", f"{m.provider} at {sw.safe_url(m.base_url)} · {len(installed[name])} models")
        except sw.WaveError as e:
            if m.provider != "ollama":
                hint = "start your OpenAI-compatible server or fix its endpoint"
            elif not shutil.which("ollama"):
                hint = "Ollama doesn't seem to be installed: get it from https://ollama.com, open it, then run setup again"
            else:
                hint = "start Ollama (open the Ollama app, or run `ollama serve`), or set WAVE_DISPATCH_ENDPOINT in .env"
            r.line(FAIL if name in used else WARN, f"[[{name}]] not reachable", f"{sw.safe_text(e)}. {hint}")
    has_cli = bool(shutil.which("ollama"))
    if not has_cli and installed:
        r.line(WARN, "ollama CLI not found", "install it from https://ollama.com to install and pull models")
    unreachable = sorted({b.machine for b in team.active()} - set(installed))

    # 5. Models -----------------------------------------------------------------------------------------
    print("\n[5/6] Models")
    changed = False
    for mname, models in installed.items():
        m = team.machines[mname]
        names = [x["name"] for x in models]
        sizes = {x["name"]: x.get("size_gb") or 0.0 for x in models}
        sub = de.Team(team.vault, [b for b in team.active() if b.machine == mname], team.machines)
        done_tiny: set[str] = set()
        pulled: dict[str, bool] = {}
        plan = sw.plan_models(sub, names)
        for p in plan:
            tiny = tiny_name(p["model"])
            if p["action"] == "ok":
                r.line(OK, f"[[{p['bot']}]] {p['model']}",
                       "installed" + ("" if p["use"] == p["model"] else f" as {p['use']}"))
                continue
            if tiny:                                       # our own model: install it from models/<name>/
                if tiny in done_tiny:
                    continue
                done_tiny.add(tiny)
                users = ", ".join(f"[[{b.name}]]" for b in sub.active() if tiny_name(b.model) == tiny)
                if m.provider != "ollama":
                    r.line(FAIL, f"{tiny} not on [[{mname}]] ({users})",
                           f"load models/{tiny}/*.gguf in that server under the name {tiny} (setup installs into Ollama only)")
                    continue
                if args.check:
                    folder, why = tiny_files(tiny)
                    r.line(FAIL, f"{tiny} not installed on [[{mname}]] ({users})",
                           "setup can install it from models/" + tiny if folder else why)
                    continue
                if not has_cli:
                    r.line(FAIL, f"{tiny} not installed on [[{mname}]]", "install the ollama CLI, then rerun setup")
                    continue
                if ask(f"install {tiny} (our own 0.5B model, from models/{tiny}/) on [[{mname}]]?", args):
                    ok, detail = install_tiny(tiny, m.base_url)
                    r.line(OK if ok else FAIL, f"installed {tiny}" if ok else f"install {tiny} failed", detail)
                    changed |= ok
                else:
                    r.line(FAIL, f"{tiny} not installed on [[{mname}]] ({users})")
                continue
            if args.check:
                hint = "setup can pull it" if m.provider == "ollama" else "download it on that server"
                if p["use"]:
                    hint += f", or run setup with --map-models to use installed {p['use']}"
                r.line(FAIL, f"[[{p['bot']}]] {p['model']} not installed on [[{mname}]]", hint)
                continue
            if p["model"] in pulled:                        # one question per model, not per bot
                if pulled[p["model"]]:
                    continue
            elif m.provider == "ollama" and not args.map_models and has_cli:
                users = ", ".join(f"[[{q['bot']}]]" for q in plan if q["model"] == p["model"])
                pulled[p["model"]] = False
                if ask(f"pull {p['model']} (for {users}) on [[{mname}]]?", args):
                    ok = subprocess.run(["ollama", "pull", p["model"]], env={**os.environ, "OLLAMA_HOST": m.base_url}).returncode == 0
                    r.line(OK if ok else WARN, f"pulled {p['model']}" if ok else f"pull {p['model']} failed")
                    pulled[p["model"]] = ok
                    changed |= ok
                    if ok:
                        continue
            usable = p["use"] and not tiny_name(p["use"])
            if usable and (args.map_models or ask(f"map [[{p['bot']}]] to installed {p['use']} instead?", args)):
                sw.set_bot_model(team, sw.VaultWriter(vault), p["bot"], p["use"])
                r.line(OK, f"[[{p['bot']}]] now uses {p['use']}", f"was {p['model']} (kept as starter_model)")
                changed = True
        need = sum(sizes.get(x, 0.0) for x in {sw.find_installed(b.model, names) or "" for b in sub.active()})
        if need:
            r.line(OK if need <= m.vram_gb * 0.9 else WARN, f"memory on [[{mname}]]",
                   f"~{need:.1f} GB for all of the team's models loaded at once, of {m.vram_gb:g} GB "
                   "(fewer fit = models swap in and out: slower, still correct)")
    if changed:                                    # re-read the notes and the machines after installs / remaps
        team = de.load_team(vault, env, defaults, overrides)
        for mname in list(installed):
            try:
                installed[mname] = sw.list_models(team.machines[mname])
            except sw.WaveError as e:
                r.line(FAIL, f"[[{mname}]] stopped answering", sw.safe_text(e))
    if unreachable:
        r.line(FAIL, "models not checked", "first make " + ", ".join(f"[[{n}]]" for n in unreachable)
               + " reachable (step 4), then run setup again")
    problems = sw.resolve_models(team, {k: [x["name"] for x in v] for k, v in installed.items()})
    problems = [p for p in problems if not any(f"on [[{n}]]" in p for n in unreachable)]
    if not args.check:
        for p in problems:
            r.line(FAIL, p, "install or pull it, or rerun with --map-models (worker bots only)")

    # 6. Smoke test / run -------------------------------------------------------------------------------
    print("\n[6/6] Hello Dispatch")
    prompt = args.run or HELLO
    if args.check:
        r.line(OK, "skipped in --check", "run `python3 wave_dispatch_setup.py --smoke` to execute it")
    elif r.fails:
        r.line(FAIL, "not run: fix the FAIL lines above first")
    elif not (args.run or args.smoke or ask("run the Hello Dispatch smoke test?", args)):
        r.line(WARN, "smoke test not run")
    else:
        def step(stage, bot, m):
            print(f"    {stage:<8} [[{bot.name}]] · {bot.send_model} on [[{m.name}]] ...", flush=True)
        writer = sw.VaultWriter(vault)
        res = de.run_dispatch(prompt, team=team, writer=writer, label="run" if args.run else "hello-dispatch",
                              max_tokens=int(dcfg.get("max_tokens", 2000)), timeout=float(dcfg.get("timeout_s", 120)),
                              graph_depth=int(dcfg.get("graph_depth", 1)), on_step=step)
        level = OK if res.status == "VERIFIED" else FAIL       # the smoke test passes only when VERIFIED
        bad = [k for k, v in res.checks.items() if not v]
        r.line(level, f"{res.status} in {res.seconds:.1f} s",
               res.error or (("failed checks: " + ", ".join(bad)) if bad else "all checks passed"))
        if res.receipt_note:
            r.line(OK, "receipt", f"{vault.name}/{res.receipt_note}  (guard: {writer.kind})")
            r.line(OK, "run note", f"{vault.name}/{res.run_note}")
        if res.answer:
            print("\n  Final answer:\n    " + res.answer.replace("\n", "\n    "))
        print("\n  Speed check (Ollama)")
        for mname in sorted({st["machine"] for st in res.steps}):
            m = team.machines[mname]
            if m.provider != "ollama":
                continue
            used = [st["runtime_model"] for st in res.steps if st["machine"] == mname]
            fast, detail = loaded_check(sw, m, used, datetime.now().year)
            r.line(OK if fast else WARN, f"[[{mname}]] keeps the team loaded" if fast
                   else f"[[{mname}]] does not keep the team loaded", detail)
            if not fast:
                local = sw.safe_url(m.base_url).split("//")[-1].split(":")[0] in ("127.0.0.1", "localhost")
                print("        Fix it " + ("on this computer" if local else f"on the computer [[{mname}]] points at")
                      + " (the team still works without it, just slower):")
                lines = ollama_commands() if local else (
                    [f"{k}={v}" for k, v in OLLAMA_ENV] + ["set these three variables for Ollama on that computer, "
                                                           "then restart Ollama there (Windows: setx; macOS: launchctl setenv)"])
                for line in lines:
                    print("          " + line)

    print(f"\n{'READY' if not r.fails else 'NOT READY'}: {r.fails} failed, {r.warns} warnings.")
    if not r.fails:
        print(f"Next: open {vault.name}/ in Obsidian ('Open folder as vault') and click the Wave Dispatch icon.")
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main())
