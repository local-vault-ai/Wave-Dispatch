"""Suite 22: the standalone Wave Dispatch engine (wave_dispatch/dispatch_engine.py) with a fake model server.

  uv run scripts/verify_wave_dispatch.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wave_dispatch import dispatch_core as dc, dispatch_engine as de, wavecore as sw  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except Exception as e:                       # noqa: BLE001
            RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        return fn
    return wrap


def vault_copy() -> Path:
    d = Path(tempfile.mkdtemp(prefix="wd_test_"))
    shutil.copytree(ROOT / "dispatch_vault", d / "vault")
    return d / "vault"


def team_for(vault: Path) -> de.Team:
    t = de.load_team(vault, {}, {}, {})
    for b in t.bots:
        b.runtime_model = b.model
    return t


class Fake:
    """chat_fn: replies by bot (the system prompt names the bot or its job) and records every call."""

    def __init__(self, script: dict):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list]] = []

    def who(self, msgs) -> str:
        sysm = msgs[0]["content"]
        if sysm.startswith("You split a user's"):
            return "Splitter"
        m = re.match(r'You are "([^"]+)"', sysm) or re.match(r"You are the ([\w ]+?)[.,]", sysm)
        return m.group(1) if m else "?"

    def __call__(self, machine, model, msgs, **kw):
        name = self.who(msgs)
        self.calls.append((name, msgs))
        q = self.script.get(name) or ["ok"]
        r = q.pop(0) if len(q) > 1 else q[0]
        text = r(msgs) if callable(r) else r
        return sw.Reply(text, False, model)


def gate_on(test):
    """A gate reply that says yes when test(part) is true; the part is inside ("...")."""
    return lambda msgs: "yes" if test(re.search(r'\("(.*)"\)', msgs[-1]["content"]).group(1)) else "no"


GATES = lambda code=lambda p: "function" in p, math=lambda p: "How many" in p, writer=lambda p: False, notes=lambda p: "[[" in p: {
    "Gate Bot Code": [gate_on(code)], "Gate Bot Math": [gate_on(math)], "Gate Bot Writer": [gate_on(writer)],
    "Gate Bot Graph": [gate_on(notes)]}
MSG = "Write a Python function is_prime(n), and tell me how many prime numbers there are below 100."
SPLIT = "1. Write a Python function is_prime(n).\n2. How many prime numbers are there below 100?"
CODE = "```python\ndef is_prime(n):\n    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))\n```"


@check("D1 the starter vault loads and validates: 12 bots, 4 gates with questions, review gate -> Try Again Coding Bot")
def _():
    t = team_for(vault_copy())
    t.validate()
    assert len(t.bots) == 12 and len(t.duty("gate")) == 4 and t.duty("review")[0].reviewer == "Try Again Coding Bot"
    assert {g.target for g in t.duty("gate")} == {"Math Bot", "Code Bot", "Writer", "Graph Reader"}
    assert all("()" in g.gate_question and g.gate_examples for g in t.duty("gate"))


@check("D2 split -> every part through every gate -> each bot gets ONLY its part -> Compiler with [CODE n] markers")
def _():
    t = team_for(vault_copy())
    f = Fake({"Splitter": [SPLIT], **GATES(), "Code Checker": ["no"], "Code Bot": [CODE],
              "Math Bot": ["There are 25 primes below 100."], "Compiler": ["[CODE 1]\n\nThere are 25 primes below 100."]})
    r = de.run_dispatch(MSG, team=t, chat_fn=f)
    assert r.parts == ["Write a Python function is_prime(n).", "How many prime numbers are there below 100?"], r.parts
    assert r.routes == {"1": ["Code Bot"], "2": ["Math Bot"]}, r.routes
    assert [n for n, _ in f.calls].count("Gate Bot Code") == 2 and [n for n, _ in f.calls].count("Writer") == 0
    assert [m for n, m in f.calls if n == "Splitter"][0] == dc.split_prompt(MSG)          # the trained prompt
    mq = [m for n, m in f.calls if n == "Math Bot"][0][-1]["content"]
    assert "How many prime numbers" in mq and "Write a Python" not in mq, mq                # only its part
    g = [m for n, m in f.calls if n == "Gate Bot Code"][0]
    assert g == dc.gate_prompt("Gate Bot Code", "Wave Dispatch", t.by_name("Gate Bot Code").instructions,
                               t.by_name("Gate Bot Code").gate_question, t.by_name("Gate Bot Code").gate_examples,
                               "Write a Python function is_prime(n).")[1]
    comp = [m for n, m in f.calls if n == "Compiler"][0][-1]["content"]
    assert "[CODE 1]" in comp and "def is_prime" not in comp and "(1) Write a Python function" in comp, comp
    assert r.answer.startswith(CODE) and r.answer.endswith("There are 25 primes below 100."), r.answer
    assert r.status == "VERIFIED" and r.checks["review_passed"], (r.status, r.checks)


@check("D3 a split that loses a number is refused: the question goes through the gates whole")
def _():
    t = team_for(vault_copy())
    f = Fake({"Splitter": ["1. Write a Python function is_prime(n).\n2. How many primes?"], **GATES(),
              "Code Checker": ["no"], "Code Bot": [CODE], "Compiler": ["[CODE 1]"]})
    r = de.run_dispatch(MSG, team=t, chat_fn=f)
    assert r.split_refused == "lost 100" and r.parts == [] and [n for n, _ in f.calls].count("Gate Bot Code") == 1


@check("D4 a part no gate takes goes to the Writer; a lone answer is the final answer (no Compiler call)")
def _():
    t = team_for(vault_copy())
    f = Fake({"Splitter": ["1. Hey, how's it going?"], **GATES(), "Writer": ["Going well, thanks!"]})
    r = de.run_dispatch("Hey, how's it going?", team=t, chat_fn=f)
    assert r.routes == {"1": ["Writer"]} and r.answer == "Going well, thanks!" and "Compiler" not in [n for n, _ in f.calls]


@check("D5 output kinds: the Math Bot writing code gets one retry; the calculator runs; a wrong sum goes back once")
def _():
    t = team_for(vault_copy())
    f = Fake({"Splitter": ["1. What is 18% of 3,750?"], **GATES(math=lambda p: "%" in p),
              "Math Bot": ["```python\nprint(0.18*3750)\n```", "```calc\n0.18 * 3750\n```", "18% of 3,750 is 675."]})
    r = de.run_dispatch("What is 18% of 3,750?", team=t, chat_fn=f)
    stages = [s["wave"] for s in r.steps if s["bot"] == "Math Bot"]
    assert stages == ["work", "recheck", "calc"], stages
    assert r.answer == "18% of 3,750 is 675." and r.checks["sums_correct"], r.answer
    f2 = Fake({"Splitter": ["1. What is 14 + 22 + 37 + 41 + 56?"], **GATES(math=lambda p: "+" in p),
               "Math Bot": [r"\( 14 + 22 + 37 + 41 + 56 = 160 \)", "14 + 22 + 37 + 41 + 56 = 170"]})
    r2 = de.run_dispatch("What is 14 + 22 + 37 + 41 + 56?", team=t, chat_fn=f2)
    fix = [m for n, m in f2.calls if n == "Math Bot"][-1][-1]["content"]
    assert "is 170, not 160" in fix and r2.answer.endswith("= 170") and r2.status == "VERIFIED", (fix, r2.answer)


@check("D6 review loop: yes -> Try Again Coding Bot -> Code Bot rewrites -> checked again; stops at max_rounds; OK stops it")
def _():
    t = team_for(vault_copy())
    bad, good = "```python\ndef is_even(n):\n    return n % 2 == 1\n```", "```python\ndef is_even(n):\n    return n % 2 == 0\n```"
    q = "Write a Python function is_even(n)."
    f = Fake({"Splitter": ["1. " + q], **GATES(code=lambda p: "function" in p), "Code Checker": ["yes", "no"],
              "Try Again Coding Bot": ["It returns True for odd numbers: use == 0."], "Code Bot": [bad, good]})
    r = de.run_dispatch(q, team=t, chat_fn=f)
    assert [(x["round"], x["result"]) for x in r.reviews] == [(1, "fixed"), (2, "good")] and r.answer == good, r.reviews
    assert [m for n, m in f.calls if n == "Code Checker"][0] == dc.review_gate_prompt("Code Checker", "Wave Dispatch", q, bad)
    f2 = Fake({"Splitter": ["1. " + q], **GATES(code=lambda p: "function" in p), "Code Checker": ["yes"],
               "Try Again Coding Bot": ["Use == 0."], "Code Bot": [bad]})
    r2 = de.run_dispatch(q, team=t, chat_fn=f2)
    assert len(r2.reviews) == 3 and r2.status == "UNVERIFIED" and not r2.checks["review_passed"], r2.reviews
    f3 = Fake({"Splitter": ["1. " + q], **GATES(code=lambda p: "function" in p), "Code Checker": ["yes"],
               "Try Again Coding Bot": ["OK"], "Code Bot": [good]})
    r3 = de.run_dispatch(q, team=t, chat_fn=f3)
    assert [x["result"] for x in r3.reviews] == ["reviewer ok"] and r3.status == "VERIFIED"
    r4 = de.run_dispatch(q, team=t, chat_fn=Fake({"Splitter": ["1. " + q], **GATES(), "Code Bot": [bad]}), review=False)
    assert r4.reviews == [] and r4.status == "ANSWERED"


@check("D7 notes: the Graph Reader reads the [[linked]] note only when its gate says yes; its facts reach the bots")
def _():
    t = team_for(vault_copy())
    q = "According to the [[Example Setup]] note, how much VRAM does the PC have?"
    f = Fake({"Splitter": ["1. " + q], **GATES(math=lambda p: False, writer=lambda p: False),
              "Graph Reader": ["The example PC has a 24 GB graphics card."]})
    r = de.run_dispatch(q, team=t, chat_fn=f)
    gq = [m for n, m in f.calls if n == "Graph Reader"][0][-1]["content"]
    assert "24 GB graphics card" in gq and r.checks["graph_context_found"] and r.context[0][0] == "Knowledge/Example Setup.md"
    assert r.routes == {"1": ["Graph Reader", "Writer"]} or r.routes == {"1": ["Graph Reader"]}, r.routes
    cq = [m for n, m in f.calls if n in ("Compiler", "Writer")]
    assert cq and "24 GB graphics card" in cq[0][-1]["content"], "the facts reach whoever writes the answer"
    r2 = de.run_dispatch(q, team=t, chat_fn=Fake({"Splitter": ["1. " + q], **GATES(notes=lambda p: True)}), use_notes=False)
    assert r2.context is None and all(s["bot"] != "Graph Reader" for s in r2.steps)


@check("D8 receipts: a run writes a run note + receipt through the Guard writer; --verify-receipt recomputes the hash")
def _():
    vault = vault_copy()
    t = team_for(vault)
    state = Path(tempfile.mkdtemp(prefix="wd_state_"))
    writer = sw.VaultWriter(vault, state, portable=True)
    f = Fake({"Splitter": [SPLIT], **GATES(), "Code Checker": ["no"], "Code Bot": [CODE],
              "Math Bot": ["There are 25 primes below 100."], "Compiler": ["[CODE 1]\n\n25 primes."]})
    r = de.run_dispatch(MSG, team=t, chat_fn=f, writer=writer)
    assert (vault / r.run_note).exists() and (vault / r.receipt_note).exists()
    v = sw.verify_receipt(vault / r.receipt_note, vault)
    assert v["hash_ok"] and v["notes_ok"] and not v["error"], v
    run = (vault / r.run_note).read_text()
    assert "## Parts and bots" in run and "[[Bots/Math Bot|Math Bot]]" in run and "wave-dispatch/run" in run


@check("D9 bad team notes are refused with a clear message")
def _():
    vault = vault_copy()
    p = vault / "Bots/Gate Bot Math.md"
    p.write_text(p.read_text().replace('for: "[[Math Bot]]"', 'for: "[[Nobody]]"'))
    try:
        de.load_team(vault, {}, {}, {}).validate()
        raise AssertionError("not refused")
    except sw.WaveError as e:
        assert "Gate Bot Math" in str(e) and "Nobody" in str(e), e


@check("D10 any model failure ends as FAILED with a receipt, never a crash")
def _():
    t = team_for(vault_copy())
    def boom(*a, **k):
        raise sw.WaveError("machine not reachable")
    r = de.run_dispatch(MSG, team=t, chat_fn=boom)
    assert r.status == "FAILED" and "not reachable" in r.error and r.digest


# --------------------------------------------------------------------------- app, setup, panel (release pieces)

import contextlib  # noqa: E402
import functools  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import shutil as _sh  # noqa: E402
import subprocess  # noqa: E402

import wave_dispatch_app as app  # noqa: E402
import wave_dispatch_setup as setup  # noqa: E402

INSTALLED = ["wave-split:latest", "wave-gate:latest", "wave-gate2:latest", "wave-review:latest", "llama3.1:8b",
             "qwen2.5-coder:7b"]


def _models(names):
    return lambda m, timeout=3: [{"name": n, "size_gb": 0.5} for n in names]


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def run_app(argv, stdin="", names=INSTALLED, fake=None):
    os.environ["WAVE_DISPATCH_STATE"] = tempfile.mkdtemp(prefix="wd_app_state_")
    out = io.StringIO()
    fake = fake or Fake({"Splitter": [SPLIT], **GATES(), "Code Checker": ["no"], "Code Bot": [CODE],
                         "Math Bot": ["There are 25 primes below 100."], "Compiler": ["[CODE 1]\n\n25 primes."]})
    with patched(sw, "list_models", _models(names)), \
            patched(de, "run_dispatch", functools.partial(de.run_dispatch, chat_fn=fake)), \
            patched(de, "run_solo", functools.partial(de.run_solo, chat_fn=lambda *a, **k: sw.Reply("solo answer", False, a[1]))), \
            patched(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
        code = app.main(argv)
    return code, [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


@check("D11 app: status, run (steps, parts, routes, VERIFIED) and compare mode write a run note, receipt and Compare note")
def _():
    vault = vault_copy()
    code, ev = run_app(["status", "--vault", str(vault)])
    assert code == 0 and ev[0]["event"] == "status" and ev[0]["ready"] and len(ev[0]["bots"]) == 12, ev
    code, ev = run_app(["run", "--vault", str(vault), "--solo-model", "llama3.1:8b"], stdin=MSG)
    assert code == 0, ev
    kinds = [e["event"] for e in ev]
    assert kinds[0] == "step" and {"result", "solo_start", "solo", "compare_note"} <= set(kinds), kinds
    res = next(e for e in ev if e["event"] == "result")
    assert res["status"] == "VERIFIED" and len(res["parts"]) == 2 and res["routes"]["2"] == ["Math Bot"], res
    assert {e["stage"] for e in ev if e["event"] == "step"} >= {"split", "gate", "work", "compile"}
    for k in ("run_note", "receipt_note"):
        assert (vault / res[k]).exists()
    cmp_note = (vault / next(e for e in ev if e["event"] == "compare_note")["path"]).read_text()
    assert "solo answer" in cmp_note and "wave-dispatch/compare" in cmp_note and "winner: ''" in cmp_note
    v = sw.verify_receipt(vault / res["receipt_note"], vault)
    assert v["hash_ok"] and v["notes_ok"], v


@check("D12 app refuses: empty question, an uninstalled compare model, a missing tiny model (error event, nothing run)")
def _():
    vault = vault_copy()
    code, ev = run_app(["run", "--vault", str(vault)], stdin="   ")
    assert code == 1 and ev[-1]["event"] == "error" and "empty" in ev[-1]["message"]
    code, ev = run_app(["run", "--vault", str(vault), "--solo-model", "evil; rm -rf"], stdin=MSG)
    assert code == 1 and "not installed" in ev[-1]["message"] and not any(e["event"] == "step" for e in ev)
    code, ev = run_app(["run", "--vault", str(vault)], stdin=MSG, names=[n for n in INSTALLED if "split" not in n])
    assert code == 1 and "wave-split" in ev[-1]["message"] and "wave_dispatch_setup.py" in ev[-1]["hint"], ev
    code, ev = run_app(["run", "--vault", str(vault), "--no-split", "--no-review", "--no-notes"], stdin=MSG)
    res = next(e for e in ev if e["event"] == "result")
    assert res["mode"] == {"use_notes": False, "split": False, "review": False} and res["status"] == "ANSWERED", res
    assert not any(e.get("bot") in ("Splitter", "Code Checker") for e in ev if e["event"] == "step")


@check("D13 setup: tiny models only from models/<name>/<name>-q8_0.gguf, checked by size + sha256, installed from a hashed copy")
def _():
    assert setup.tiny_name("wave-gate:latest") == "wave-gate" and setup.tiny_name("wave-split") == "wave-split"
    assert setup.tiny_name("wave-gate:old") == "" and setup.tiny_name("llama3.1:8b") == ""
    d = Path(tempfile.mkdtemp(prefix="wd_models_"))
    (d / "wave-gate").mkdir()
    blob = b"GGUF" + b"\0" * 60
    g = d / "wave-gate" / "wave-gate-q8_0.gguf"
    g.write_bytes(blob)
    meta = {"wave-gate": {"file": g.name, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}}
    (d / "models.json").write_text(json.dumps(meta))
    assert setup.tiny_files("wave-gate", d) == (g, "")
    assert "no entry" in setup.tiny_files("wave-split", d)[1]
    for bad in ("../../requirements.txt", "other.gguf"):              # Astra security review: models.json can't
        (d / "models.json").write_text(json.dumps({"wave-gate": {**meta["wave-gate"], "file": bad}}))   # redirect
        assert "must name the file" in setup.tiny_files("wave-gate", d)[1], bad
    (d / "models.json").write_text(json.dumps(meta))
    g.write_bytes(blob[:-1] + b"x")                                     # same size, other bytes
    assert "sha256 does not match" in setup.tiny_files("wave-gate", d)[1]
    g.write_bytes(blob + b"x")
    assert "wrong size" in setup.tiny_files("wave-gate", d)[1]
    g.unlink()
    g.symlink_to(d / "models.json")
    assert "missing" in setup.tiny_files("wave-gate", d)[1]             # a symlink is never followed
    g.unlink(); g.write_bytes(blob)
    seen = {}
    def fake_run(cmd, cwd=None, **k):
        seen.update(cmd=cmd, modelfile=(Path(cwd) / "Modelfile").read_text(), copy=(Path(cwd) / g.name).read_bytes(),
                    cwd=cwd)
        return subprocess.CompletedProcess(cmd, 0, "", "success")
    with patched(subprocess, "run", fake_run):
        ok, why = setup.install_tiny("wave-gate", "http://127.0.0.1:1", d)
    assert ok and seen["cmd"] == ["ollama", "create", "wave-gate", "-f", "Modelfile"] and seen["copy"] == blob
    assert seen["modelfile"] == setup.modelfile("wave-gate") and seen["modelfile"].startswith("FROM ./wave-gate-q8_0.gguf")
    assert not Path(seen["cwd"]).exists()                                # the private copy is gone afterwards
    def no_run(*a, **k):
        raise AssertionError("ollama must not run for a bad file")
    g.write_bytes(blob[:-1] + b"y")
    with patched(subprocess, "run", no_run):
        assert not setup.install_tiny("wave-gate", "http://127.0.0.1:1", d)[0]
    real = json.loads((ROOT / "models/models.json").read_text())                # the shipped checksums
    assert set(real) == {"wave-gate", "wave-gate2", "wave-split", "wave-review"} and all(len(v["sha256"]) == 64 for v in real.values())
    for name in real:
        assert real[name]["file"] == f"{name}-q8_0.gguf"
        assert sorted((ROOT / "models" / name / "Modelfile").read_text().strip().splitlines()) == \
            sorted(setup.modelfile(name).strip().splitlines()), name          # shipped file = what setup writes
    assert "repeat_penalty 1.0" in setup.modelfile("wave-split")         # 91.6% without it


@check("D14 setup --check: READY with every model, FAIL for a missing tiny model, writes nothing")
def _():
    vault = vault_copy()
    before = sorted((p.relative_to(vault), p.stat().st_mtime_ns) for p in vault.rglob("*"))
    for names, want_code, needle in ((INSTALLED, 0, "READY: 0 failed"),
                                     ([n for n in INSTALLED if "review" not in n], 1, "wave-review not installed")):
        out = io.StringIO()
        with patched(sw, "list_models", _models(names)), contextlib.redirect_stdout(out):
            code = setup.main(["--check", "--vault", str(vault), "--env-file", str(vault / "no.env")])
        assert code == want_code and needle in out.getvalue(), out.getvalue()[-800:]
        assert "0 broken" in out.getvalue()
    assert sorted((p.relative_to(vault), p.stat().st_mtime_ns) for p in vault.rglob("*")) == before


NODE = _sh.which("node")
PLUGIN = ROOT / "dispatch_vault/.obsidian/plugins/wave-dispatch"
STUB = r"""
const Module = require("module"); const orig = Module._load;
class Base {}
Module._load = function (req, ...a) {
  if (req === "obsidian") return { Plugin: Base, ItemView: Base, PluginSettingTab: Base, Setting: Base, Notice: Base, TFile: Base };
  return orig.call(this, req, ...a);
};
const P = require(process.argv[1]);
"""


@check("D15 panel (main.js): runs wave_dispatch_app.py with an argument list, the three switches, plain-text output")
def _():
    src = (PLUGIN / "main.js").read_text()
    for banned in ("shell: true", "exec(", "execSync", "innerHTML", "outerHTML", "MarkdownRenderer", "eval(",
                   "new Function", "sai" + "_wave", "SAI" + " WAVE", "sai" + "-wave"):   # split: the export renames them
        assert banned not in src, banned
    assert "shell: false" in src and 'createEl("pre", { cls: "wave-dispatch-answer", text:' in src
    man = json.loads((PLUGIN / "manifest.json").read_text())
    assert man["id"] == "wave-dispatch" and man["name"] == "Wave Dispatch" and man["isDesktopOnly"] is True
    assert json.loads((ROOT / "dispatch_vault/.obsidian/community-plugins.json").read_text()) == ["wave-dispatch"]
    if not NODE:
        return
    r = subprocess.run([NODE, "-e", STUB + r"""
const H = P.helpers;
console.log(JSON.stringify({
  args: H.appArgs("run", { repo: "" }, "/x/repo/dispatch_vault", H.runExtra({ useNotes: false, split: false, review: false }, "m")),
  allOn: H.runExtra(H.DEFAULTS, ""), mode: H.modeLabel({ use_notes: true, split: false, review: false }),
  routes: H.routeLines(["a", "b"], { "1": ["Math Bot"], "2": ["Code Bot", "Writer"] }),
  whole: H.routeLines([], { "1": ["Writer"] }), stages: ["split", "gate", "compile", "zzz"].map(H.stageLabel),
}));""", str(PLUGIN / "main.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-1000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["args"] == ["/x/repo/wave_dispatch_app.py", "run", "--json", "--vault", "/x/repo/dispatch_vault",
                           "--solo-model", "m", "--no-notes", "--no-split", "--no-review"], out["args"]
    assert out["allOn"] == [] and out["mode"] == "split off · review off"
    assert out["routes"] == ["1. a → Math Bot", "2. b → Code Bot, Writer"] and out["whole"] == ["1. (the whole question) → Writer"]
    assert out["stages"] == ["Split", "Gate", "Compile", "zzz"]


@check("D16 setup speed check: unloaded or soon-unloaded models are reported with the exact Ollama commands per OS")
def _():
    m = sw.Machine("This Computer", "ollama", "http://127.0.0.1:11434")
    def ps(rows):
        return lambda method, url, body, timeout, key="": (
            {"models": rows} if url.endswith("/api/ps") else (_ for _ in ()).throw(AssertionError(url)))
    used = ["wave-gate:latest", "llama3.1:8b"]
    forever = [{"name": "wave-gate:latest", "expires_at": "2319-02-16T15:12:41-06:00"},
               {"name": "llama3.1:8b", "expires_at": "2319-02-16T15:12:41-06:00"}]
    with patched(sw, "_http", ps(forever)):
        assert setup.loaded_check(sw, m, used, 2026) == (True, "all 2 models of this run stay loaded")
    with patched(sw, "_http", ps(forever[:1])):
        ok, why = setup.loaded_check(sw, m, used, 2026)
        assert not ok and "llama3.1:8b" in why and "already unloaded" in why
    soon = [dict(forever[0]), {"name": "llama3.1:8b", "expires_at": "2026-11-06T21:50:00-06:00"}]
    with patched(sw, "_http", ps(soon)):
        ok, why = setup.loaded_check(sw, m, used, 2026)
        assert not ok and why.startswith("llama3.1:8b will be unloaded")
    def down(*a, **k):
        raise sw.WaveError("refused")
    with patched(sw, "_http", down):
        assert setup.loaded_check(sw, m, used, 2026)[0]          # can't tell -> never a false alarm
    for plat, needle in (("win32", "setx OLLAMA_KEEP_ALIVE -1"), ("darwin", "launchctl setenv OLLAMA_MAX_LOADED_MODELS 6"),
                         ("linux", 'Environment="OLLAMA_CONTEXT_LENGTH=16384"')):
        assert needle in setup.ollama_commands(plat), plat


@check("D17 setup with Ollama not running: one clear step-4 failure, no per-bot model failures")
def _():
    vault = vault_copy()
    def down(m, timeout=3):
        raise sw.WaveError("cannot reach http://127.0.0.1:11434/api/tags: Connection refused")
    out = io.StringIO()
    with patched(sw, "list_models", down), contextlib.redirect_stdout(out):
        code = setup.main(["--check", "--vault", str(vault), "--env-file", str(vault / "no.env")])
    t = out.getvalue()
    assert code == 1 and "not reachable" in t and "models not checked" in t, t[-600:]
    assert "is not installed" not in t and t.count("FAIL") == 2, t[-600:]


@check("D18 setup asks to pull each missing worker model ONCE (not once per bot) and pulls it once")
def _():
    vault = vault_copy()
    calls, asked = [], []
    have = [n for n in INSTALLED if n not in ("llama3.1:8b", "qwen2.5-coder:7b")]
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    def fake_ask(q, args):
        asked.append(q)
        return q.startswith("pull")
    out = io.StringIO()
    with patched(sw, "list_models", _models(have)), patched(subprocess, "run", fake_run), \
            patched(setup, "ask", fake_ask), patched(setup.shutil, "which", lambda n: "/usr/bin/" + n), \
            contextlib.redirect_stdout(out):
        setup.main(["--vault", str(vault), "--env-file", str(vault / "no.env")])
    pulls = [c for c in calls if c[:2] == ["ollama", "pull"]]
    assert sorted(c[2] for c in pulls) == ["llama3.1:8b", "qwen2.5-coder:7b"], pulls
    pq = [q for q in asked if q.startswith("pull")]
    assert len(pq) == 2 and "[[Compiler]]" in next(q for q in pq if "llama" in q), pq


@check("D19 a part only the Gate Bot Graph takes also goes to the Writer: the Graph Reader's facts are never the answer")
def _():
    t = team_for(vault_copy())
    q = "What does the [[Example Setup]] note say about the graphics card?"
    f = Fake({"Splitter": ["1. " + q], **GATES(notes=lambda p: True), "Graph Reader": ["The example PC has a 24 GB card."],
              "Writer": ["The note says the example PC has a 24 GB graphics card."],
              "Compiler": ["The note says the example PC has a 24 GB graphics card."]})
    r = de.run_dispatch(q, team=t, chat_fn=f)
    assert r.routes["1"] == ["Graph Reader", "Writer"], r.routes
    w = next(m for n, m in f.calls if n == "Writer")
    assert "24 GB card" in w[-1]["content"]                       # the Writer got the facts
    assert r.checks["all_bots_answered"] and r.status == "VERIFIED", (r.status, r.checks)


@check("D20 a code bot's '(no code needed)' is no answer: no review loop, the Writer answers instead (live 2026-11-06)")
def _():
    t = team_for(vault_copy())
    q = "write me a poem"
    f = Fake({"Splitter": ["1. " + q], **GATES(code=lambda p: True), "Code Bot": ["```python\n# (no code needed)\n```"],
              "Code Checker": ["yes"], "Writer": ["Roses are red, violets are blue."], "Compiler": ["x"]})
    r = de.run_dispatch(q, team=t, chat_fn=f)
    who = [n for n, _ in f.calls]
    assert "Writer" in who and "Code Checker" not in who and "Try Again Coding Bot" not in who, who
    assert r.answer == "Roses are red, violets are blue." and r.routes["1"] == ["Writer"], (r.answer, r.routes)
    assert dc.no_code_answer("```html\n<!-- no code needed -->\n```") and not dc.no_code_answer("def f(): pass  # no code needed")


@check("D21 security review 2026-11-06: vault Machine notes can't read other secrets or use unapproved servers")
def _():
    vault = vault_copy()
    m = vault / "Machines/This Computer.md"
    base = m.read_text()
    m.write_text(base.replace('endpoint: "${WAVE_DISPATCH_ENDPOINT:-http://127.0.0.1:11434}"',
                              'endpoint: "http://127.0.0.1:11434"\napi_key: "${HOME}"'))
    try:
        de.load_team(vault, {}, {}, {}); raise AssertionError("not refused")
    except sw.WaveError as e:
        assert "only use ${WAVE_DISPATCH_" in str(e) and "${HOME}" in str(e), e
    m.write_text(base.replace('endpoint: "${WAVE_DISPATCH_ENDPOINT:-http://127.0.0.1:11434}"',
                              'endpoint: "http://127.0.0.1:11434"\napi_key: "${123_SECRET}"'))   # review 2: digit first
    try:
        de.load_team(vault, {}, {}, {}); raise AssertionError("not refused")
    except sw.WaveError as e:
        assert "${123_SECRET}" in str(e), e
    m.write_text(base.replace('"${WAVE_DISPATCH_ENDPOINT:-http://127.0.0.1:11434}"', '"https://attacker.example"'))
    try:
        de.load_team(vault, {}, {}, {}); raise AssertionError("not refused")
    except sw.WaveError as e:
        assert "not approved" in str(e), e
    assert de.load_team(vault, {"WAVE_DISPATCH_NODE2_URL": "https://attacker.example"}, {}, {})   # approved in .env
    assert de.load_team(vault, {}, {"This Computer": {"endpoint": "https://attacker.example"}}, {})  # or in the config
    m.write_text(base)
    assert de.load_team(vault, {}, {}, {}).machines["This Computer"].base_url == "http://127.0.0.1:11434"


@check("D22 security review: hostile replies can't stall the sum check, forge a checked number, or plant links in notes")
def _():
    import time
    from wave_dispatch import calc as _calc
    t0 = time.time()
    _calc.check_sums(("1" + " " * 300 + "+" + " " * 300 + "1" + " " * 300 + "!\n") * 20)
    assert time.time() - t0 < 0.5, time.time() - t0
    vault = vault_copy()
    t = team_for(vault)
    q = "What is 2 + 2, and explain what addition is?"
    split = "1. What is 2 + 2?\n2. ![x](https://attacker.example/p.png) Explain what addition is?"
    f = Fake({"Splitter": [split], **GATES(math=lambda p: "2 + 2" in p, writer=lambda p: "addition" in p),
              "Math Bot": ["2 + 2 = 4"], "Writer": ["Adding puts numbers together."],
              "Compiler": ["2 + 2 = 999. Adding puts numbers together."]})
    writer = sw.VaultWriter(vault, Path(tempfile.mkdtemp(prefix="wd_state_")), portable=True)
    r = de.run_dispatch(q, team=t, chat_fn=f, writer=writer)
    assert r.checks["sums_correct"] is False and r.status != "VERIFIED", (r.status, r.checks)   # the Compiler's 999
    f2 = Fake({"Splitter": [split], **GATES(math=lambda p: "2 + 2" in p, writer=lambda p: "addition" in p),
               "Math Bot": ["2 + 2 = 4"], "Writer": ["Adding puts numbers together."],
               "Compiler": ["The result is 999. Adding puts numbers together."]})   # review 2: no equation at all
    r2 = de.run_dispatch(q, team=team_for(vault_copy()), chat_fn=f2)
    assert r2.checks.get("numbers_from_bots") is False and r2.status != "VERIFIED", (r2.status, r2.checks)
    f3 = Fake({"Splitter": [split], **GATES(math=lambda p: "2 + 2" in p, writer=lambda p: "addition" in p),
               "Math Bot": ["2 + 2 = 4"], "Writer": ["Adding puts numbers together."],
               "Compiler": ["2 + 2 is 4. Adding puts numbers together (part 1 of 2)."]})
    r3 = de.run_dispatch(q, team=team_for(vault_copy()), chat_fn=f3)
    assert r3.checks.get("numbers_from_bots") is True, r3.checks                           # no false alarm
    note = (vault / r.run_note).read_text()
    assert len(r.parts) == 2, (r.parts, r.split_refused)
    before_fence = note.split("## Parts and bots", 1)[1].split("```text", 1)[0]
    assert "attacker.example" not in before_fence, before_fence                           # only inside the fence


@check("D23 security review: receipts hash the final answer, and verifying compares it with the run note; no fake receipts")
def _():
    vault = vault_copy()
    t = team_for(vault)
    writer = sw.VaultWriter(vault, Path(tempfile.mkdtemp(prefix="wd_state_")), portable=True)
    f = Fake({"Splitter": [SPLIT], **GATES(), "Code Checker": ["no"], "Code Bot": [CODE],
              "Math Bot": ["There are 25 primes below 100."], "Compiler": ["[CODE 1]\n\n25 primes."]})
    r = de.run_dispatch(MSG, team=t, chat_fn=f, writer=writer)
    v = sw.verify_receipt(vault / r.receipt_note, vault)
    assert v["hash_ok"] and v["notes_ok"] and v["answer_ok"], v
    run = vault / r.run_note
    run.write_text(run.read_text().replace("25 primes.", "99 primes."))                    # someone edits the answer
    v = sw.verify_receipt(vault / r.receipt_note, vault)
    assert v["hash_ok"] and not v["answer_ok"] and "final answer in the run note" in v["changed"], v
    run.write_text(run.read_text().replace("99 primes.", "25 primes."))                    # back to the original
    assert sw.verify_receipt(vault / r.receipt_note, vault)["answer_ok"]
    orig = run.read_text()
    shown = orig.replace("25 primes.", "99 primes.") + "\n## Final answer\n" + sw._fence(r.answer) + "\n"
    run.write_text(shown)                                       # review 2: the real answer appended as a 2nd section
    assert not sw.verify_receipt(vault / r.receipt_note, vault)["answer_ok"]
    run.write_text(orig)
    assert "older than 7" in sw.verify_receipt(vault / r.receipt_note, vault, min_version=7)["error"]   # no downgrade
    canon = json.loads(re.search(r"```+json\n(.*?)\n```+", (vault / r.receipt_note).read_text(), re.S).group(1))
    fake = {**canon, "team": [], "steps": []}
    assert "needs its team" in sw.validate_manifest(fake)
    fake = {**canon, "checks": {"final_answer_present": True}}
    assert "every check" in sw.validate_manifest(fake)


@check("D24 security review 3: sneaky numbers, sum forms, answer-section tricks and short keys")
def _():
    from wave_dispatch import calc as _calc
    w = lambda t: [(x["claimed"], x["actual"]) for x in _calc.check_sums(t)[0]]
    assert w("2 + 2 = +7") == [("7", "4")] and w("3x(4+1)=999") == [("999", "15")]
    assert w("4 + 6 = 2 * 5 = 10") == [] and w("2000 + 2000 = 4,000") == [] and w("max 3 + 4 = 7") == []
    for bad in ("The result is 999e0", "The result is 9.99e2", "The result is 7", "The result is 1,000"):
        assert de._new_numbers(bad, ["2 + 2 = 4"], parts=2), bad
    assert de._new_numbers("2 + 2 is 4 (part 1 of 2)", ["2 + 2 = 4"], parts=2) == []
    for bad in (".999", "-4", "1"):                                    # security review 4
        assert de._new_numbers(f"The result is {bad}", ["2 + 2 = 4"], parts=2), bad
    for ok in ("1. The answer is 4.", "(1) 4 (2) four", "This is part 1 of 2: 4"):
        assert de._new_numbers(ok, ["2 + 2 = 4"], parts=2) == [], ok
    assert w("4 + 6 = 2 * 6 = 12") == [("2 * 6", "10")] and w("x = 3 + 4 = 7") == []
    assert w("4 + 6 = (2 * 6) = 12") == [("(2 * 6)", "10")] and w("5 − 3 = 3") == [("3", "2")]      # review 5
    assert de._new_numbers("The result is −4", ["2 + 2 = 4"], parts=2)
    for bad in ("The result is − 4", "The result is (99)", "99. The result"):          # review 6
        assert de._new_numbers(bad, ["2 + 2 = 4"], parts=2), bad
    assert de._new_numbers("10 - 4 is 6", ["10 - 4 = 6"], parts=1) == []
    assert w("2 + 2 = **99**") == [("99", "4")] and w("2 ** 3 = 9") == [("9", "8")] and w("2**3 = 8") == []   # review 7
    assert de._new_numbers("The result is − **4**", ["2 + 2 = 4"]) and not de._new_numbers("It is **4**", ["2 + 2 = 4"])
    import time
    t0 = time.time(); _calc.check_sums(("1,2,3,4,5 " * 100 + "%\n") * 200); assert time.time() - t0 < 0.5
    good = "# Run\n\n## Final answer\n" + sw._fence("4") + "\n\n## split\nx\n"
    assert sw._final_answer_section(good) == sw._fence("4").strip()
    tricks = [good.replace("## Final answer\n", "## Final answer ##\n") + "\n## Final answer\n" + sw._fence("9") + "\n",
              "<!--\n" + good + "\n-->\n## Final answer\n" + sw._fence("9") + "\n",
              good + "\n### final answer\n" + sw._fence("9") + "\n"]
    for t in tricks:
        assert sw._final_answer_section(t) is None, t[:80]
    assert sw._final_answer_section("## Final answer\n```text\n## Final answer\n```\n") == "```text\n## Final answer\n```"
    vault = vault_copy()
    m = vault / "Machines/This Computer.md"
    m.write_text(m.read_text().replace("tags: [wave-dispatch/machine]", 'api_key: "${WAVE_DISPATCH_KEY:-abc}"\ntags: [wave-dispatch/machine]'))
    try:
        de.load_team(vault, {}, {}, {}); raise AssertionError("short key accepted")
    except sw.WaveError as e:
        assert "shorter than 8" in str(e), e


if __name__ == "__main__":
    for name, ok, why in RESULTS:
        print(("  \033[32mPASS\033[0m  " if ok else "  \033[31mFAIL\033[0m  ") + name + (f"\n        {why}" if why else ""))
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{passed} passed, {len(RESULTS) - passed} failed, 0 skipped")
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs/verify_wave_dispatch.json").write_text(json.dumps([{"name": n, "ok": o, "why": w} for n, o, w in RESULTS]))
    sys.exit(0 if passed == len(RESULTS) else 1)
