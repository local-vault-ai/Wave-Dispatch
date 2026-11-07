"""Wave Dispatch ("Small AI Wave"): a team of small local models run as one "Small Brain" in synchronized waves,
driven by an Obsidian vault (`starter_vault/` by default).

  Router Brain (duty router)    plans first (one short line) and writes the final answer last
  then every other active bot, in ascending `wave` order (bots that share a wave run concurrently; calls to one
  host are still limited by that host's `max_parallel_models`):
    duty graph   Python walks the [[wikilink]] graph from the prompt's seed notes; the model distills a briefing
    duty build   solves / builds from the prompt + plan + briefings (+ earlier build outputs)
    duty verify  audits every build output; must answer ONLY {"verdict": "PASS"|"FAIL", "issues": [...]}

Python owns all structure: wave order comes from the bot notes and is validated (graph < build < verify < router),
the graph walk is deterministic, and every receipt check is computed here, never taken from a model's prose. A run is
VERIFIED only if every check passes AND every verifier said PASS. Any error = FAILED, and a receipt is still written.

Machines are vault notes (`Machines/<Name>.md`: provider, endpoint, enabled, max_parallel_models, vram_gb). The note wins;
config `machines:` only fills keys a note leaves out (or defines a machine with no note). Bots send the exact model id
the machine reported (`resolve_models`), never a guessed alias.

Vault writes (Run notes, Logs/ receipts, model remaps) go through `wave_dispatch.guard.Guard` (write-ahead audit log,
symlink-safe writes). If the Guard cannot start, that is an error (fail closed). Only where the Guard cannot exist at all
(no POSIX `fcntl`, i.e. Windows) a portable writer with its own write-ahead audit and symlink checks is used, and the
receipt says so (`guard: portable`).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import yaml

from . import redact

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = ROOT / "starter_vault"
LINK = re.compile(r"\[\[([^\]|#]+?)\\?(?:#[^\]|]*)?(?:\\?\|[^\]]*)?\]\]")
FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
THINK = re.compile(r"<think>.*?</think>", re.S)
CODE = re.compile(r"^(```|~~~).*?^\1[^\n]*$|`[^`\n]+`", re.S | re.M)   # Obsidian ignores links in code
ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")      # '/' is needed for MLX / HF ids (mlx-community/...)
BOT_KEYS = ("model", "role", "wave", "temperature", "seed", "machine", "status")
DUTIES = ("router", "graph", "build", "improve", "finalize", "verify")
REFINE = ("improve", "finalize")      # the refine loops: draft (build) -> improve -> finalize, each its own bot note
ACTIVE = ("active", "on", "enabled")
# Exact server messages that mean "this model / server does not take the think option" (Ollama + JSON decoders).
THINK_UNSUPPORTED = [re.compile(p, re.I) for p in (
    r'^"[^"]{1,200}" does not support thinking$',                 # Ollama: "<model>" does not support thinking
    r'^json: unknown field "think"$',                            # older Ollama: strict JSON decoder
    r'^(unknown|unsupported|unrecognized) (field|parameter|option):? "?think"?$',
)]
ERROR_KEEP = 300                                                 # chars of an error body kept AFTER redaction
# Preferred models per duty, best first; anything installed that is not listed is a last resort.
PREFER = {
    "router": ["qwen3:4b", "llama3.1:8b", "qwen2.5:7b", "gemma3:4b", "llama3.2:3b", "phi4-mini", "qwen3:8b"],
    "graph": ["qwen3:4b", "llama3.2:3b", "gemma3:4b", "llama3.1:8b", "qwen2.5:7b", "phi4-mini"],
    "build": ["qwen2.5-coder:7b", "qwen2.5-coder:14b", "qwen2.5-coder:3b", "llama3.1:8b", "qwen3:4b"],
    "verify": ["qwen2.5-coder:7b", "llama3.1:8b", "qwen3:4b", "qwen2.5:7b", "gemma3:4b"],
}
NOT_CHAT = ("embed", "tts", "whisper", "rerank", "clip", "bge", "nomic", "voice")


class WaveError(RuntimeError):
    pass


def safe_text(text: str) -> str:
    """Error text for the terminal / vault: secrets and URL credentials removed."""
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", str(text))
    text = re.sub(r"([?&](?:key|token|api_key|apikey|access_token|auth)=)[^&\s]+", r"\1[REDACTED]", text, flags=re.I)
    return redact.scrub(text)


def safe_url(url: str) -> str:
    """URL without userinfo or query (for display)."""
    try:
        u = urlparse(url)
        netloc = u.hostname or ""
        if u.port:
            netloc += f":{u.port}"
        return urlunparse((u.scheme, netloc, u.path, "", "", ""))
    except ValueError:
        return "[invalid url]"


# --------------------------------------------------------------------------- env + notes

def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines from a .env file (no shell expansion). Missing file = {}."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def expand(value: Any, env: dict[str, str]) -> Any:
    """`${VAR}` / `${VAR:-default}` from env (process env wins over .env)."""
    if not isinstance(value, str):
        return value
    return ENV.sub(lambda m: os.environ.get(m.group(1)) or env.get(m.group(1)) or (m.group(2) or ""), value)


@dataclass
class Note:
    path: Path
    rel: str            # POSIX path relative to the vault, with .md
    props: dict
    body: str
    text: str = ""      # full file content (frontmatter + body)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def key(self) -> str:
        """Vault path without .md: unique (two folders may both hold a note called 'Fact')."""
        return self.rel.removesuffix(".md")

    def links(self) -> list[str]:
        """Every [[target]] in the properties and the body (not inside code), in order."""
        raw = yaml.safe_dump(self.props) if self.props else ""
        return [m.group(1).strip().rstrip("\\") for m in LINK.finditer(raw + "\n" + CODE.sub("", self.body))]


_ROOTS: dict[str, tuple[str, int, int]] = {}      # vault path -> (real path, st_dev, st_ino), pinned at first use
_ROOTS_LOCK = threading.Lock()


def _pin_root(vault: Path) -> tuple[str, int, int]:
    """The vault root's identity, captured once per process. A root later swapped for a symlink or another folder
    no longer matches and is refused."""
    key = os.path.abspath(vault)
    with _ROOTS_LOCK:
        if key not in _ROOTS:
            # the root is resolved exactly ONCE, before any validation, and its identity is taken from a no-follow
            # open of that real path (a swap between realpath and open is refused, never pinned)
            real = os.path.realpath(vault)
            if DIRFD:
                fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    st = os.fstat(fd)
                finally:
                    os.close(fd)
            else:
                st = os.stat(real)
            if not stat.S_ISDIR(st.st_mode):
                raise WaveError("the vault is not a folder")
            _ROOTS[key] = (real, st.st_dev, st.st_ino)
        return _ROOTS[key]


def _open_root(vault: Path) -> int:
    real, dev, ino = _pin_root(vault)
    fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (dev, ino):
        os.close(fd)
        raise WaveError("the vault folder was replaced while in use; refusing to read")
    return fd


def read_in_vault(vault: Path, rel: str) -> str:
    """Read vault/rel without a check-then-open race: every folder is opened by fd from the vault root with
    O_NOFOLLOW and the file itself with O_NOFOLLOW (+ O_NONBLOCK, so a swapped-in FIFO can't hang us), so a file
    or folder replaced by a symlink after validation is refused instead of read. Must be a regular file."""
    try:
        _pin_root(vault)                               # pin FIRST: every check below uses this one identity
    except OSError as e:
        raise WaveError(f"vault unreadable ({e.strerror})") from None
    if jailed_note_path(vault, rel) is None:
        raise WaveError(f"refusing to read outside the vault: {safe_text(rel)}")
    parts = rel.split("/")
    try:
        if not DIRFD:                               # Windows: no dir_fd (weaker, checked above)
            data = (vault.joinpath(*parts)).read_bytes()
        else:
            fd = _open_root(vault)
            try:
                for part in parts[:-1]:
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = nfd
                ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
            finally:
                os.close(fd)
            with os.fdopen(ffd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise WaveError(f"{safe_text(rel)} is not a regular file")
                data = f.read()
    except OSError as e:
        raise WaveError(f"{safe_text(rel)}: unreadable or not a plain file inside the vault ({e.strerror})") from None
    # same text as Path.read_text() (universal newlines), so note text and receipt hashes are unchanged
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def read_note(path: Path, vault: Path) -> Note:
    rel = PurePosixPath(*path.relative_to(vault).parts).as_posix()
    text = read_in_vault(vault, rel)
    full = text
    props: dict = {}
    m = FRONT.match(text)
    if m:
        try:
            props = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise WaveError(f"{rel}: bad YAML frontmatter: {e}") from None
        if not isinstance(props, dict):
            raise WaveError(f"{rel}: frontmatter must be key: value pairs")
        text = text[m.end():]
    return Note(path, rel, props, text, full)


SKIP_DIRS = (".obsidian", ".trash")


def vault_files(vault: Path, suffix: str | None = None, sub: str | None = None, recursive: bool = True) -> list[Path]:
    """Every regular file inside the vault, jailed to it: symlinked files and folders are skipped (never followed),
    and a file whose real path is outside the vault root is dropped. `sub` limits the walk to one top-level folder."""
    try:
        root = Path(_pin_root(vault)[0])               # the FIRST touch of the root is the no-follow pin
    except (OSError, WaveError):
        return []
    start = vault / sub if sub else vault
    if os.path.islink(start) or not os.path.isdir(start):
        return []
    out: list[Path] = []
    for d, dirs, files in os.walk(start, followlinks=False):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not os.path.islink(os.path.join(d, x))] if recursive else []
        for f in files:
            p = Path(d) / f
            if (suffix and p.suffix != suffix) or os.path.islink(p) or not p.is_file():
                continue
            if not Path(os.path.realpath(p)).is_relative_to(root):
                continue
            out.append(p)
    return sorted(out)


def jailed_note_path(vault: Path, rel: str) -> Path | None:
    """vault/rel only if rel is vault-relative, has no symlinked part and its real path stays inside the vault."""
    pp = PurePosixPath(rel)
    if pp.is_absolute() or not pp.parts or any(x in ("..", ".", "") for x in rel.split("/")) or "\\" in rel:
        return None
    cur = vault
    for part in pp.parts:
        cur = cur / part
        if os.path.islink(cur):
            return None
    try:
        root = _pin_root(vault)[0]                     # the pinned root, never a fresh realpath of the vault
    except (OSError, WaveError):
        return None
    if not Path(os.path.realpath(cur)).is_relative_to(Path(root)):
        return None
    return cur


def vault_notes(vault: Path) -> list[Note]:
    return [read_note(p, vault) for p in vault_files(vault, ".md")]


class Index:
    """Resolves link targets like Obsidian: an exact vault path (with or without .md), else a bare name that is unique.
    A bare name shared by two notes is ambiguous (reported, never guessed)."""

    def __init__(self, vault: Path, notes: list[Note] | None = None):
        self.notes = {n.key: n for n in (notes if notes is not None else vault_notes(vault))}
        files = vault_files(vault)
        self.paths = {PurePosixPath(*p.relative_to(vault).parts).as_posix() for p in files}
        self.by_name: dict[str, list[str]] = {}
        for p in sorted(self.paths):
            pp = PurePosixPath(p)
            for nm in {pp.name, pp.stem} if pp.suffix == ".md" else {pp.name}:
                self.by_name.setdefault(nm, []).append(p)

    def resolve(self, target: str) -> tuple[str | None, bool]:
        """(vault path or None, ambiguous?)"""
        t = target.strip().lstrip("/")
        for cand in (t, t + ".md"):
            if cand in self.paths:
                return cand, False
        hits = self.by_name.get(t, [])
        if len(hits) == 1:
            return hits[0], False
        return None, len(hits) > 1

    def note(self, target: str) -> Note | None:
        p, _ = self.resolve(target)
        return self.notes.get(p.removesuffix(".md")) if p and p.endswith(".md") else None


def link_target(t: str) -> str:
    """[[Models/qwen3 4b]] and [[qwen3 4b]] both name the note `qwen3 4b`."""
    return t.split("/")[-1].removesuffix(".md")


def graph_report(vault: Path) -> dict:
    """Links checked, broken + ambiguous links and orphan notes (no link in or out)."""
    notes = vault_notes(vault)
    idx = Index(vault, notes)
    linked_in: set[str] = set()
    total, broken, ambiguous, out_count = 0, [], [], {}
    for n in notes:
        ls = n.links()
        out_count[n.key] = len(ls)
        for t in ls:
            total += 1
            p, amb = idx.resolve(t)
            if p:
                linked_in.add(p.removesuffix(".md"))
            elif amb:
                ambiguous.append((n.rel, t))
            else:
                broken.append((n.rel, t))
    orphans = [n.rel for n in notes if not out_count[n.key] and n.key not in linked_in]
    return {"notes": len(notes), "links": total, "broken": broken, "ambiguous": ambiguous, "orphans": orphans}



def _quantum():
    """Wave Dispatch's exact circuit tool (run_wave and receipts with circuits). Imported on first use, so a build without
    quantum.py (the Wave Dispatch release) still reads, writes and verifies receipts that have no circuits."""
    from . import quantum
    return quantum

# --------------------------------------------------------------------------- team

@dataclass
class Machine:
    name: str
    provider: str           # ollama | openai
    base_url: str
    max_parallel_models: int = 1
    vram_gb: float = 24
    status: str = "on"
    api_key: str = ""

    @property
    def host(self) -> str:
        u = urlparse(self.base_url)
        return f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"

    @property
    def enabled(self) -> bool:
        return self.status in ACTIVE


@dataclass
class Bot:
    name: str
    model: str
    role: str
    wave: int
    duty: str               # router | graph | build | improve | finalize | verify
    temperature: float
    seed: int
    machine: str
    status: str
    instructions: str
    note: Note | None = None
    runtime_model: str = ""   # exact id the machine reported (resolve_models); empty = unresolved

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def send_model(self) -> str:
        return self.runtime_model or self.model


@dataclass
class Team:
    vault: Path
    bots: list[Bot]
    machines: dict[str, Machine]

    def active(self) -> list[Bot]:
        return [b for b in self.bots if b.active]

    def by_duty(self, duty: str) -> Bot:
        for b in self.active():
            if b.duty == duty:
                return b
        raise WaveError(f"no active bot with duty '{duty}' in {self.vault.name}/Bots")

    def machine_for(self, bot: Bot) -> Machine:
        m = self.machines.get(bot.machine)
        if not m:
            raise WaveError(f"bot '{bot.name}': machine [[{bot.machine}]] has no note in Machines/")
        if not m.enabled:
            raise WaveError(f"bot '{bot.name}': machine [[{m.name}]] is disabled (enabled: false)")
        return m

    def waves(self) -> list[tuple[int, list[Bot]]]:
        """Non-router active bots grouped by `wave`, ascending."""
        out: dict[int, list[Bot]] = {}
        for b in self.active():
            if b.duty != "router":
                out.setdefault(b.wave, []).append(b)
        return sorted(out.items())

    def validate(self) -> None:
        """Exactly one router; at least one graph/build/verify; graph < build < improve < finalize < verify < router
        waves (improve/finalize are optional, one bot per wave: each loop refines the one version before it);
        each active bot's machine exists; two machines on one host agree on max_parallel_models."""
        act = self.active()
        routers = [b for b in act if b.duty == "router"]
        if len(routers) != 1:
            raise WaveError(f"need exactly one active router bot, found {len(routers)}")
        for d in ("graph", "build", "verify"):
            if not any(b.duty == d for b in act):
                raise WaveError(f"no active bot with duty '{d}'")
        for b in act:
            if b.duty not in DUTIES:
                raise WaveError(f"bot '{b.name}': unknown duty '{b.duty}' (use {', '.join(DUTIES)})")
            self.machine_for(b)
        top = lambda d: max(b.wave for b in act if b.duty == d)
        low = lambda d: min(b.wave for b in act if b.duty == d)
        chain = [d for d in ("graph", "build", "improve", "finalize", "verify") if any(b.duty == d for b in act)]
        if not (all(top(a) < low(b) for a, b in zip(chain, chain[1:])) and top("verify") < routers[0].wave):
            raise WaveError("wave order must be graph < build < improve < finalize < verify < router "
                            "(edit the bots' `wave` property)")
        for d in REFINE:
            waves = [b.wave for b in act if b.duty == d]
            if len(waves) != len(set(waves)):
                raise WaveError(f"two '{d}' bots share a wave: give each refine loop its own `wave` number")
        hosts: dict[str, Machine] = {}
        for m in self.machines.values():
            if m.enabled:
                o = hosts.setdefault(m.host, m)
                if o.max_parallel_models != m.max_parallel_models:
                    raise WaveError(f"machines [[{o.name}]] and [[{m.name}]] share {m.host} but set different "
                                    "max_parallel_models")


def _status(v: Any) -> str:
    """YAML reads on/off/yes/no/true/false as booleans."""
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v).strip().lower()


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def _machine(name: str, pr: dict, where: str) -> Machine:
    url = str(pr.get("endpoint", pr.get("base_url", "")) or "").rstrip("/")
    status = _status(pr["enabled"]) if "enabled" in pr else _status(pr.get("status", "on"))
    provider = str(pr.get("provider", "ollama")).lower()
    if provider not in ("ollama", "openai"):
        raise WaveError(f"{where}: provider must be ollama or openai")
    u = urlparse(url)
    if status in ACTIVE and (u.scheme not in ("http", "https") or not u.hostname):
        raise WaveError(f"{where}: endpoint must be an http(s) URL")
    try:
        return Machine(name, provider, url, max(1, int(pr.get("max_parallel_models", 1) or 1)),
                       float(pr.get("vram_gb", 24) or 24), status, str(pr.get("api_key", "") or ""))
    except (TypeError, ValueError) as e:
        raise WaveError(f"{where}: {e}") from None


def load_team(vault: Path = DEFAULT_VAULT, env: dict[str, str] | None = None,
              defaults: dict[str, dict] | None = None, overrides: dict[str, dict] | None = None) -> Team:
    """Bots + Machines from the vault. Machine note properties WIN over `defaults` (config `machines:`), which only
    fill keys a note leaves out or define machines that have no note. `overrides` (CLI --endpoint/--provider) win."""
    env = env if env is not None else load_env(ROOT / ".env")
    defaults, overrides = defaults or {}, overrides or {}
    machines: dict[str, Machine] = {}
    for p in vault_files(vault, ".md", "Machines", recursive=False):
        n = read_note(p, vault)
        if n.props.get("type") != "machine":
            continue
        merged = {**(defaults.get(n.name) or {}), **n.props, **(overrides.get(n.name) or {})}
        machines[n.name] = _machine(n.name, {k: expand(v, env) for k, v in merged.items()}, n.rel)
        if machines[n.name].api_key:
            redact.register(machines[n.name].api_key)
    for name in set(defaults) | set(overrides):
        if name not in machines and isinstance(defaults.get(name, overrides.get(name)), dict):
            merged = {**(defaults.get(name) or {}), **(overrides.get(name) or {})}
            machines[name] = _machine(name, {k: expand(v, env) for k, v in merged.items()}, f"config machines.{name}")
            if machines[name].api_key:
                redact.register(machines[name].api_key)
    bots = []
    for p in vault_files(vault, ".md", "Bots", recursive=False):
        n = read_note(p, vault)
        if n.props.get("type") != "bot":
            continue
        missing = [k for k in BOT_KEYS if k not in n.props]
        if missing:
            raise WaveError(f"{n.rel}: missing properties {missing}")
        pr = n.props
        try:
            bots.append(Bot(n.name, str(pr["model"]), str(pr["role"]), int(pr["wave"]), str(pr.get("duty", "")),
                            float(pr["temperature"]), int(pr["seed"]), link_target(str(pr["machine"]).strip("[]")),
                            _status(pr["status"]), _section(n.body, "Instructions"), n))
        except (TypeError, ValueError) as e:
            raise WaveError(f"{n.rel}: {e}") from None
    if not bots:
        raise WaveError(f"no bot notes in {vault.name}/Bots")
    return Team(vault, sorted(bots, key=lambda b: (b.wave, b.name)), machines)


# --------------------------------------------------------------------------- connector

class HostLimiter:
    """Counting limiter whose limit follows the Machine note (raised or lowered between calls)."""

    def __init__(self, limit: int):
        self.limit, self.active = max(1, limit), 0
        self.cond = threading.Condition()

    def set_limit(self, limit: int) -> None:
        with self.cond:
            self.limit = max(1, limit)
            self.cond.notify_all()

    def acquire(self, deadline: float | None = None) -> None:
        """Wait for a slot; raises WaveError when `deadline` (time.monotonic) passes first."""
        with self.cond:
            while self.active >= self.limit:
                if deadline is None:
                    self.cond.wait()
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WaveError("deadline passed while waiting for a free model slot on this host")
                self.cond.wait(left)
            self.active += 1

    def release(self) -> None:
        with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()


_host_locks: dict[str, HostLimiter] = {}
_host_guard = threading.Lock()


def host_lock(m: Machine) -> HostLimiter:
    """One limiter per host:port; its limit is set from this Machine every time (Team.validate refuses two machines on
    one host with different limits)."""
    with _host_guard:
        lim = _host_locks.get(m.host)
        if lim is None:
            lim = _host_locks[m.host] = HostLimiter(m.max_parallel_models)
        elif lim.limit != m.max_parallel_models:
            lim.set_limit(m.max_parallel_models)
        return lim


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Local model APIs never need redirects; following one could send the API key to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WaveError(f"refused HTTP {code} redirect from {safe_url(req.full_url)} (redirects are never followed)")


class _Watchdog:
    """Hard wall clock for ONE request: at the deadline every socket of the request is shut down, so any blocked read
    (status line, headers, chunk markers, body) ends at once. A connection that would start after the deadline fired
    refuses to connect (no race between the timer and socket creation)."""

    def __init__(self, deadline: float | None = None):
        self.fired = False
        self.deadline = deadline
        self.lock = threading.Lock()
        # duplicate handles of every TCP connection of this request, taken right after the TCP connect: shutting one
        # down ends ANY blocked phase (TLS handshake, headers, chunk markers, body), even after urllib / ssl detached
        # or wrapped the original socket object
        self.socks: list = []

    def register(self, raw: socket.socket) -> None:
        dup = raw.dup()          # portable socket duplicate (Winsock needs WSADuplicateSocket; os.dup is POSIX-only)
        with self.lock:
            self.socks.append(dup)
            late = self.fired
        if late:
            self.abort()
            raise TimeoutError("deadline passed while connecting")

    def close(self) -> None:
        with self.lock:
            socks, self.socks = self.socks, []
        for sk in socks:
            try:
                sk.close()
            except OSError:
                pass

    def abort(self) -> None:
        with self.lock:
            self.fired = True
            socks = list(self.socks)
        for sk in socks:
            if sk is not None:
                try:
                    sk.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sk.close()
                except OSError:
                    pass

    def connection(self, base):
        w = self

        def create(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kw):
            """TCP connect, then register with the watchdog BEFORE any proxy CONNECT or TLS handshake runs."""
            if w.fired:
                raise TimeoutError("deadline passed before connecting")
            raw = socket.create_connection(address, timeout, source_address, **kw)
            try:
                w.register(raw)
                if w.deadline is not None:
                    remaining = w.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("deadline passed while connecting")
                    raw.settimeout(remaining)
            except BaseException:
                raw.close()
                raise
            return raw

        class Conn(base):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._create_connection = create          # used by HTTPConnection.connect (and so HTTPSConnection)

            def connect(self):
                if w.fired:
                    raise TimeoutError("deadline passed before connecting")
                super().connect()                          # TCP (registered) -> [proxy CONNECT] -> [TLS handshake]
        return Conn


class _WatchHTTP(urllib.request.HTTPHandler):
    def __init__(self, w: _Watchdog):
        super().__init__()
        self.w = w

    def http_open(self, req):
        return self.do_open(self.w.connection(http.client.HTTPConnection), req)


class _WatchHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, w: _Watchdog):
        super().__init__()
        self.w = w

    def https_open(self, req):
        return self.do_open(self.w.connection(http.client.HTTPSConnection), req, context=self._context)


def _opener_for(w: _Watchdog):
    """Direct connections only: an empty ProxyHandler means HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / getproxies() are
    never consulted, so model traffic and the Authorization header never go through an ambient proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _WatchHTTP(w), _WatchHTTPS(w))


@dataclass
class HTTPFail(WaveError):
    code: int
    body: str

    def __str__(self):
        return f"HTTP {self.code}: {safe_text(self.body)[:ERROR_KEEP]}"

    def error_message(self) -> str:
        """The server's `error` string when the body is JSON {"error": "..."}; else ''."""
        try:
            e = json.loads(self.body).get("error")
        except (ValueError, AttributeError):
            return ""
        return e.strip() if isinstance(e, str) else ""


def think_unsupported(e: "HTTPFail") -> bool:
    return e.code == 400 and any(p.fullmatch(e.error_message()) for p in THINK_UNSUPPORTED)


MAX_BODY = 8_000_000


def _http(method: str, url: str, body: dict | None, timeout: float, api_key: str = "",
          deadline: float | None = None) -> dict:
    """One request. `deadline` (time.monotonic) is a hard wall clock: checked before sending and between reads."""
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    left = deadline - time.monotonic()
    if left <= 0:
        raise WaveError(f"deadline passed before the request to {safe_url(url)} was sent")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    def read_all(r) -> bytes:
        """Wall-clock bounded: before EVERY read the socket timeout is set to the time left, and read1 returns what has
        arrived instead of waiting for a full buffer (a trickling server can't stretch the deadline)."""
        buf = bytearray()
        sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
        reader = getattr(r, "read1", None) or r.read
        closed = getattr(r, "isclosed", None)
        while True:
            if closed is not None and closed():      # the body was read completely (the response closed its socket)
                return bytes(buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaveError(f"deadline passed while reading the reply from {safe_url(url)}")
            if sock is not None:
                sock.settimeout(remaining)
            try:
                chunk = reader(65536)
            except (TimeoutError, OSError) as e:
                if time.monotonic() >= deadline or isinstance(e, TimeoutError):
                    raise WaveError(f"deadline passed while reading the reply from {safe_url(url)}") from None
                raise
            if not chunk:
                return bytes(buf)
            buf += chunk
            if len(buf) > MAX_BODY:
                raise WaveError(f"reply from {safe_url(url)} is larger than {MAX_BODY} bytes")

    w = _Watchdog(deadline)
    timer = threading.Timer(left, w.abort)
    timer.daemon = True
    timer.start()
    try:
        try:
            with _opener_for(w).open(req, timeout=left) as r:
                raw = read_all(r).decode(errors="replace")
            if w.fired:                     # a shut-down socket reads as a clean EOF: never take that as a reply
                raise WaveError("watchdog fired")
        except urllib.error.HTTPError as e:
            if w.fired:
                raise
            try:
                err = read_all(e).decode(errors="replace")      # whole body: redaction runs before any truncation
            except WaveError:
                err = ""
            raise HTTPFail(e.code, err) from None
    except HTTPFail:
        raise
    except Exception as e:
        if w.fired or time.monotonic() >= deadline:
            raise WaveError(f"deadline passed while waiting for {safe_url(url)}") from None
        if isinstance(e, WaveError):
            raise
        if isinstance(e, (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException)):
            raise WaveError(f"cannot reach {safe_url(url)}: {safe_text(getattr(e, 'reason', e))}") from None
        raise
    finally:
        timer.cancel()
        w.close()
    try:
        out = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise WaveError(f"malformed JSON from {safe_url(url)}") from None
    if not isinstance(out, dict):
        raise WaveError(f"unexpected response shape from {safe_url(url)} (not a JSON object)")
    return out


def _api_root(m: Machine) -> str:
    return m.base_url.removesuffix("/v1") if m.provider == "ollama" else m.base_url.removesuffix("/v1") + "/v1"


def list_models(m: Machine, timeout: float = 3) -> list[dict]:
    """[{name, size_gb}] installed on the machine. Ids outside MODEL_ID are dropped (never written anywhere)."""
    key = "models" if m.provider == "ollama" else "data"
    data = _http("GET", _api_root(m) + ("/api/tags" if m.provider == "ollama" else "/models"), None, timeout, m.api_key)
    items = data.get(key)
    if not isinstance(items, list):
        raise WaveError(f"malformed model list from {safe_url(m.base_url)}: '{key}' is not a list")
    if m.provider == "ollama":
        rows = [(x.get("name") or x.get("model"), x.get("size")) for x in items if isinstance(x, dict)]
    else:
        rows = [(x.get("id"), 0) for x in items if isinstance(x, dict)]
    return [{"name": n, "size_gb": round((s or 0) / 1e9, 2) if isinstance(s, (int, float)) else 0.0}
            for n, s in rows if clean_model_id(n)]


@dataclass
class Reply:
    text: str
    truncated: bool
    served_model: str = ""      # the id the server says it ran (falls back to the id that was sent)


def chat(m: Machine, model: str, messages: list[dict], *, temperature: float, seed: int, max_tokens: int,
         timeout: float, json_mode: bool = False) -> Reply:
    """One completion, limited per host, within ONE overall deadline. Thinking blocks are stripped."""
    if not MODEL_ID.fullmatch(model):
        raise WaveError("refusing a model id with unexpected characters")
    deadline = time.monotonic() + timeout
    lim = host_lock(m)
    lim.acquire(deadline)                         # a queued call never waits past its deadline
    try:
        if m.provider == "ollama":
            body: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "think": False,
                                    "options": {"temperature": temperature, "seed": seed, "num_predict": max_tokens}}
            if json_mode:
                body["format"] = "json"
            try:
                data = _http("POST", _api_root(m) + "/api/chat", body, timeout, m.api_key, deadline)
            except HTTPFail as e:       # only an explicit "think is not supported" 400 gets one retry without it
                if not think_unsupported(e):
                    raise
                body.pop("think")
                data = _http("POST", _api_root(m) + "/api/chat", body, timeout, m.api_key, deadline)
            msg = data.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
                raise WaveError("unexpected Ollama reply shape (no message.content)")
            text, truncated = msg["content"], data.get("done_reason") == "length"
        else:
            body = {"model": model, "messages": messages, "temperature": temperature, "seed": seed,
                    "max_tokens": max_tokens, "stream": False}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            data = _http("POST", _api_root(m) + "/chat/completions", body, timeout, m.api_key, deadline)
            ch = data.get("choices")
            if not isinstance(ch, list) or not ch or not isinstance(ch[0], dict) or not isinstance(ch[0].get("message"), dict) \
                    or not isinstance(ch[0]["message"].get("content", ""), (str, type(None))):
                raise WaveError("unexpected OpenAI reply shape (no choices[0].message)")
            text, truncated = ch[0]["message"].get("content") or "", ch[0].get("finish_reason") == "length"
            served = data.get("model")
    finally:
        lim.release()
    if m.provider == "ollama":
        served = data.get("model")
    served = served if isinstance(served, str) and MODEL_ID.fullmatch(served) else model
    return Reply(THINK.sub("", text).strip(), truncated, served)


# --------------------------------------------------------------------------- model mapping

def _is_chat(name: str) -> bool:
    return not any(x in name.lower() for x in NOT_CHAT)


_TOK = re.compile(r"\d+(?:\.\d+)?b(?![a-z])|[a-z]+|\d+")


def _tokens(s: str) -> list[str]:
    return _TOK.findall(s.lower().split("/")[-1])


def matches(wanted: str, installed: str) -> bool:
    """Strict family + size match. `qwen3:4b` matches `qwen3:4b`, `qwen3:4b-q4_K_M` and
    `mlx-community/Qwen3-4B-Instruct-2507-4bit`, never `qwen3:14b` or `qwen3-coder:30b`. The family's tokens must be
    followed IMMEDIATELY by the size token (so `qwen2.5:7b` never matches `qwen2.5-coder:7b`)."""
    w, i = wanted.lower(), installed.lower()
    if i == w:
        return True
    fam, _, size = w.partition(":")
    ft, it = _tokens(fam), _tokens(i)
    if not size:                                   # bare family ("llama3.1") = that family's default tag only
        return i.split(":")[0] == w and i.split(":")[-1] in ("latest", w)
    st = _tokens(size)
    if not ft or len(st) < 1 or not re.fullmatch(r"\d+(?:\.\d+)?b", st[0]):
        return False
    need = ft + [st[0]]
    for k in range(len(it) - len(need) + 1):
        if it[k:k + len(need)] == need and all(not re.fullmatch(r"[a-z]+", t) or t in ("meta", "mlx", "community", "library")
                                                for t in it[:k]):
            return True
    return False


def find_installed(wanted: str, installed: list[str]) -> str | None:
    exact = [i for i in installed if i.lower() == wanted.lower()]
    if exact:
        return exact[0]
    return next((i for i in installed if matches(wanted, i)), None)


def plan_models(team: Team, installed: list[str]) -> list[dict]:
    """For each active bot: the exact installed id for its model, else the best installed model for its duty
    (remap), else None (pull)."""
    chat_models = [i for i in installed if _is_chat(i) and MODEL_ID.fullmatch(i)]
    out = []
    for b in team.active():
        have = find_installed(b.model, chat_models)
        if have:
            out.append({"bot": b.name, "model": b.model, "use": have, "action": "ok"})
            continue
        pick = next((x for w in PREFER.get(b.duty, []) if (x := find_installed(w, chat_models))), None)
        pick = pick or (chat_models[0] if chat_models else None)
        out.append({"bot": b.name, "model": b.model, "use": pick, "action": "remap" if pick else "pull"})
    return out


def resolve_models(team: Team, installed: dict[str, list[str]]) -> list[str]:
    """Bind every active bot to the exact id its machine reported. Returns problems (empty = runnable)."""
    problems = []
    for b in team.active():
        have = find_installed(b.model, [i for i in installed.get(b.machine, []) if MODEL_ID.fullmatch(i)])
        b.runtime_model = have or ""
        if not have:
            problems.append(f"[[{b.name}]] model {b.model} is not installed on [[{b.machine}]]")
    return problems


# --------------------------------------------------------------------------- vault writer (Guard)

DIRFD = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
STATE_FILES = ("permissions.yaml", "audit.jsonl", "audit.jsonl.lock", "sandbox", ".lc_scratch")


def _open_dir_nofollow(path: Path) -> int:
    """fd of a real folder (never a symlink); the folder is created if missing (its parent must exist)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise WaveError(f"{path.name} must be a real folder, not a symlink")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _write_at(dfd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    """Atomic write of `name` inside the open folder `dfd`: random exclusive temp file, never follows a symlink."""
    tmp = f".{name}.{os.urandom(6).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)     # replaces a symlink entry, never follows it
        os.fsync(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise


def _write_all(fd: int, data: bytes, write=None) -> None:
    """Write every byte or raise (no silent short writes)."""
    write = write or os.write                    # looked up per call (so it is the real os.write, or a test's stand-in)
    view = memoryview(data)
    while view:
        n = write(fd, view)
        if not n or n < 0:
            raise OSError("write made no progress")
        view = view[n:]


def _append_at(dfd: int, name: str, line: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        _write_all(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def check_state_tree(state: Path) -> None:
    """ssh-style check of the WHOLE state tree (Guard scratch included): no symlinks anywhere, every entry owned by
    this user, nothing group/world-writable. Raises WaveError naming the first bad entry (relative path)."""
    posix = hasattr(os, "geteuid")
    uid = os.geteuid() if posix else None
    stack = [state]
    while stack:
        cur = stack.pop()
        st = os.lstat(cur)
        rel = cur.relative_to(state).as_posix() if cur != state else "."
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise WaveError(f"state entry '{rel}' is a symlink; refusing to start")
        if posix and st.st_uid != uid:
            raise WaveError(f"state entry '{rel}' is not owned by you; refusing to start")
        if posix and st.st_mode & 0o022:
            raise WaveError(f"state entry '{rel}' is group/world-writable; refusing to start (chmod go-w)")
        if stat.S_ISDIR(st.st_mode):
            stack += [cur / n for n in sorted(os.listdir(cur))]


class VaultWriter:
    """Writes inside the vault only, through the Guard. Fail closed: a Guard that can't start is an error.
    The portable writer is used only when the Guard can't exist on this OS (no fcntl), or when forced by tests.
    State files (permissions.yaml, audit log) are opened without following symlinks; a symlinked state folder or
    state file is refused before anything is written."""

    def __init__(self, vault: Path, state_dir: Path | None = None, *, portable: bool = False):
        self.vault = vault.resolve()
        state = Path(os.path.abspath(state_dir or os.environ.get("WAVE_DISPATCH_STATE") or ROOT / ".wave_dispatch"))
        try:
            import fcntl  # noqa: F401
            posix = True
        except ImportError:
            posix = False
        self.state = state
        self._sfd: int | None = None
        try:
            if DIRFD:
                state.parent.mkdir(parents=True, exist_ok=True)
                self._sfd = _open_dir_nofollow(state)
                for name in STATE_FILES:          # a planted symlink here would redirect the Guard's own files
                    try:
                        st = os.lstat(name, dir_fd=self._sfd)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        raise WaveError(f"state file {name} is a symlink; refusing to start")
            else:
                if os.path.islink(state):
                    raise WaveError("state folder is a symlink; refusing to start")
                state.mkdir(parents=True, exist_ok=True)
            check_state_tree(state)               # every entry, Guard scratch included, BEFORE the Guard starts
        except OSError as e:
            raise WaveError(f"state folder unusable: {safe_text(e)}") from None
        if posix and not portable:
            from .guard import Guard
            try:
                perms = yaml.safe_dump({
                    "toggles": {"ALLOW_SHELL_EXEC": False, "ALLOW_NETWORK_FETCH": False, "ALLOW_WAVE_DISPATCH_VAULT": True},
                    "grants": {"ALLOW_WAVE_DISPATCH_VAULT": [{"path": str(self.vault), "mode": "rw", "shell": False}]},
                    "hard_deny": ["~/.ssh", "~/.gnupg", "~/.aws"]}).encode()
                old = os.umask(0o077)                 # whatever the Guard creates stays private (passes the next check)
                try:
                    _write_at(self._sfd, "permissions.yaml", perms)
                    self.guard = Guard(state / "sandbox", state / "permissions.yaml", state / "audit.jsonl")
                finally:
                    os.umask(old)
            except Exception as e:
                raise WaveError(f"the Guard could not start, nothing was written: {safe_text(e)}") from None
            self.kind = "wave_dispatch.guard"
        else:
            self.guard = None
            self.kind = "portable"

    def __del__(self):
        if getattr(self, "_sfd", None) is not None:
            try:
                os.close(self._sfd)
            except OSError:
                pass

    def _check_rel(self, rel: str) -> PurePosixPath:
        p = PurePosixPath(rel)
        if p.is_absolute() or not p.parts or any(x in ("..", "", ".") for x in p.parts) or "\\" in rel:
            raise WaveError(f"refusing to write outside the vault: {rel}")
        return p

    def write(self, rel: str, text: str, rationale: str) -> Path:
        p = self._check_rel(rel)
        target = self.vault.joinpath(*p.parts)
        if self.guard is not None:
            return self.guard.write_file(target, text, node="wave-dispatch", rationale=rationale)
        return self._portable(p, target, text, rationale)

    def _audit(self, entry: dict) -> None:
        line = json.dumps(entry) + "\n"
        if self._sfd is not None:
            _append_at(self._sfd, "audit.jsonl", line)
            return
        path = self.state / "audit.jsonl"           # no dir_fd on this OS (Windows): refuse a symlinked log
        if os.path.islink(path):
            raise WaveError("audit log is a symlink")
        with open(path, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _portable(self, p: PurePosixPath, target: Path, text: str, rationale: str) -> Path:
        data = text.encode()
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": "write_file", "target": str(target),
                 "rationale": rationale, "bytes": len(data), "status": "intent"}
        self._audit(entry)                            # write-ahead: BEFORE any folder or file is touched
        if DIRFD:
            fd = os.open(self.vault, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in p.parts[:-1]:             # pin each folder by fd: a swapped folder can't redirect the write
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    try:
                        nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except OSError as e:
                        raise WaveError(f"symlink or non-folder in path at '{part}'") from e
                    os.close(fd)
                    fd = nfd
                name = p.parts[-1]
                try:
                    st = os.lstat(name, dir_fd=fd)
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                        raise WaveError(f"target is a symlink or not a file: {p}")
                except FileNotFoundError:
                    pass
                _write_at(fd, name, data, 0o644)
            finally:
                os.close(fd)
        else:                                         # Windows: no dir_fd; lstat walk + reparse-point check (weaker)
            cur = self.vault
            for part in p.parts[:-1]:
                cur = cur / part
                if not os.path.lexists(cur):
                    os.mkdir(cur, 0o755)
                st = os.lstat(cur)
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
                    raise WaveError(f"symlink or non-folder in path at '{part}'")
            if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
                raise WaveError(f"target is a symlink or not a file: {p}")
            fd, tmp = tempfile.mkstemp(prefix=f".{p.parts[-1]}.", suffix=".tmp", dir=cur)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        self._audit({**entry, "status": "ok"})
        return target


def _frontmatter(props: dict) -> str:
    return "---\n" + yaml.safe_dump(props, sort_keys=False, allow_unicode=True) + "---\n"


def set_bot_model(team: Team, writer: VaultWriter, bot_name: str, model: str) -> None:
    """Point a bot note at another (installed, exact) model id; keeps the original in `starter_model` and makes sure a
    Model note exists."""
    if not MODEL_ID.fullmatch(model):
        raise WaveError("refusing a model id with unexpected characters")
    bot = next(b for b in team.bots if b.name == bot_name)
    note = bot.note
    assert note is not None
    props = dict(note.props)
    props.setdefault("starter_model", props.get("model"))
    old_link = f"[[{model_note_name(str(props['model']))}]]"
    props["model"] = model
    props["model_note"] = f"[[{model_note_name(model)}]]"
    body = note.body.replace(old_link, f"[[{model_note_name(model)}]]")
    writer.write(note.rel, _frontmatter(props) + body, f"Wave Dispatch setup: map {bot_name} to installed model")
    bot.model, bot.runtime_model = model, model
    ensure_model_note(team.vault, writer, model, bot_name)


def clean_model_id(value, fallback: str = "") -> str:
    """A model id a server reported, kept only if it matches MODEL_ID AND redaction leaves it unchanged (a token-shaped
    "name" is never written into notes, events or receipts: Astra review 47). Otherwise `fallback`."""
    if isinstance(value, str) and MODEL_ID.fullmatch(value) and safe_text(value) == value:
        return value
    return fallback


def model_note_name(model: str) -> str:
    """Obsidian/Windows-safe note name for a model id: qwen2.5-coder:7b -> qwen2.5-coder 7b."""
    return re.sub(r"[^A-Za-z0-9._ -]+", " ", model.split("/")[-1]).strip() or "model"


def ensure_model_note(vault: Path, writer: VaultWriter, model: str, used_by: str) -> None:
    if not MODEL_ID.fullmatch(model):
        raise WaveError("refusing a model id with unexpected characters")
    name = model_note_name(model)
    if (vault / "Models" / f"{name}.md").exists():
        return
    props = {"type": "model", "tag": model, "params": "unknown", "tags": ["wave-dispatch/model"]}
    writer.write(f"Models/{name}.md", _frontmatter(props) + f"# {name}\n\nInstalled model `{model}`, mapped by the "
                 f"setup helper.\n\nUsed by: [[Bots/{used_by}|{used_by}]] · Team: [[AI Bots]]\n",
                 "Wave Dispatch: Model note")


# --------------------------------------------------------------------------- the wave

def graph_context(vault: Path, seeds: list[str], depth: int = 2, max_notes: int = 8, max_chars: int = 6000) -> list[tuple[Note, str]]:
    """Breadth-first walk over [[wikilinks]] from the seed notes (deterministic). Bots/Machines/Runs/Logs/Models notes
    are not knowledge and are skipped. Returns (note, exact text given to the model); the joined text, headings
    included, never exceeds max_chars (the last note is cut if needed)."""
    idx = Index(vault)
    skip = ("Bots", "Machines", "Runs", "Logs", "Models")
    seen: set[str] = set()
    order: list[Note] = []
    frontier = [n for s in seeds if (n := idx.note(s))]
    for _ in range(depth + 1):
        nxt: list[Note] = []
        for n in frontier:
            if n.key in seen or len(order) >= max_notes:
                continue
            seen.add(n.key)
            if n.rel.split("/")[0] in skip:
                continue
            order.append(n)
            nxt += [x for t in n.links() if (x := idx.note(t))]
        frontier = nxt
    out, used = [], 0
    for n in order:
        # redact the WHOLE note first, then budget: a cut can never land inside a secret, and redaction growth
        # ("[REDACTED]" may be longer than what it replaced) is counted against max_chars
        block = safe_text(f"### [[{n.name}]]\n{n.body.strip()}")
        room = max_chars - used - (2 if out else 0)
        if room <= 0:
            break
        if len(block) > room:
            cut = room
            for mk in re.finditer(re.escape(redact.REDACTED), block):   # never split a redaction marker
                if mk.start() < cut < mk.end():
                    cut = mk.start()
            block = block[:cut]
        out.append((n, block))
        used += len(block) + (2 if len(out) > 1 else 0)
    return out


def _fence(text: str) -> str:
    """Model text inside a fence; backtick runs are defused so it can't close the fence."""
    return "```text\n" + text.replace("```", "`​``").replace("~~~", "~​~~") + "\n```"


def parse_verdict(text: str, agree: bool = False) -> dict | None:
    """The WHOLE reply must be one JSON object {"verdict": "PASS"|"FAIL", "issues": [str, ...]} (no prose, no extra
    keys); with agree=True (second opinion on) also "agree": true|false. Anything else = None (invalid)."""
    try:
        v = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    keys = {"verdict", "issues"} | ({"agree"} if agree else set())
    if not isinstance(v, dict) or set(v) != keys:
        return None
    if v["verdict"] not in ("PASS", "FAIL") or not isinstance(v["issues"], list) \
            or not all(isinstance(x, str) for x in v["issues"]):
        return None
    if agree and not isinstance(v["agree"], bool):
        return None
    out = {"verdict": v["verdict"], "issues": v["issues"][:20]}
    if agree:
        out["agree"] = v["agree"]
    return out


@dataclass
class WaveResult:
    run_id: str
    status: str             # VERIFIED | ANSWERED (checks pass, QA switched off) | UNVERIFIED | FAILED
    answer: str
    checks: dict[str, bool]
    steps: list[dict] = field(default_factory=list)
    run_note: str = ""
    receipt_note: str = ""
    error: str = ""
    seconds: float = 0.0
    digest: str = ""
    context: list | None = None   # [(vault path, exact text the Graph Reader got)]; None = the vault was never read
    mode: dict = field(default_factory=lambda: {"use_notes": True, "strict_qa": True, "second_opinion": False,
                                                "circuit_tool": False})
    circuits: list = field(default_factory=list)   # [{source, spec, result, error}] as in the receipt


MANIFEST_VERSION = 4          # 2 added "mode"; 3 adds second_opinion / circuit_tool, step "variant" and "circuits"
MODE_KEYS = {2: {"use_notes", "strict_qa"}, 3: {"use_notes", "strict_qa", "second_opinion", "circuit_tool"},
             4: {"use_notes", "strict_qa", "second_opinion", "circuit_tool", "refine"},
             5: {"use_notes", "split", "review"},      # 5 = the Wave Dispatch engine (dispatch_engine.py)
             6: {"use_notes", "split", "review"}}      # 6 = 5 + the final answer (security review 2026-11-06)
V6_CHECKS = {"all_bots_answered", "no_truncated_output", "sums_correct", "python_parses", "final_answer_present"}
VARIANTS = ("main", "followup", "second_opinion", "second_opinion_followup")


def manifest_json(canon: dict) -> str:
    """The exact bytes the receipt hash is computed over (also stored verbatim in the receipt)."""
    return json.dumps(canon, sort_keys=True, ensure_ascii=True)


def manifest_digest(canon: dict) -> str:
    return hashlib.sha256(manifest_json(canon).encode()).hexdigest()


MANIFEST_BLOCK = re.compile(r"^## Canonical input manifest\n+(`{3,})json\n(.*?)\n\1$", re.S | re.M)


_HEX64 = re.compile(r"[0-9a-f]{64}")
_STEP_TYPES = {"wave": str, "bot": str, "model": str, "runtime_model": str, "served_model": str, "machine": str,
               "seed": int, "temperature": (int, float), "truncated": bool, "output": str, "input_sha256": str}
_TEAM_TYPES = {"name": str, "duty": str, "wave": int, "model": str, "runtime_model": str, "temperature": (int, float),
               "seed": int, "machine": str, "instructions": str}


def validate_manifest(canon: Any) -> str:
    """'' if the manifest has exactly the expected shape and types, else the first problem."""
    keys = {"manifest", "prompt", "seeds", "team", "context", "steps", "checks", "status", "error"}
    if not isinstance(canon, dict):
        return "manifest is not an object"
    version = canon.get("manifest")
    if version not in (1, 2, 3, 4, 5, 6) or isinstance(version, bool):
        return "unsupported manifest version"
    if version >= 2:
        keys = keys | {"mode"}
    if version >= 3:
        keys = keys | {"circuits"}
    if version >= 6:
        keys = keys | {"answer"}
    if set(canon) != keys:
        return f"manifest keys differ: missing {sorted(keys - set(canon))}, extra {sorted(set(canon) - keys)}"
    mode = canon.get("mode", {"use_notes": True, "strict_qa": True})
    want = MODE_KEYS.get(version, MODE_KEYS[2])
    if not isinstance(mode, dict) or set(mode) != want or not all(isinstance(v, bool) for v in mode.values()):
        return f"mode must be {{{', '.join(sorted(want))}}} true/false"
    if version >= 3:
        if not isinstance(canon["circuits"], list) or (canon["circuits"] and len(canon["circuits"]) > _quantum().MAX_PER_RUN):
            return "circuits must be a short list"
        for c in canon["circuits"]:
            if not isinstance(c, dict) or set(c) != {"source", "spec", "result", "error"} \
                    or not isinstance(c["source"], str) or not isinstance(c["spec"], dict) \
                    or not isinstance(c["error"], str) or not (c["result"] is None) == bool(c["error"]) \
                    or not (c["result"] is None or isinstance(c["result"], dict)):
                return "circuit entry has the wrong shape"
    if not isinstance(canon["prompt"], str) or not isinstance(canon["error"], str):
        return "prompt/error must be strings"
    if version >= 6 and not isinstance(canon["answer"], str):
        return "answer must be a string"
    if canon["status"] not in ("VERIFIED", "ANSWERED", "UNVERIFIED", "FAILED") or \
            (canon["status"] == "ANSWERED" and version == 1):
        return "bad status"
    if not isinstance(canon["seeds"], list) or not all(isinstance(x, str) for x in canon["seeds"]):
        return "seeds must be a list of strings"
    if not isinstance(canon["checks"], dict) or not all(isinstance(k, str) and isinstance(v, bool) for k, v in canon["checks"].items()):
        return "checks must map names to true/false"
    step_types = {**_STEP_TYPES, "variant": str} if version >= 3 else _STEP_TYPES
    for field_, types in (("team", _TEAM_TYPES), ("steps", step_types)):
        rows = canon[field_]
        if not isinstance(rows, list):
            return f"{field_} must be a list"
        for r in rows:
            if not isinstance(r, dict) or set(r) != set(types):
                return f"{field_} entry has the wrong keys"
            for k, t in types.items():
                if not isinstance(r[k], t) or (t is int and isinstance(r[k], bool)):
                    return f"{field_}.{k} has the wrong type"
    if not isinstance(canon["context"], list):
        return "context must be a list"
    for c in canon["context"]:
        if not isinstance(c, dict) or set(c) != {"note", "file_sha256", "given"}:
            return "context entry has the wrong keys"
        if not isinstance(c["note"], str) or not isinstance(c["given"], str) or not isinstance(c["file_sha256"], str) \
                or not _HEX64.fullmatch(c["file_sha256"]):
            return "context entry has the wrong types"
        pp = PurePosixPath(c["note"])
        if pp.is_absolute() or ".." in pp.parts or not c["note"].endswith(".md"):
            return "context note path is not a vault-relative note"
    if version >= 6 and canon["status"] != "FAILED" and (not canon["team"] or not canon["steps"]
                                                        or not V6_CHECKS <= set(canon["checks"])):
        return "a run that was not FAILED needs its team, its steps and every check"   # no hand-made VERIFIED receipts
    passed = bool(canon["checks"]) and all(canon["checks"].values()) and not canon["error"]
    strict = mode["review"] if version >= 5 else mode["strict_qa"]   # v5: the review loop plays QA's part
    if canon["status"] == "VERIFIED" and not (passed and strict):
        return "status VERIFIED contradicts the checks or the mode"
    if canon["status"] == "ANSWERED" and not (passed and not strict):
        return "status ANSWERED contradicts the checks or the mode"
    return ""


def _final_answer_section(note: str) -> str | None:
    """The run note's ONE final-answer section, read like Markdown (security review 3): any heading level, trailing #s,
    fenced blocks skipped while looking for headings; None if there are 0 or 2+ such sections or an HTML comment
    outside a fence (it could hide one)."""
    lines, fence, heads = note.splitlines(), False, []
    for i, ln in enumerate(lines):
        if re.match(r"\s{0,3}(```|~~~)", ln):
            fence = not fence
            continue
        if fence:
            continue
        if "<!--" in ln:
            return None
        if re.match(r"\s{0,3}#{1,6}\s", ln):
            heads.append((i, bool(re.match(r"\s{0,3}#{1,6}\s+final\s+answer\s*#*\s*$", ln, re.I))))
    finals = [i for i, f in heads if f]
    if len(finals) != 1:
        return None
    nxt = next((i for i, _ in heads if i > finals[0]), len(lines))
    return "\n".join(lines[finals[0] + 1:nxt]).strip()


def verify_receipt(receipt: Path, vault: Path, min_version: int = 1) -> dict:
    """Recompute a receipt from disk. Never raises: {"hash_ok", "notes_ok", "changed": [...], "error"}. hash_ok needs a
    well-formed manifest (validate_manifest) whose sha256 equals the receipt's sha256 and whose status matches it;
    notes_ok needs every context note to still have its recorded full-file sha256."""
    out = {"hash_ok": False, "notes_ok": False, "changed": [], "error": "", "circuits_ok": True, "answer_ok": True}
    try:
        n = read_note(receipt, vault)
        m = MANIFEST_BLOCK.search(n.body)
        if not m:
            out["error"] = "no manifest block"
            return out
        try:
            canon = json.loads(m.group(2))
        except ValueError:
            out["error"] = "manifest is not valid JSON"
            return out
        problem = validate_manifest(canon)
        if problem:
            out["error"] = problem
            return out
        if canon["manifest"] < min_version:          # no downgrade to an older, weaker receipt format
            out["error"] = f"receipt format {canon['manifest']} is older than {min_version}"
            return out
        sha = n.props.get("sha256")
        if not isinstance(sha, str) or not _HEX64.fullmatch(sha):
            out["error"] = "receipt sha256 property missing or malformed"
            return out
        if n.props.get("status") != canon["status"]:
            out["error"] = "receipt status differs from its manifest"
            return out
        out["hash_ok"] = manifest_digest(canon) == sha and manifest_json(canon) == m.group(2)
        for c in canon["context"]:
            try:
                body = read_in_vault(vault, c["note"])       # fd-pinned, never follows a swapped-in symlink
            except WaveError:
                body = None
            if body is None or hashlib.sha256(body.encode()).hexdigest() != c["file_sha256"]:
                out["changed"].append(c["note"])
        out["notes_ok"] = not out["changed"]
        if "answer" in canon:                        # v6: the run note must show exactly the answer that was hashed
            m2 = re.match(r"\[\[([^\]|]+)", str(n.props.get("run") or ""))
            try:
                note = read_in_vault(vault, m2.group(1) + ".md") if m2 else None
            except WaveError:
                note = None
            out["answer_ok"] = note is not None and _final_answer_section(note) == _fence(canon["answer"] or "(none)").strip()
            if not out["answer_ok"]:
                out["changed"].append("final answer in the run note")
        for c in canon.get("circuits", []):          # every circuit result is recomputed from its spec
            if c["error"]:
                continue
            try:
                same = _quantum().run(c["spec"]) == c["result"]
            except _quantum().CircuitError:
                same = False
            out["circuits_ok"] = out["circuits_ok"] and same
    except Exception as e:                            # never crash a verifier: report it
        out["error"] = f"{type(e).__name__}: {safe_text(e)}"
        out["hash_ok"] = out["notes_ok"] = out["circuits_ok"] = False
    return out


def run_wave(prompt: str, *, team: Team, writer: VaultWriter | None = None, seeds: list[str] | None = None,
             max_tokens: int = 400, timeout: float = 120, briefing_words: int = 200,
             chat_fn: Callable[..., Reply] = chat, label: str = "run", graph_depth: int = 2,
             on_step: Callable[[str, Bot, Machine], None] | None = None,
             now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
             use_notes: bool = True, strict_qa: bool = True, second_opinion: bool = False,
             circuit_tool: bool = False, refine: bool = False) -> WaveResult:
    """use_notes=False: no vault read, the graph bots sit out, the builders answer from their own knowledge.
    strict_qa=False: the verifier bots sit out; a run whose other checks pass is ANSWERED (never VERIFIED).
    second_opinion=True: every build bot answers twice (direct, then edge cases first); the verifier says whether the
    two agree, and disagreement keeps the run from VERIFIED. circuit_tool=True: circuit notes the Graph Reader reaches
    and ```circuit blocks a build bot writes are simulated exactly by Python (_quantum().py); a bot that asked gets the
    numbers back for one follow-up answer, and every circuit and result is in the receipt.
    refine=True: the refine loops run after the builders: each `improve` bot rewrites the latest version (fix mistakes,
    fill gaps, cut what isn't supported), then the `finalize` bot writes the version the user gets. With a finalize bot
    its text IS the answer (the Router doesn't rewrite it); the verifier checks that final version."""
    started = time.monotonic()
    t0 = now()
    mode = {"use_notes": bool(use_notes), "strict_qa": bool(strict_qa), "second_opinion": bool(second_opinion),
            "circuit_tool": bool(circuit_tool), "refine": bool(refine)}
    circuits: list[dict] = []
    circuits_lock = threading.Lock()
    skip = ({"graph"} if not use_notes else set()) | ({"verify"} if not strict_qa else set()) \
        | (set(REFINE) if not refine else set())
    # SINGLE redaction point: every input is redacted once, here. The models get exactly this text and the manifest
    # records exactly this text (byte for byte), so a receipt never holds a secret the models didn't see redacted.
    prompt = safe_text(prompt)
    seeds = [safe_text(x) for x in (seeds or LINK.findall(prompt) or ["Wave Dispatch Premise"])] if use_notes else []
    instr = {b.name: safe_text(b.instructions or f"You are the {b.name}.") for b in team.bots}
    steps: list[dict] = []
    steps_lock = threading.Lock()
    ctx: list[tuple[Note, str]] = []
    answer, verdicts, error = "", {}, ""
    ctx_read = False

    def call(bot: Bot, wave: str, user: str, *, tokens: int = max_tokens, json_mode: bool = False,
             variant: str = "main") -> str:
        m = team.machine_for(bot)
        if on_step:
            on_step(wave, bot, m, variant)
        t = time.monotonic()
        messages = [{"role": "system", "content": instr[bot.name]}, {"role": "user", "content": user}]
        sent_sha = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        rep = chat_fn(m, bot.send_model, messages,
                      temperature=bot.temperature, seed=bot.seed, max_tokens=tokens, timeout=timeout, json_mode=json_mode)
        if not isinstance(rep, Reply):
            raise WaveError("connector returned an unexpected type")
        text = safe_text(rep.text)                    # redacted ONCE here: notes, hash and later waves all see this
        served = clean_model_id(rep.served_model, bot.send_model)
        with steps_lock:
            steps.append({"wave": wave, "bot": bot.name, "model": bot.model, "runtime_model": bot.send_model,
                          "served_model": served, "machine": m.name, "seed": bot.seed, "temperature": bot.temperature,
                          "truncated": rep.truncated, "seconds": round(time.monotonic() - t, 2), "output": text,
                          "input_sha256": sent_sha, "variant": variant})
        return text

    def simulate(source: str, spec_or_text) -> str | None:
        """Run one circuit (a parsed note spec, or a bot's raw ```circuit text) and record it; None when the per-run
        limit is reached. Returns the plain-text result block for the bots."""
        with circuits_lock:
            if len(circuits) >= _quantum().MAX_PER_RUN:
                return None
            slot = {"source": source, "spec": {}, "result": None, "error": ""}
            circuits.append(slot)
        try:
            spec = json.loads(spec_or_text) if isinstance(spec_or_text, str) else spec_or_text
            slot["spec"] = _quantum().parse(spec)
            slot["result"] = _quantum().run(slot["spec"])
        except (ValueError, TypeError, RecursionError) as e:   # CircuitError and bad JSON alike: recorded, not raised
            slot["spec"], slot["result"] = {}, None
            slot["error"] = safe_text(str(e))[:300] or "invalid circuit"
        return _quantum().describe(source, slot["result"], slot["error"])

    try:
        team.validate()
        router = team.by_duty("router")
        if use_notes:
            ctx = graph_context(team.vault, seeds, depth=graph_depth)   # already redacted + capped; used verbatim below
            ctx_read = True                             # [] now means "read, nothing found" (Astra review 48)
        ctx_text = "\n\n".join(block for _, block in ctx)
        named = {s.strip().removesuffix(".md") for s in seeds}   # only circuit notes NAMED in the prompt/seeds run
        note_circuits = [d for n, _ in ctx if circuit_tool and n.props.get("type") == "circuit"
                         and (n.name in named or n.key in named)
                         if (d := simulate(f"[[{safe_text(n.name)}]]",
                                           _quantum().from_note(n.props, safe_text(n.name)))) is not None]
        circ_text = ("\n\nCircuit results (exact, computed by Python, not by a model):\n" + "\n".join(note_circuits)) \
            if note_circuits else ""
        tool_help = ("\n\n" + _quantum().HELP) if circuit_tool else ""
        # The Router plans BEFORE anyone has read the vault, so it gets only the note titles and must not state facts
        # (live run 20261103-042330: a 4B Router invented "on-chain governance" and every later wave repeated it).
        titles = ", ".join(safe_text(n.name) for n, _ in ctx) or "(none found)"
        if use_notes:
            plan = call(router, "plan", f"Prompt:\n{prompt}\n\nVault notes the graph readers will read: {titles}\n\n"
                                        "You have NOT read these notes. Do not answer the prompt and do not state any facts. "
                                        "In ONE sentence, say which topics the graph readers should look for and what form "
                                        "the builders' answer should take.", tokens=80)
        else:
            plan = call(router, "plan", f"Prompt:\n{prompt}\n\nNo vault notes are used for this run: the builders "
                                        "answer from their own knowledge. Do not answer the prompt and do not state any "
                                        "facts. In ONE sentence, say what form the builders' answer should take.", tokens=80)
        no_notes = "(none: vault notes are switched off for this run)"
        briefings: list[str] = []
        builds: list[tuple[str, str]] = []
        versions: list[tuple[str, str]] = []          # refine loops: (bot, text), each the newest full answer
        for wave_no, all_bots in team.waves():
            bots = [b for b in all_bots if b.duty not in skip]
            if not bots:
                continue
            def build(b: Bot, variant: str) -> tuple[Bot, str, str]:
                prior = "\n\n".join(f"[{label}]\n{o}" for label, o in builds)
                brief = chr(10).join(briefings) if use_notes else no_notes
                ask = (f"Prompt:\n{prompt}\n\nRouter plan: {plan}\n\nBriefing:\n{brief}{circ_text}\n\n"
                       + (f"Earlier build output:\n{prior}\n\n" if prior else "")
                       + ("Solve the prompt." if use_notes else "Solve the prompt from your own knowledge. "
                          "If you are not sure of a fact, say so instead of guessing.")
                       + (" Second opinion: solve it again independently. Start from the edge cases and constraints "
                          "(what could go wrong, what the prompt rules out), then give your answer."
                          if variant == "second_opinion" else "") + tool_help)
                out = call(b, str(wave_no), ask, variant=variant)
                if circuit_tool:
                    asked = [d for raw in _quantum().requests(out)
                             if (d := simulate(f"{b.name}" + (" (second opinion)" if variant != "main" else ""), raw))]
                    if asked:                         # ONE follow-up: the bot gets the exact numbers and answers
                        out = call(b, str(wave_no), ask + f"\n\nYour first reply:\n{out}\n\nCircuit results (exact, "
                                   "computed by Python):\n" + "\n".join(asked) + "\n\nNow give your final answer "
                                   "using these exact numbers. Do not write another circuit block.",
                                   variant="followup" if variant == "main" else "second_opinion_followup")
                return b, variant, out

            def refine_ask(b: Bot, brief: str, outs: str) -> str:
                if versions:
                    src = f"Current version (from {versions[-1][0]}):\n{versions[-1][1]}"
                else:
                    src = ("Drafts from the builders" + (" (each answered twice: direct and a second opinion; "
                           "reconcile them)" if second_opinion else "") + f":\n{outs}")
                head = f"Prompt:\n{prompt}\n\nBriefing:\n{brief}{circ_text}\n\n{src}\n\n"
                if b.duty == "improve":
                    return head + ("Improve this answer: fix mistakes, fill in anything the prompt asks for that is "
                                   "missing, and cut anything the briefing does not support. Do not add new facts you "
                                   "are not sure of. Reply with the full improved answer only, no comments about the "
                                   "changes.")
                return head + ("Write the final version for the user: correct, complete, clear and concise. Keep every "
                               "correct fact and any code exactly working. Reply with the final answer only.")

            def one(b: Bot) -> tuple[Bot, str]:
                if b.duty == "graph":
                    return b, call(b, str(wave_no), f"Prompt:\n{prompt}\n\nVault notes (from the "
                                   f"[[wikilink]] graph):\n{ctx_text or '(none found)'}\n\nWrite a context briefing of at "
                                   f"most {briefing_words} words. Only facts from these notes.",
                                   tokens=min(max_tokens, int(briefing_words * 1.6)))
                outs = "\n\n".join(f"[{label}]\n{o}" for label, o in builds)
                brief = chr(10).join(briefings) if use_notes else no_notes
                if b.duty in REFINE:
                    return b, call(b, str(wave_no), refine_ask(b, brief, outs))
                if versions:                          # the verifier checks what the user will get
                    outs = (f"{outs}\n\n" if second_opinion else "") \
                        + f"Final answer (after the refine loops):\n{versions[-1][1]}"
                if second_opinion:
                    shape = ('{"verdict": "PASS" or "FAIL", "issues": ["..."], "agree": true or false}  ("agree": do '
                             'the direct answer and the second opinion reach the same result?)')
                else:
                    shape = '{"verdict": "PASS" or "FAIL", "issues": ["..."]}'
                with circuits_lock:                   # every circuit so far (notes AND the builders' requests)
                    ran = [_quantum().describe(c["source"], c["result"], c["error"]) for c in circuits]
                ran_text = ("\n\nCircuit results (exact, computed by Python, not by a model):\n" + "\n".join(ran)) \
                    if ran else ""
                return b, call(b, str(wave_no), f"Prompt:\n{prompt}\n\nBriefing:\n{brief}{ran_text}\n\n"
                               f"Specialist output:\n{outs}\n\nReply with ONLY this JSON object and nothing else: "
                               + shape, tokens=200, json_mode=True)
            # bots in one wave run concurrently (each host's limiter still applies); results kept in note order
            jobs = [(b, v) for b in bots for v in (("main", "second_opinion") if second_opinion and b.duty == "build"
                                                   else ("main",))]
            def job(j: tuple[Bot, str]) -> tuple[Bot, str, str]:
                b, variant = j
                return build(b, variant) if b.duty == "build" else (b, variant, one(b)[1])
            with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
                results = list(ex.map(job, jobs))
            for b, variant, out in results:
                if b.duty == "graph":
                    briefings.append(out)
                elif b.duty == "build":
                    builds.append((b.name + (" · second opinion" if variant == "second_opinion" else ""), out))
                elif b.duty in REFINE:
                    versions.append((b.name, out))
                else:
                    verdicts[b.name] = parse_verdict(out, agree=second_opinion)
        vtext = (json.dumps({k: v for k, v in verdicts.items()}) if verdicts else "(none)") if strict_qa \
            else "(QA check switched off for this run)"
        final = [t for n, t in versions if any(x.name == n and x.duty == "finalize" for x in team.bots)]
        if final:                                     # the finalizer's version IS the answer (one wave, one bot)
            answer = final[-1]
        else:
            answer = call(router, "synthesis", f"Prompt:\n{prompt}\n\nSpecialist output:\n"
                          + "\n\n".join(f"[{n}]\n{o}" for n, o in (versions[-1:] or builds))
                          + f"\n\nVerifier verdicts: {vtext}\n\n"
                          + ("Each specialist answered twice (direct, and a second opinion from the edge cases). Keep "
                             "what both agree on and say plainly where they disagree.\n\n"
                             if second_opinion and not versions else "")
                          + "Write the final answer for the user.")
    except Exception as e:                            # ANY failure ends as FAILED with a receipt
        error = safe_text(f"{type(e).__name__}: {e}" if not isinstance(e, WaveError) else e)

    active = [b for b in team.active() if b.duty not in skip] if team.bots else []
    steps.sort(key=lambda s: ({"plan": -1, "synthesis": 10**9}.get(s["wave"], None) or int(s["wave"]), s["bot"],
                              VARIANTS.index(s["variant"])))
    circuits.sort(key=lambda c: (c["source"], json.dumps(c["spec"], sort_keys=True), c["error"]))
    briefs = [s["output"] for s in steps if any(b.name == s["bot"] and b.duty == "graph" for b in active)]
    answered = {s["bot"] for s in steps if s["output"]}
    checks = {
        "all_bots_answered": not error and all(b.name in answered for b in active),
        "no_truncated_output": bool(steps) and not any(s["truncated"] for s in steps),
    }
    if use_notes:                                     # checks for a part that was switched off are left out, not faked
        checks["graph_context_found"] = bool(ctx)
        checks["briefing_within_limit"] = bool(briefs) and all(len(x.split()) <= briefing_words for x in briefs)
    if strict_qa:
        checks["verifier_json_valid"] = bool(verdicts) and all(v is not None for v in verdicts.values())
        checks["verifier_pass"] = bool(verdicts) and all(v is not None and v["verdict"] == "PASS" for v in verdicts.values())
    if second_opinion and strict_qa:                  # the verifier's judgement, like verifier_pass
        checks["second_opinion_agrees"] = bool(verdicts) and all(v is not None and v.get("agree") is True
                                                                 for v in verdicts.values())
    if circuits:
        checks["circuits_ran"] = all(not c["error"] for c in circuits)
    checks["final_answer_present"] = bool(answer) and not error
    status = "FAILED" if error else ("UNVERIFIED" if not all(checks.values()) else "VERIFIED" if strict_qa else "ANSWERED")
    team_cfg = [{**{k: getattr(b, k) for k in ("name", "duty", "wave", "model", "runtime_model", "temperature", "seed",
                                                "machine")}, "instructions": instr[b.name]} for b in active]
    canon = {"manifest": MANIFEST_VERSION, "prompt": prompt, "seeds": seeds, "team": team_cfg,
             "context": [{"note": n.rel, "file_sha256": hashlib.sha256(n.text.encode()).hexdigest(), "given": block}
                         for n, block in ctx],
             "steps": [{k: v for k, v in s.items() if k != "seconds"} for s in steps], "checks": checks,
             "status": status, "error": error, "mode": mode, "circuits": circuits}
    digest = manifest_digest(canon)
    run_id = f"{t0.strftime('%Y%m%d-%H%M%S')}-{digest[:8]}"
    res = WaveResult(run_id, status, answer, checks, steps, error=error,
                     seconds=round(time.monotonic() - started, 2), digest=digest,
                     context=[(n.rel, b) for n, b in ctx] if ctx_read else None, mode=mode, circuits=circuits)
    if writer is not None:
        _write_notes(res, writer, team, prompt, ctx, verdicts, canon, t0, label)
    return res


def _write_notes(res: WaveResult, writer: VaultWriter, team: Team, prompt: str, ctx: list[tuple[Note, str]],
                 verdicts: dict, canon: dict, t0: datetime, label: str) -> None:
    run_name, receipt_name = f"Run {res.run_id}", f"Receipt {res.run_id}"
    run_l, rec_l = f"[[Runs/{run_name}|{run_name}]]", f"[[Logs/{receipt_name}|{receipt_name}]]"
    bot_l = lambda b: f"[[Bots/{b}|{b}]]"
    mach_l = lambda m: f"[[Machines/{m}|{m}]]"
    model_l = lambda m: f"[[Models/{model_note_name(m)}|{model_note_name(m)}]]" if MODEL_ID.fullmatch(m) else "(model)"
    bots = sorted({s["bot"] for s in res.steps} | {b.name for b in team.active()})
    machines = sorted({s["machine"] for s in res.steps}) or sorted(m for m, x in team.machines.items() if x.enabled)
    served = sorted({s["served_model"] for s in res.steps if MODEL_ID.fullmatch(s["served_model"])})
    requested = sorted({s["model"] for s in res.steps})
    for s in res.steps:                               # Model notes are for the models that actually ran
        if MODEL_ID.fullmatch(s["served_model"]):
            ensure_model_note(team.vault, writer, s["served_model"], s["bot"])
    meta = {"type": "run", "label": label, "run_id": res.run_id, "status": res.status, "started": t0.isoformat(timespec="seconds"),
            "seconds": res.seconds, "bots": [bot_l(b) for b in bots], "machines": [mach_l(m) for m in machines],
            "models_run": [model_l(m) for m in served], "models_requested": requested,
            "receipt": rec_l, "use_notes": res.mode["use_notes"], "strict_qa": res.mode["strict_qa"],
            "second_opinion": res.mode.get("second_opinion", False), "circuit_tool": res.mode.get("circuit_tool", False),
            "refine": res.mode.get("refine", False),
            "circuits": len(canon.get("circuits", [])), "tags": ["wave-dispatch/run"]}
    body = [f"# {run_name}\n", f"Team: [[AI Bots]] · Wave: [[Small Brain Wave]] · Receipt: {rec_l}\n",
            "## Prompt", _fence(safe_text(prompt)), "",
            "## Mode", f"Vault notes: {'on' if res.mode['use_notes'] else 'OFF (answered from the models’ own knowledge)'}"
            f" · QA check: {'on' if res.mode['strict_qa'] else 'OFF (not checked)'}"
            f" · Second opinion: {'on' if res.mode.get('second_opinion') else 'off'}"
            f" · Circuit tool: {'on' if res.mode.get('circuit_tool') else 'off'}"
            f" · Refine loops: {'on (draft → improve → finalize)' if res.mode.get('refine') else 'off'}", "",
            "## Graph context (given to the graph readers)",
            (", ".join(f"[[{n.key}|{n.name}]]" for n, _ in ctx) or "(none)") if res.mode["use_notes"] else "(not used: notes switched off)", ""]
    if canon.get("circuits"):
        body += ["## Circuits (simulated exactly by Python; recomputed by --verify-receipt)"]
        for c in canon["circuits"]:
            body += [_fence(_quantum().describe(c["source"], c["result"], c["error"])
                            + ("\n" + json.dumps(c["spec"], sort_keys=True) if c["spec"] else ""))]
        body += [""]
    tag = {"followup": " · after circuit results", "second_opinion": " · second opinion",
           "second_opinion_followup": " · second opinion, after circuit results"}
    for s in res.steps:
        body += [f"## Wave {s['wave']} · {bot_l(s['bot'])}{tag.get(s.get('variant', 'main'), '')} · "
                 f"{model_l(s['served_model'])} on {mach_l(s['machine'])}",
                 f"requested `{s['model']}` · sent `{s['runtime_model']}` · served `{s['served_model']}` · "
                 f"seed {s['seed']} · temperature {s['temperature']} · {s['seconds']} s"
                 + (" · TRUNCATED" if s["truncated"] else ""), _fence(s["output"]), ""]
    if res.error:
        body += ["## Error", _fence(res.error), ""]
    writer.write(f"Runs/{run_name}.md", _frontmatter(meta) + "\n".join(body), f"Wave Dispatch {label}: run note")
    vv = [v["verdict"] if v else "INVALID" for v in verdicts.values()]
    rmeta = {"type": "receipt", "run": run_l, "run_id": res.run_id, "status": res.status,
             "verifier_verdict": ("NONE" if not vv else vv[0] if len(set(vv)) == 1 else "MIXED"), "sha256": res.digest,
             "checks": res.checks, "context_sha256": {c["note"]: c["file_sha256"] for c in canon["context"]},
             "models": [model_l(m) for m in served], "models_requested": requested,
             "executed": [{"wave": s["wave"], "bot": s["bot"], "requested": s["model"], "sent": s["runtime_model"],
                           "served": s["served_model"], "seed": s["seed"]} for s in res.steps],
             "bots": [bot_l(b) for b in bots],
             "guard": writer.kind, "use_notes": res.mode["use_notes"], "strict_qa": res.mode["strict_qa"],
             "second_opinion": res.mode.get("second_opinion", False), "circuit_tool": res.mode.get("circuit_tool", False),
            "refine": res.mode.get("refine", False),
             "tags": ["wave-dispatch/receipt"]}
    rbody = [f"# {receipt_name}\n", f"Run: {run_l} · Verifier: " + (", ".join(bot_l(b) for b in verdicts) or "none")
             + " · Index: [[Logs/Logs|Logs]]\n",
             f"**Status: {res.status}** (deterministic checks computed by Python; the model's verdict is one input)\n",
             "| check | result |", "|---|---|"] + [f"| {k} | {'pass' if v else 'FAIL'} |" for k, v in res.checks.items()]
    issues = [f"{b}: {i}" for b, v in verdicts.items() if v for i in v["issues"]]
    if issues:
        rbody += ["", "## Verifier issues", _fence("\n".join(issues))]
    if res.error:
        rbody += ["", "## Error", _fence(res.error)]
    mj = manifest_json(canon)
    fence = "`" * max(3, max((len(r) for r in re.findall(r"`+", mj)), default=0) + 1)
    rbody += ["", f"sha256 of the manifest below (prompt, team settings, each context note's full-file sha256 and the "
              f"exact excerpt given to the model, every wave output with the model ids that ran, the checks): `{res.digest}`",
              f"Recompute it: `python3 wave_dispatch_setup.py --verify-receipt \"{writer.vault.name}/Logs/{receipt_name}.md\"`", "",
              "## Canonical input manifest", "", f"{fence}json", mj, fence, ""]
    writer.write(f"Logs/{receipt_name}.md", _frontmatter(rmeta) + "\n".join(rbody), f"Wave Dispatch {label}: receipt")
    res.run_note, res.receipt_note = f"Runs/{run_name}.md", f"Logs/{receipt_name}.md"


# --------------------------------------------------------------------------- compare: the team vs one model

SOLO_SYSTEM = ("You are a single general-purpose model answering on your own. Use the vault notes you are given as "
               "context. Answer the prompt directly and concisely. Never invent facts that are not in the notes.")
SOLO_SYSTEM_NO_NOTES = ("You are a single general-purpose model answering on your own, from your own knowledge. Answer "
                        "the prompt directly and concisely. If you are not sure of a fact, say so instead of guessing.")


@dataclass
class SoloResult:
    model: str              # the exact installed id that was sent
    machine: str
    answer: str = ""
    served_model: str = ""
    truncated: bool = False
    seconds: float = 0.0
    error: str = ""
    context: list[str] = field(default_factory=list)   # vault paths of the notes it was given (the team's notes)


def run_solo(prompt: str, *, team: Team, model: str, machine: str, seeds: list[str] | None = None,
             max_tokens: int = 400, timeout: float = 120, graph_depth: int = 2,
             chat_fn: Callable[..., Reply] = chat, context: list | None = None,
             use_notes: bool = True) -> SoloResult:
    """The baseline for a comparison: ONE model, ONE call, the SAME redacted prompt and the SAME vault notes the
    Graph Reader got. `context` = the team's exact [(path, text)] (WaveResult.context): reused as-is, never re-read, so
    an edit during the team's run can't give the two different notes (Astra review 47). Without it (the team failed
    before reading the vault) the notes are read now. use_notes=False (the team's notes switch was off): no notes at
    all, the model answers from its own knowledge, like the team did. Python checks nothing here. Never raises."""
    started = time.monotonic()
    out = SoloResult(model=model, machine=machine)
    try:
        if not MODEL_ID.fullmatch(model or ""):
            raise WaveError("solo model id has unexpected characters")
        m = team.machines.get(machine)
        if m is None or not m.enabled:
            raise WaveError(f"machine {machine!r} is not defined or not enabled")
        prompt = safe_text(prompt)                    # the same single redaction point as run_wave
        seeds = [safe_text(x) for x in (seeds or LINK.findall(prompt) or ["Wave Dispatch Premise"])]
        if not use_notes:
            pairs = []
        elif context is not None:                     # the team's snapshot, even an EMPTY one (Astra 48)
            pairs = list(context)
        else:
            pairs = [(n.rel, b) for n, b in graph_context(team.vault, seeds, depth=graph_depth)]
        out.context = [rel for rel, _ in pairs]
        ctx_text = "\n\n".join(block for _, block in pairs)
        if use_notes:
            messages = [{"role": "system", "content": SOLO_SYSTEM},
                        {"role": "user", "content": f"Prompt:\n{prompt}\n\nVault notes (from the [[wikilink]] graph):\n"
                                                    f"{ctx_text or '(none found)'}"}]
        else:
            messages = [{"role": "system", "content": SOLO_SYSTEM_NO_NOTES},
                        {"role": "user", "content": f"Prompt:\n{prompt}"}]
        rep = chat_fn(m, model, messages, temperature=0.2, seed=7, max_tokens=max_tokens, timeout=timeout)
        if not isinstance(rep, Reply):
            raise WaveError("connector returned an unexpected type")
        out.answer = safe_text(rep.text)
        out.truncated = rep.truncated
        out.served_model = clean_model_id(rep.served_model, model)
    except Exception as e:                            # a comparison never crashes the panel: report it
        out.error = safe_text(f"{type(e).__name__}: {e}" if not isinstance(e, WaveError) else str(e))
    out.seconds = round(time.monotonic() - started, 2)
    return out


def write_compare_note(writer: VaultWriter, res: WaveResult, solo: SoloResult, prompt: str) -> str:
    """Runs/Compare <run id>.md: the team's answer and the single model's answer side by side, linked to the run note
    and receipt. `winner` is left for the reader to fill in (team | solo | tie). Returns the vault path."""
    name = f"Compare {res.run_id}"
    run_l = f"[[Runs/Run {res.run_id}|Run {res.run_id}]]" if res.run_note else "(no run note)"
    rec_l = f"[[Logs/Receipt {res.run_id}|Receipt {res.run_id}]]" if res.receipt_note else "(no receipt)"
    solo_id = clean_model_id(solo.served_model) or clean_model_id(solo.model) or "(model)"
    solo_l = f"[[Models/{model_note_name(solo_id)}|{model_note_name(solo_id)}]]" if clean_model_id(solo_id) else "(model)"
    if clean_model_id(solo_id) and not solo.error:
        ensure_model_note(writer.vault, writer, solo_id, "Router Brain")
    meta = {"type": "compare", "run_id": res.run_id, "run": run_l, "receipt": rec_l,
            "team_status": res.status, "team_seconds": res.seconds,
            "solo_model": solo_l, "solo_model_id": solo_id, "solo_seconds": solo.seconds,
            "solo_status": "ERROR" if solo.error else ("TRUNCATED" if solo.truncated else "UNCHECKED"),
            "use_notes": res.mode["use_notes"], "strict_qa": res.mode["strict_qa"],
            "winner": "", "tags": ["wave-dispatch/compare"]}
    given = "given the same vault notes" if res.mode["use_notes"] else "both from their own knowledge (notes switched off)"
    checked = ("Python checked the result (see the receipt)." if res.mode["strict_qa"]
               else "QA check switched off: Python checked only that every bot answered in full (see the receipt).")
    if res.circuits:
        checked += (f" The team used the circuit tool ({len(res.circuits)} circuit(s) simulated exactly by Python); the "
                    "single model has no tools.")
    if res.mode.get("second_opinion"):
        checked += " Each builder answered twice (second opinion)."
    body = [f"# {name}\n",
            f"The same prompt, two ways: the Wave Dispatch team ({run_l}, {rec_l}) and one model on its own ({solo_l}), "
            f"{given}. Fill in `winner` (team, solo or tie) once you have read both. "
            f"Index: [[Runs/Runs|Runs]]\n",
            "## Prompt", _fence(safe_text(prompt)), "",
            f"## Wave Dispatch team · {res.status} · {res.seconds} s",
            f"Small models in waves; {checked}",
            _fence(res.answer or res.error or "(no answer)"), "",
            f"## One model · {solo_l} · {solo.seconds} s" + (" · TRUNCATED" if solo.truncated else ""),
            "One call, no checks.",
            _fence(solo.answer or solo.error or "(no answer)"), "",
            "## Notes both were given", ", ".join(f"[[{p.removesuffix('.md')}|{Path(p).stem}]]" for p in solo.context) or "(none)", ""]
    writer.write(f"Runs/{name}.md", _frontmatter(meta) + "\n".join(body), "Wave Dispatch compare: comparison note")
    return f"Runs/{name}.md"
