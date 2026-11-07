"""Write-ahead audit log and sandbox guard.

Every side effect an agent can cause (file write, file read outside the sandbox, shell
command, network connection, or any future action) goes through ``Guard``. For each one:

1. Under the admission lock: kill-switch/fault check, permission check, and the intent entry
   is appended to ``audit_trail.jsonl`` and fsynced. If that write fails, ``AuditWriteError``
   is raised and the action does NOT run. ``halt()`` takes the same lock. Operator controls
   (toggles, task controls) also COMMIT under that lock, so nothing widened can land after
   a halt; a sandboxed process is spawned and registered under it too.
2. The action runs. Shell commands run under a deny-by-default macOS ``sandbox-exec``
   profile. Each run's profile can read one unique marker file, so every process the run
   creates (even after setsid/chdir/closing every descriptor) is found by asking macOS which
   processes' sandboxes allow that marker, and killed when the run ends, on halt, or when
   one of the run's capabilities is revoked. Network goes only through the Guard's broker
   (netbroker.py), which logs each connection first and only reaches public addresses.
3. The outcome is appended. If THAT fails, the action already happened: the guard latches
   a fault that ``resume()`` cannot clear, and ``ActionUnrecorded`` is raised.

Halt, hard faults and actions with no recorded outcome survive a restart: the Guard rebuilds
that state from the audit log before it admits anything.

File actions open every path component relative to a trusted directory with O_NOFOLLOW,
so a sandboxed process can't swap a checked folder for a symlink between check and use.
"""
from __future__ import annotations

import contextvars
import ctypes
import errno
import fcntl
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from . import redact

SHELL = "ALLOW_SHELL_EXEC"
NETWORK = "ALLOW_NETWORK_FETCH"
MAX_OUTPUT = 20_000          # characters of shell/fetch output returned to the agent
MAX_WRITE = 5_000_000        # bytes per write_file
MAX_CAPTURE = 4_000_000      # bytes per output stream before a command is killed
MAX_READ = 2_000_000         # bytes per read_file / fetch body

# Secrets that no agent action or sandboxed process may ever read or write, whatever
# permissions.yaml says (the YAML can add to this list, never remove from it).
# With the keychain folders denied and /usr/bin/security unrunnable (DENY_EXEC), sandboxed code can't read
# secrets stored in the keychain.
BUILTIN_DENY = ["~/.config/wave_dispatch", "~/Library/Keychains", "/Library/Keychains"]
# Programs sandboxed commands may not run at all (security = Keychain access).
DENY_EXEC = ["/usr/bin/security"]

SANDBOX_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Deny-by-default sandbox: programs, libraries and shared data a command may read. Nothing
# user-specific (no /Users, /private/var/folders, /private/tmp, preferences or keychains).
SYSTEM_READ = [
    "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/libexec", "/usr/share",
    "/System/Library", "/System/Cryptexes", "/Library/Apple/usr",
    "/Library/Frameworks/Python.framework", "/Library/Developer/CommandLineTools", "/usr/local/bin",
    "/opt/homebrew/bin", "/opt/homebrew/Cellar", "/opt/homebrew/lib", "/opt/homebrew/opt",
    "/opt/homebrew/share", "/opt/homebrew/libexec", "/opt/homebrew/Frameworks",
    "/private/var/db/timezone", "/private/etc/ssl",
    "/opt/homebrew/etc/openssl@3", "/opt/homebrew/etc/openssl@4",          # TLS config/CA data only
    "/opt/homebrew/etc/ca-certificates",
]
SYSTEM_EXEC = [
    "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/libexec", "/Library/Frameworks/Python.framework",
    "/Library/Developer/CommandLineTools", "/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/Cellar",
    "/opt/homebrew/opt", "/opt/homebrew/libexec",
]
SYSTEM_FILES = [
    "/private/etc/zshenv", "/private/etc/zshrc", "/private/etc/localtime", "/private/etc/passwd",
    "/private/etc/group", "/private/etc/hosts", "/private/etc/services", "/private/etc/protocols",
    "/private/etc/gitconfig", "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper",
]
# Mach services: user/group lookup and logging only. No preferences, keychain/SecurityServer,
# DNS, LaunchServices, Apple Events or window server.
MACH_ALLOW = ["com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
              "com.apple.system.logger", "com.apple.logd"]
REAP_PASSES = 5


class GuardError(Exception):
    """Base class for everything the guard refuses."""


class AuditWriteError(GuardError):
    """An audit entry could not be written. Raised before an action: it did not run."""


class ActionUnrecorded(AuditWriteError):
    """The action RAN but its outcome could not be logged. The guard is now faulted."""


class PermissionDenied(GuardError, PermissionError):
    """The action is outside what the current toggles allow."""


class DuplicateWrite(PermissionDenied):
    """A run tried to write a sandbox file it already wrote (per-run write-once lock)."""
    def __init__(self, message: str, path: Path):
        super().__init__(message)
        self.path = path


class Halted(GuardError):
    """The kill switch is on (or the guard is faulted); no actions run."""


class ScopeCancelled(GuardError):
    """The run this action belongs to was stopped (e.g. a company run); nothing more of it runs."""


# The run an action belongs to (e.g. "company:<clipboard id>"). Set inside an asyncio task, it is
# copied into asyncio.to_thread workers, so every Guard action that run causes carries it.
current_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("wave_dispatch_scope", default=None)
MAX_CANCELLED_SCOPES = 1000


# ---------------------------------------------------------------- audit log

class AuditLog:
    """Append-only JSONL file. Each append is fsynced before it returns. A failed append
    is rolled back so the file never keeps a partial line. One process owns the file (an
    exclusive lock held for the process lifetime); Guards in the same process share one
    writer lock. Lines are scrubbed of secrets (redact.py) before they are written."""

    _owners: dict[str, tuple[int, threading.Lock, int]] = {}
    _owners_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)
        key = os.path.realpath(self.path)
        with AuditLog._owners_lock:
            if key in AuditLog._owners:
                fd, lock, n = AuditLog._owners[key]
                AuditLog._owners[key] = (fd, lock, n + 1)
            else:
                lock = threading.Lock()
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                except OSError:
                    fd = None                 # unusable location: every append will fail (fail closed)
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(fd)
                        raise AuditWriteError(f"another Wave Dispatch process is writing {self.path}") from None
                AuditLog._owners[key] = (fd, lock, 1)
        self._key = key
        self._lock = lock
        self._repair_tail()
        self._seq = self._last_seq()
        self._listeners: list[Callable[[dict], None]] = []

    def close(self) -> None:
        with AuditLog._owners_lock:
            if self._key not in AuditLog._owners:
                return
            fd, lock, n = AuditLog._owners[self._key]
            if n > 1:
                AuditLog._owners[self._key] = (fd, lock, n - 1)
            else:
                del AuditLog._owners[self._key]
                if fd is not None:
                    os.close(fd)

    def _repair_tail(self) -> None:
        """Drop a trailing partial line left by a crash mid-write, however long it is.
        Refuses to start (AuditWriteError) if the file can't be repaired."""
        try:
            with self.path.open("rb+") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return
                f.seek(size - 1)
                if f.read(1) == b"\n":
                    return
                pos = size
                while pos > 0:
                    step = min(65536, pos)
                    pos -= step
                    f.seek(pos)
                    cut = f.read(step).rfind(b"\n")
                    if cut >= 0:
                        f.truncate(pos + cut + 1)
                        f.flush()
                        os.fsync(f.fileno())
                        return
                f.truncate(0)                       # the whole file is one partial line
                os.fsync(f.fileno())
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as e:
            raise AuditWriteError(f"audit log {self.path} has a damaged tail and could not be repaired: {e}") from e

    def _last_seq(self) -> int:
        """Sequence number of the last complete record (scans back past huge lines)."""
        try:
            with self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                pos, tail = f.tell(), b""
                while pos > 0:
                    step = min(65536, pos)
                    pos -= step
                    f.seek(pos)
                    tail = f.read(step) + tail
                    lines = tail.split(b"\n")
                    for line in reversed(lines[1:] if pos > 0 else lines):
                        if line.strip():
                            try:
                                return int(json.loads(line)["seq"])
                            except (ValueError, KeyError, TypeError):
                                raise AuditWriteError(f"last record of {self.path} is not valid JSON") from None
                    tail = lines[0]
        except (FileNotFoundError, NotADirectoryError):
            return 0
        return 0

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """Get every entry after it is on disk (used for the live UI stream)."""
        self._listeners.append(callback)

    def append(self, entry: dict) -> dict:
        with self._lock:
            entry = {"seq": self._seq + 1, **entry}
            text = redact.scrub(json.dumps(entry, ensure_ascii=False, default=str))
            line = (text + "\n").encode()
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    before = os.fstat(fd).st_size
                    try:
                        view = memoryview(line)
                        while view:                       # complete the record even after short writes
                            n = os.write(fd, view)
                            if n <= 0:
                                raise OSError(errno.EIO, "write made no progress")
                            view = view[n:]
                        os.fsync(fd)
                    except OSError:
                        try:
                            os.ftruncate(fd, before)
                        except OSError:
                            pass
                        raise
                finally:
                    os.close(fd)
            except OSError as e:
                raise AuditWriteError(f"could not write audit log {self.path}: {e}") from e
            self._seq += 1
            entry = json.loads(text)
        # The disk copy is the record of truth; a failing listener must not block actions.
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:
                pass
        return entry


# ---------------------------------------------------------------- permissions

@dataclass
class Grant:
    path: Path
    mode: str            # "rw" or "ro"
    toggle: str
    shell: bool = True   # False: only Guard file actions may use it, never sandboxed shell code


@dataclass
class Permissions:
    toggles: dict[str, bool] = field(default_factory=dict)
    grants: list[Grant] = field(default_factory=list)
    hard_deny: list[Path] = field(default_factory=list)       # no reads, no writes
    write_deny: list[Path] = field(default_factory=list)      # reads allowed, never writes

    @classmethod
    def load(cls, path: Path) -> "Permissions":
        data = yaml.safe_load(Path(path).read_text()) or {}
        toggles = {k: v is True for k, v in (data.get("toggles") or {}).items()}
        # Relative grant paths are relative to the project root (the folder above config/).
        root = Path(path).resolve().parent.parent
        grants = [Grant(_real(g["path"] if str(g["path"]).startswith(("/", "~")) else root / g["path"]),
                        g.get("mode", "ro"), toggle, g.get("shell", True) is True)
                  for toggle, items in (data.get("grants") or {}).items() for g in items or []]
        for g in grants:
            if g.mode not in ("rw", "ro"):
                raise ValueError(f"grant {g.path}: mode must be 'rw' or 'ro'")
        deny, write_deny = [], []
        for item in data.get("hard_deny") or []:
            if isinstance(item, dict):
                if item.get("block", "all") not in ("all", "write"):
                    raise ValueError(f"hard_deny {item}: block must be 'all' or 'write'")
                (write_deny if item.get("block") == "write" else deny).append(_real(item["path"]))
            else:
                deny.append(_real(item))
        deny += [d for d in map(_real, BUILTIN_DENY) if d not in deny]
        return cls(toggles, grants, deny, write_deny)

    def on(self, toggle: str) -> bool:
        return self.toggles.get(toggle, False)

    def active_grants(self) -> list[Grant]:
        return [g for g in self.grants if self.on(g.toggle)]


def _real(p: str | Path) -> Path:
    """Absolute path with ~ expanded and every symlink resolved (works for missing files too)."""
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def _inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


# ---------------------------------------------------------------- guard

@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    over_limit: bool = False      # output exceeded MAX_CAPTURE; command was killed
    stdout_truncated: bool = False  # only the tail of stdout was kept (skills must reject this)


@dataclass
class _Run:
    """One sandboxed command: its marker, capabilities and network credential."""
    id: str
    caps: set[str]
    mark: Path
    token: str | None = None
    proc: subprocess.Popen | None = None
    scope: str | None = None
    needs: set[str] = field(default_factory=set)      # toggles it can't run without (SHELL always): off -> killed
    tag: str | None = None                            # e.g. "skill:<name>": disabling the skill kills it
    admit: Callable[[], None] | None = None           # last check under the admission lock (e.g. skill still valid)


@dataclass
class Access:
    """A checked path: a trusted root directory plus the components below it."""
    root: Path
    parts: tuple[str, ...]

    @property
    def path(self) -> Path:
        return self.root.joinpath(*self.parts)


class Guard:
    def __init__(self, sandbox: Path, permissions_file: Path, audit_file: Path):
        self.sandbox = _real(sandbox)
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.tmp = self.sandbox / ".tmp"          # TMPDIR for sandboxed shell commands
        # Guard-owned scratch space beside the sandbox: staged code (read-only to children),
        # throwaway workspaces for isolated runs, and run markers. Sandboxed code can never
        # write here.
        self.scratch = self.sandbox.parent / ".lc_scratch"
        self.scratch.mkdir(exist_ok=True)
        self.marks = self.scratch / ".marks"
        self.marks.mkdir(exist_ok=True)
        for name in ("all", "probe"):
            (self.marks / name).touch()
        self.permissions_file = _real(permissions_file)
        self.audit = AuditLog(audit_file)
        self.audit_file = _real(audit_file)
        self.managed: list[Path] = []             # folders only their registry may write (skills/)
        self._lease_lock = threading.RLock()
        self._lease_holders: dict[str, threading.Timer] = {}   # run id -> its watchdog (scoped Shell lease)
        self._perms: Permissions | None = None
        self._perms_mtime = -1.0
        self._admit = threading.RLock()
        self._procs: set[subprocess.Popen] = set()
        self._runs: dict[str, _Run] = {}
        self._cancelled: dict[str, None] = {}      # stopped scopes (insertion-ordered, bounded)
        self._written: dict[str, set[Path]] = {}   # scope -> files written with once=True (write-once per run)
        self._revising: set[str] = set()           # scopes the controller put in a post-QA revision stage
        self._broker = None
        self._closing = False
        self._halted, self._fault = self._recover()

    def _recover(self) -> tuple[str | None, str | None]:
        """Rebuild halt/fault state from the audit log (it survives restarts). An action whose
        intent is on disk with no outcome may or may not have happened: start faulted."""
        halted, fault, open_intents = None, None, {}
        try:
            with self.audit.path.open("rb") as f:
                for raw in f:
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        continue
                    action, status, eid = e.get("intended_action"), e.get("status"), e.get("id")
                    if status == "intent":
                        open_intents[eid] = action
                    elif eid in open_intents:
                        del open_intents[eid]
                    if action == "halt":
                        halted = e.get("rationale") or "halted"
                    elif action == "resume":
                        halted = None
                    elif action == "fault":
                        fault = e.get("fault") or "fault"
                    elif action == "clear_fault":
                        fault, open_intents = None, {}
        except (FileNotFoundError, NotADirectoryError):
            pass
        if open_intents and not fault:
            ids = ", ".join(f"{i} ({a})" for i, a in list(open_intents.items())[:5])
            fault = f"{len(open_intents)} action(s) have no recorded outcome (process stopped mid-action): {ids}"
        if fault and not halted:
            halted = "restarted with an unresolved fault"
        return halted, fault

    # --- permissions (re-read whenever the file changes, so toggles apply live)

    @property
    def perms(self) -> Permissions:
        mtime = self.permissions_file.stat().st_mtime
        if mtime != self._perms_mtime:
            self._perms = Permissions.load(self.permissions_file)
            self._perms_mtime = mtime
        return self._perms

    @property
    def protected(self) -> list[Path]:
        """Controller files nothing the agent runs may ever write, whatever the grants say."""
        return [self.permissions_file, self.audit_file, Path(str(self.audit_file) + ".lock")]

    def manage(self, root: Path) -> None:
        """Make a folder writable only through ``write_managed`` (its registry), never by the
        agent's file tools or any sandboxed command."""
        self.managed.append(_real(root))

    def resolve(self, path: str | Path) -> Path:
        """Relative paths are relative to the sandbox."""
        p = Path(os.path.expanduser(str(path)))
        return _real(p if p.is_absolute() else self.sandbox / p)

    def check_path(self, path: str | Path, write: bool) -> Path:
        """Return the real path if access is allowed, else raise PermissionDenied."""
        return self._access(path, write).path

    def _access(self, path: str | Path, write: bool) -> Access:
        p = self.resolve(path)
        perms = self.perms
        for d in perms.hard_deny + (perms.write_deny if write else []):
            if _inside(p, d):
                raise PermissionDenied(f"{p} is in a protected location ({d})")
        if write and any(_inside(p, c) or _inside(c, p) for c in self.protected):
            raise PermissionDenied(f"{p} is a Wave Dispatch control file")
        if write and any(_inside(p, m) for m in self.managed):
            raise PermissionDenied(f"{p} is managed by the skill registry; use create_or_update_skill")
        if any(_inside(p, x) for x in (self.scratch,)):
            raise PermissionDenied(f"{p} is Wave Dispatch scratch space")
        roots = [(self.sandbox, "rw")] + [(g.path, g.mode) for g in perms.active_grants()]
        for root, mode in roots:
            if _inside(p, root):
                if write and mode != "rw":
                    raise PermissionDenied(f"{p} is read-only")
                return Access(root, p.relative_to(root).parts)
        needed = sorted({g.toggle for g in perms.grants if _inside(p, g.path)})
        hint = f"; turn on {' or '.join(needed)}" if needed else ""
        raise PermissionDenied(f"{p} is outside the sandbox{hint}")

    # --- kill switch

    def halt(self, reason: str = "kill switch", *, node: str = "system") -> None:
        """Refuse new actions, close every network tunnel, and kill every process any run
        created (process groups first, then marker-identified escapees)."""
        with self._admit:
            already = self._halted is not None
            self._halted = self._halted or reason
        self._kill_all()
        if self._broker:
            self._broker.revoke(everything=True)
        if not already:
            try:
                self.audit.append(self._entry(node, "halt", "", reason, "halted"))
            except AuditWriteError as e:
                self._set_fault(f"audit log failed while halting: {e}")
        self._reap(None, reason)

    def resume(self, *, node: str = "system", rationale: str = "") -> None:
        with self._admit:
            if self._fault:
                raise Halted(f"guard is faulted and needs manual repair: {self._fault}")
            self.audit.append(self._entry(node, "resume", "", rationale, "resumed"))   # log first
            self._halted = None

    def clear_fault(self, *, node: str, rationale: str) -> None:
        """Operator confirms the unrecorded action was reconciled. The fault is cleared only
        once that confirmation is on disk; the guard stays halted until ``resume()``."""
        with self._admit:
            if not self._fault:
                return
            self.audit.append(self._entry(node, "clear_fault", "", rationale, "cleared", fault=self._fault))
            self._halted = self._halted or f"fault cleared by {node}; resume to continue"
            self._fault = None

    def _set_fault(self, message: str) -> None:
        """Latch a fault (first one wins) and try to record it, so it survives a restart."""
        with self._admit:
            if self._fault:
                return
            self._fault = message
        try:
            self.audit.append(self._entry("system", "fault", "", message, "faulted", fault=message))
        except AuditWriteError:
            pass               # the open intent still makes the next start faulted

    def shutdown(self, timeout: float = 10.0) -> None:
        """Server stopping: refuse new work, kill running commands, wait for in-flight actions
        to record their outcome. Does not persist a halt."""
        with self._admit:
            self._closing = True
        self._kill_all()
        if self._broker:
            self._broker.revoke(everything=True)
        self._reap(None, "shutdown")
        end = time.monotonic() + timeout
        while self._procs and time.monotonic() < end:
            time.sleep(0.05)
        if self._broker:
            self._broker.close()
        self.audit.close()

    @property
    def halted(self) -> bool:
        return self._halted is not None or self._fault is not None or self._closing

    @property
    def fault(self) -> str | None:
        return self._fault

    def _kill_all(self) -> None:
        for p in list(self._procs):
            _kill_group(p)

    def _reap(self, run_ids: set[str] | None, reason: str) -> int:
        """Kill every process created by the given runs (None = by any run), wherever it is:
        macOS is asked, for every PID, whether its sandbox allows that run's marker file
        while denying the probe file. That holds only for processes started under that run's
        profile, so setsid, chdir and closed descriptors don't hide them, and unrelated
        processes (sandboxed or not) never match. Each PID is re-checked right before kill."""
        marks = [self.marks / "all"] if run_ids is None else [self.marks / r for r in run_ids]
        marks = [m for m in marks if m.exists()]
        if not marks:
            return 0
        probe = self.marks / "probe"
        me = os.getpid()

        def ours(pid: int) -> bool:
            return (pid != me and _sandboxed(pid) and not _sb_allows(pid, probe)
                    and any(_sb_allows(pid, m) for m in marks))

        killed = 0
        for _ in range(REAP_PASSES):             # a forking escapee may add children while we kill
            found = [pid for pid in _all_pids() if ours(pid)]
            if not found:
                break
            for pid in found:
                if ours(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                    except (ProcessLookupError, PermissionError):
                        pass
            time.sleep(0.02)
        if killed:
            try:
                self.audit.append(self._entry("system", "kill_detached", str(self.sandbox), reason, "killed",
                                              count=killed))
            except AuditWriteError as e:
                self._set_fault(f"audit log failed while killing detached processes: {e}")
        return killed

    def revoke_tag(self, tag: str, reason: str) -> int:
        """Stop every running command carrying this tag (a skill that was just disabled or replaced)."""
        with self._admit:
            victims = [r for r in list(self._runs.values()) if r.tag == tag]
            for r in victims:
                if r.proc:
                    _kill_group(r.proc)
                if r.token and self._broker:
                    self._broker.revoke(r.token)
        if victims:
            self._reap({r.id for r in victims}, reason)
        return len(victims)

    def _revoke(self, toggle: str, reason: str) -> int:
        """A capability was turned off: stop every running command that had it."""
        victims = [r for r in list(self._runs.values()) if toggle in r.caps or toggle in r.needs]
        for r in victims:
            if r.proc:
                _kill_group(r.proc)
            if r.token and self._broker:
                self._broker.revoke(r.token)
        if victims:
            self._reap({r.id for r in victims}, reason)
        return len(victims)

    def _check_scope(self) -> None:
        scope = current_scope.get()
        if scope is not None and scope in self._cancelled:
            raise ScopeCancelled(f"{scope} was stopped")

    def cancel_scope(self, scope: str, *, node: str, rationale: str, action: str = "cancel_scope",
                     target: Any = None, on_commit: Callable[[], None] = lambda: None) -> int:
        """Stop one run (all actions carrying this scope): logged first, then, under the admission
        lock, no new action of that scope is admitted and its running commands are killed (process
        group, network credential, marker-found escapees) before this returns. Allowed while halted."""
        def run():
            with self._admit:
                self._cancelled[scope] = None
                while len(self._cancelled) > MAX_CANCELLED_SCOPES:
                    self._cancelled.pop(next(iter(self._cancelled)))
                on_commit()
                victims = [r for r in list(self._runs.values()) if r.scope == scope]
                for r in victims:
                    if r.proc:
                        _kill_group(r.proc)
                    if r.token and self._broker:
                        self._broker.revoke(r.token)
            if victims:
                self._reap({r.id for r in victims}, f"{scope} stopped")
            return len(victims)

        return self.control(action, target if target is not None else scope, node=node, rationale=rationale,
                            run=run, widens=False, detail={"scope": scope})

    # --- the write-ahead core

    def _entry(self, node, action, target, rationale, status, **extra) -> dict:
        return {"id": extra.pop("id", uuid.uuid4().hex[:12]),
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "agent_node": node, "intended_action": action, "target_path": str(target),
                "rationale": rationale, "status": status, **extra}

    def perform(self, action: str, target: Any, *, node: str, rationale: str,
                check: Callable[[], None], run: Callable[[], Any], detail: dict | None = None,
                while_halted: bool = False, atomic: bool = False) -> Any:
        """Check -> log intent (fsync) -> run -> log outcome. Use this for any new kind of action.
        while_halted: allowed during a halt/fault (only for changes that narrow what can run).
        atomic: run() commits under the admission lock (short controller changes), so a halt
        can't land between the intent and the commit."""
        action_id = uuid.uuid4().hex[:12]
        base = dict(id=action_id, **(detail or {}))
        with self._admit:
            try:
                if self._closing:
                    raise Halted("Wave Dispatch is shutting down")
                if self._fault and not while_halted:
                    raise Halted(f"guard is faulted: {self._fault}")
                if self._halted and not while_halted:
                    raise Halted(f"actions are halted: {self._halted}")
                if not while_halted:
                    self._check_scope()
                check()
            except GuardError as e:
                # Refusals are logged too. If even that fails, AuditWriteError wins.
                self.audit.append(self._entry(node, action, target, rationale, "blocked", reason=str(e), **base))
                raise
            self.audit.append(self._entry(node, action, target, rationale, "intent", **base))
            if atomic:
                return self._run_and_record(node, action, target, rationale, action_id, run)
        return self._run_and_record(node, action, target, rationale, action_id, run)

    def _run_and_record(self, node, action, target, rationale, action_id, run):
        started = time.monotonic()
        try:
            result = run()
        except BaseException as e:
            self._outcome(node, action, target, rationale, "error", action_id, started,
                          error=f"{type(e).__name__}: {e}", cause=e)
            raise
        self._outcome(node, action, target, rationale, "ok", action_id, started, summary=_summarize(result))
        return result

    def _outcome(self, node, action, target, rationale, status, action_id, started, cause=None, **extra):
        try:
            self.audit.append(self._entry(node, action, target, rationale, status, id=action_id,
                                          duration_s=round(time.monotonic() - started, 3), **extra))
        except AuditWriteError as e:
            message = f"action {action_id} ({action}) ran but its outcome could not be logged: {e}"
            self._set_fault(message)
            self._kill_all()
            if self._broker:
                self._broker.revoke(everything=True)
            raise ActionUnrecorded(message) from (cause or e)

    # --- safe file primitives (no symlink can be swapped in between check and use)

    def _open_dir(self, access: Access, create: bool) -> int:
        fd = os.open(access.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in access.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise PermissionDenied(f"symlink or non-folder in path at '{part}'") from e
                    raise
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _write_bytes(self, access: Access, data: bytes) -> None:
        if not access.parts:
            raise PermissionDenied("cannot write to a root folder itself")
        name = access.parts[-1]
        dfd = self._open_dir(access, create=True)
        tmp = f".{name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError(errno.EIO, "write made no progress")
                    view = view[n:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)    # atomic; replaces a symlink, never follows it
            os.fsync(dfd)                                             # the rename itself is durable
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        finally:
            os.close(dfd)

    def _read_bytes(self, access: Access, limit: int) -> bytes:
        if not access.parts:
            raise PermissionDenied("cannot read a folder")
        dfd = self._open_dir(access, create=False)
        try:
            try:
                # O_NONBLOCK: a FIFO or device can't stall the read (and with it the executor).
                fd = os.open(access.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    raise PermissionDenied(f"{access.path} is a symlink") from e
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise PermissionDenied(f"{access.path} is not a regular file")
                chunks, left = [], limit
                while left > 0:
                    chunk = os.read(fd, min(left, 1 << 20))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    left -= len(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(dfd)

    # --- operator controls (dashboard / Discord): write-ahead logged like any action

    def control(self, action: str, target: Any, *, node: str, rationale: str, run: Callable[[], Any],
                widens: bool, check: Callable[[], None] = lambda: None, detail: dict | None = None) -> Any:
        """A user-initiated change (toggle, task control, launch). Logged intent-first. A change
        that only narrows what can run (turn off, pause, stop) is allowed during a halt or fault;
        one that widens it (turn on, launch) is refused until the guard is resumed."""
        return self.perform(action, target, node=node, rationale=rationale, check=check, run=run,
                            detail=detail, while_halted=not widens, atomic=True)

    def set_toggle(self, toggle: str, on: bool, *, node: str, rationale: str) -> None:
        """Flip one permission toggle in permissions.yaml (atomic replace, comments kept).
        This is the only code path that writes the permissions file."""
        pattern = re.compile(rf"^(\s+{re.escape(toggle)}:\s*)(true|false)\b", re.M)

        def check():
            if toggle not in self.perms.toggles:
                raise PermissionDenied(f"unknown toggle {toggle!r}")

        def run():
            text = self.permissions_file.read_text()
            new, n = pattern.subn(lambda m: m.group(1) + ("true" if on else "false"), text, count=1)
            if n != 1:
                raise ValueError(f"{toggle} not found in {self.permissions_file.name}")
            folder = self.permissions_file.parent
            self._write_bytes(Access(folder, (self.permissions_file.name,)), new.encode())
            self._perms_mtime = -1.0                  # reload on next access
            if self.perms.on(toggle) != on:
                raise ValueError(f"{toggle} did not change (file reads {self.perms.on(toggle)})")
            if not on:                                # revocation reaches running commands too
                self._revoke(toggle, f"{toggle} turned off by {node}")
                if toggle == NETWORK and self._broker:
                    self._broker.revoke(everything=True)
            return self.permissions_file

        self.control("set_toggle", self.permissions_file, node=node, rationale=rationale, run=run,
                     widens=on, check=check, detail={"toggle": toggle, "value": on})

    # --- scoped Shell lease: autonomous runs (Obsidian / Discord) get Shell exec only while they run

    LEASE_RATIONALE = "scoped run lease"
    LOCKED_OFF = ("ALLOW_EPIC_GAMES_DIR", "ALLOW_SKILL_AUTHORING")   # never turned on by a lease; forced off if found on

    def lease_acquire(self, run_id: str, timeout_s: float, *, node: str = "controller") -> None:
        """Shell exec ON for this run (reference-counted across concurrent runs; a watchdog forces it OFF after
        timeout_s even if the run hangs). The locked-off toggles are turned off if they were on."""
        with self._lease_lock:
            for t in self.LOCKED_OFF:
                if t in self.perms.toggles and self.perms.on(t):
                    self.set_toggle(t, False, node=node, rationale=f"{self.LEASE_RATIONALE} {run_id}: locked off")
            if not self._lease_holders and not self.perms.on(SHELL):
                self.set_toggle(SHELL, True, node=node, rationale=f"{self.LEASE_RATIONALE} {run_id}: on for the run")
            timer = threading.Timer(max(1.0, float(timeout_s)), self.lease_release, args=(run_id,), kwargs={"node": "watchdog"})
            timer.daemon = True
            self._lease_holders[run_id] = timer
            timer.start()

    def lease_release(self, run_id: str, *, node: str = "controller") -> None:
        """This run is done (success, failure, timeout or exception): when no lease is left, Shell exec goes OFF,
        retried until the file says off."""
        with self._lease_lock:
            timer = self._lease_holders.pop(run_id, None)
            if timer is not None:
                timer.cancel()
            if self._lease_holders:
                return
            for attempt in range(5):
                try:
                    if self.perms.on(SHELL):
                        self.set_toggle(SHELL, False, node=node, rationale=f"{self.LEASE_RATIONALE} {run_id}: off after the run")
                    if not self.perms.on(SHELL):
                        return
                except Exception:                     # e.g. a busy audit lock: try again
                    time.sleep(0.5 * (attempt + 1))
            # still ON: keep an owner that retries every 30 s until it is OFF (Astra review 30)
            retry = threading.Timer(30.0, self.lease_release, args=(f"{run_id}-retry",), kwargs={"node": "watchdog"})
            retry.daemon = True
            retry.start()

    def scoped_run_lease(self, run_id: str, timeout_s: float, *, node: str = "controller"):
        """`with guard.scoped_run_lease(run_id, timeout_s):` Shell exec ON inside, guaranteed OFF after."""
        guard = self

        class _Lease:
            def __enter__(self):
                guard.lease_acquire(run_id, timeout_s, node=node)
                return self

            def __exit__(self, *exc):
                guard.lease_release(run_id, node=node)
                return False
        return _Lease()

    def release_stale_lease(self, *, node: str = "controller") -> bool:
        """At start-up: Shell exec left ON by a lease of a process that died is turned OFF (nothing holds it now)."""
        if self._lease_holders or not self.perms.on(SHELL):
            return False
        last = None
        try:                                          # the whole log, newest first (a long run's ON is far back)
            for line in reversed(self.audit.path.read_text().splitlines()):
                if '"set_toggle"' not in line or SHELL not in line:
                    continue
                e = json.loads(line)
                if e.get("intended_action") == "set_toggle" and e.get("status") == "intent" and e.get("toggle") == SHELL:
                    last = e                              # the last decision about Shell exec
                    break
        except (OSError, ValueError):
            return False
        if last and str(last.get("rationale", "")).startswith(self.LEASE_RATIONALE):
            self.set_toggle(SHELL, False, node=node, rationale=f"{self.LEASE_RATIONALE}: stale after a restart, off")
            return True
        return False

    def write_managed(self, path: str | Path, content: bytes | str, *, node: str, rationale: str,
                      widens: bool, check: Callable[[], None] = lambda: None) -> Path:
        """The only way to write inside a managed folder (the skill registry's files). Logged
        and atomic like any action; the agent's own file tools can never reach these paths."""
        data = content.encode() if isinstance(content, str) else content
        target = Path(path).absolute()
        root = next((m for m in self.managed if _inside(target, m)), None)

        def full_check():
            if root is None:
                raise PermissionDenied(f"{target} is not in a managed folder")
            check()

        def run():
            self._write_bytes(Access(root, target.relative_to(root).parts), data)
            return target

        return self.perform("write_managed", target, node=node, rationale=rationale, check=full_check, run=run,
                            detail={"bytes": len(data)}, while_halted=not widens, atomic=True)

    # --- built-in actions

    def write_file(self, path, content: str, *, node: str, rationale: str, once: bool = False) -> Path:
        """once=True: write-once per run scope. A second write of the same file in the same scope is
        refused (logged as duplicate_write_prevented, raises DuplicateWrite) unless the controller
        opened a revision stage for that scope (allow_rewrites)."""
        data = content.encode()
        target = self.resolve(path)
        box: dict[str, Access] = {}
        scope = current_scope.get() if once else None
        if once and scope is None:
            raise PermissionDenied("write-once needs a run scope")

        def duplicate() -> bool:
            return target in self._written.get(scope, ()) and scope not in self._revising

        def refuse():
            raise DuplicateWrite(f"duplicate_write_prevented: {target.name} was already written in this run; "
                                 "the file on disk is kept", target)

        if once:
            with self._admit:
                if duplicate():                 # logged as blocked; raises DuplicateWrite (or AuditWriteError)
                    self.perform("duplicate_write_prevented", target, node=node, rationale=rationale,
                                 check=refuse, run=lambda: None, detail={"scope": scope, "bytes": len(data)},
                                 while_halted=True)

        def check():
            box["a"] = self._access(target, write=True)
            if len(data) > MAX_WRITE:
                raise PermissionDenied(f"write of {len(data)} bytes exceeds {MAX_WRITE}")
            if once:                            # reserve under the admission lock (no two writers race past it)
                if duplicate():
                    refuse()
                self._written.setdefault(scope, set()).add(target)

        def run():
            try:
                self._write_bytes(box["a"], data)
            except BaseException:
                if once and scope not in self._revising:
                    self._written.get(scope, set()).discard(target)   # nothing written: the lock isn't taken
                raise
            return target

        return self.perform("write_file", target, node=node, rationale=rationale,
                            check=check, run=run, detail={"bytes": len(data)})

    def allow_rewrites(self, scope: str, *, node: str, rationale: str) -> None:
        """Controller-only: open a post-QA revision stage for a run scope (its write-once files may be rewritten)."""
        self.control("allow_rewrites", scope, node=node, rationale=rationale, widens=True,
                     run=lambda: self._revising.add(scope), detail={"scope": scope})

    def end_rewrites(self, scope: str, *, node: str, rationale: str) -> None:
        """Close a scope's revision stage: its files are write-once again (narrows, so allowed while halted)."""
        self.control("end_rewrites", scope, node=node, rationale=rationale, widens=False,
                     run=lambda: self._revising.discard(scope), detail={"scope": scope})

    def written_once(self, scope: str) -> list[Path]:
        with self._admit:
            return sorted(self._written.get(scope, ()))

    def forget_writes(self, scope: str) -> None:
        """A run is over: drop its write-once lock set (bounded memory)."""
        with self._admit:
            self._written.pop(scope, None)
            self._revising.discard(scope)

    def read_file(self, path, *, node: str, rationale: str, max_chars: int = 200_000) -> str:
        target = self.resolve(path)
        box: dict[str, Access] = {}

        def check():
            box["a"] = self._access(target, write=False)

        def run():
            limit = min(MAX_READ, max_chars * 4)
            return self._read_bytes(box["a"], limit).decode(errors="replace")[:max_chars]

        return self.perform("read_file", target, node=node, rationale=rationale, check=check, run=run)

    def stage_code(self, label: str, source: str, filename: str, *, node: str, rationale: str) -> Path:
        """Copy code into a fresh Guard-owned folder that sandboxed processes can read but
        not write. Returns the file path; remove it with ``unstage``."""
        d = self.scratch / "code" / f"{label}-{uuid.uuid4().hex[:8]}"

        def run():
            d.mkdir(parents=True)
            self._write_bytes(Access(d, (filename,)), source.encode())
            return d / filename

        return self.perform("stage_code", d / filename, node=node, rationale=rationale,
                            check=lambda: None, run=run, detail={"bytes": len(source)})

    def unstage(self, path: Path) -> None:
        d = Path(path).parent
        if _inside(d, self.scratch) and d != self.scratch:
            shutil.rmtree(d, ignore_errors=True)

    def run_shell(self, command: str, *, node: str, rationale: str, cwd: str | Path = ".",
                  timeout: int = 300, capabilities: set[str] | None = None,
                  read_paths: list[Path] | None = None, isolated: bool = False,
                  tag: str | None = None, admit_check: Callable[[], None] | None = None) -> ShellResult:
        """Run a zsh command under the deny-by-default sandbox.

        capabilities: None = every active toggle applies (the agent's own commands).
                      A set = only grants/network from those toggles (skills get exactly what
                      they declared, intersected with what is switched on).
        read_paths:   extra read-only folders (staged code).
        isolated:     run in a fresh empty workspace with no grants and no network
                      (used for validating untrusted code).
        """
        workdir = self.resolve(cwd)

        def check():
            if not self.perms.on(SHELL):
                raise PermissionDenied(f"shell commands are off; turn on {SHELL}")
            if not isolated:
                self._access(workdir, write=False)

        def run():
            scratch_ws = self.scratch / "isolated" / uuid.uuid4().hex[:8] if isolated else None
            root = scratch_ws or self.sandbox
            tmp = root / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            perms = self.perms
            wanted = set(perms.toggles) if capabilities is None else set(capabilities)
            caps = set() if isolated else {t for t in wanted if perms.on(t)}
            r = _Run(uuid.uuid4().hex, caps, self.marks / uuid.uuid4().hex, scope=current_scope.get(), tag=tag,
                     admit=admit_check,
                     # Required toggles are re-checked at spawn and revoke the process when turned off
                     # (Astra review 8): Shell exec always, plus every toggle a skill declared.
                     needs={SHELL} | (set() if isolated or capabilities is None else set(capabilities)))
            r.mark = self.marks / r.id
            r.mark.touch()
            env = {"PATH": SANDBOX_PATH, "HOME": str(root), "TMPDIR": str(tmp) + "/",
                   "TMPPREFIX": str(tmp) + "/zsh", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                   "SSL_CERT_FILE": "/private/etc/ssl/cert.pem"}         # system CA bundle
            net_port = None
            if NETWORK in caps:
                broker = self._net()
                r.token = broker.issue(r.id, node)
                env |= broker.env(r.token)
                net_port = broker.port
            profile = self.sandbox_profile(capabilities=caps, read_paths=read_paths, write_root=root,
                                           grants=not isolated, net_port=net_port, run_id=r.id)
            try:
                return self._spawn(["/usr/bin/sandbox-exec", "-p", profile, "/bin/zsh", "-c", command],
                                   cwd=scratch_ws or workdir, env=env, timeout=timeout, run=r)
            finally:
                if r.token and self._broker:
                    self._broker.revoke(r.token)
                self._reap({r.id}, "command finished")      # setsid escapees, wherever they went
                r.mark.unlink(missing_ok=True)
                if scratch_ws:
                    shutil.rmtree(scratch_ws, ignore_errors=True)

        return self.perform("run_shell", workdir, node=node, rationale=rationale, check=check, run=run,
                            detail={"command": command, "isolated": isolated,
                                    "capabilities": sorted(capabilities) if capabilities is not None else "all"})

    def _spawn(self, argv, cwd, env, timeout, run: _Run) -> ShellResult:
        """Spawn + register under the admission lock (so a halt or fault either refuses it or
        sees it), in a new process group with bounded output capture. The group is killed on
        timeout, output overflow, halt, revocation, and when the command finishes."""
        with self._admit:
            if self._halted or self._fault or self._closing:
                raise Halted(f"actions are halted: {self._halted or self._fault or 'shutting down'}")
            if run.scope is not None and run.scope in self._cancelled:
                raise ScopeCancelled(f"{run.scope} was stopped")
            off = sorted(t for t in run.needs if not self.perms.on(t))
            if off:                                      # turned off after the earlier check: refuse, don't race
                raise PermissionDenied(f"{', '.join(off)} turned off; not starting the command")
            if run.admit is not None:
                run.admit()
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
            run.proc = p
            self._procs.add(p)
            self._runs[run.id] = run
        out, err = _Capture(p.stdout, p), _Capture(p.stderr, p)
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(p)
            p.wait()
            out.join()
            err.join()
            with self._admit:
                self._procs.discard(p)
                self._runs.pop(run.id, None)
        over = out.over or err.over
        stderr = err.text()
        if timed_out:
            stderr += f"\n[killed: timed out after {timeout}s]"
        if over:
            stderr += f"\n[killed: output exceeded {MAX_CAPTURE} bytes]"
        code = -1 if (timed_out or over) else p.returncode
        return ShellResult(code, out.text(), stderr.strip(), timed_out, over, out.truncated)

    def _net(self):
        """The network broker, started on first use."""
        with self._admit:
            if self._broker is None:
                from .netbroker import Broker
                self._broker = Broker(self._authorize_connection)
            return self._broker

    def _authorize_connection(self, grant, host: str, port: int, addrs: list) -> None:
        """Broker callback: a sandboxed run wants to connect. Logged write-ahead; refused when
        halted, when the run is gone, or when network is off."""
        def check():
            if not self.perms.on(NETWORK):
                raise PermissionDenied(f"network access is off; turn on {NETWORK}")
            if grant.run_id not in self._runs:
                raise PermissionDenied("the command that owned this credential has finished")

        self.perform("net_connect", f"{host}:{port}", node=grant.node, rationale=f"run {grant.run_id[:8]}",
                     check=check, run=lambda: None, detail={"addresses": [str(a) for a in addrs][:4]},
                     atomic=True)

    def fetch(self, url: str, *, node: str, rationale: str, timeout: float = 30) -> str:
        """HTTP(S) GET to a public address only. DNS is resolved once, AFTER the intent is
        logged, checked, and the connection is pinned to that address (no DNS-rebinding
        second lookup). Proxy environment variables are ignored."""
        import httpx

        u = urlparse(url)

        def check():
            if not self.perms.on(NETWORK):
                raise PermissionDenied(f"network access is off; turn on {NETWORK}")
            if u.scheme not in ("http", "https") or not u.hostname:
                raise PermissionDenied(f"only http(s) URLs are allowed: {url}")

        def run():
            addrs = _resolve(u.hostname)
            bad = [a for a in addrs if not a.is_global]
            if bad or not addrs:
                raise PermissionDenied(f"{u.hostname} resolves to non-public address {bad or 'none'}")
            ip = addrs[0]
            host_ip = f"[{ip}]" if ip.version == 6 else str(ip)
            pinned = u._replace(netloc=f"{host_ip}:{u.port}" if u.port else host_ip).geturl()
            headers = {"Host": u.netloc.split("@")[-1]}
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                with client.stream("GET", pinned, headers=headers,
                                   extensions={"sni_hostname": u.hostname}) as r:
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_READ:
                            break
                    return f"HTTP {r.status_code}\n{bytes(body[:MAX_READ]).decode(errors='replace')[:MAX_OUTPUT]}"

        return self.perform("fetch", url, node=node, rationale=rationale, check=check, run=run)

    # --- OS-level sandbox for shell commands

    def sandbox_profile(self, capabilities: set[str] | None = None, read_paths: list[Path] | None = None,
                        write_root: Path | None = None, grants: bool = True, net_port: int | None = None,
                        run_id: str | None = None) -> str:
        """Deny-by-default SBPL profile. Allowed: run/read system programs, libraries and
        shared data (SYSTEM_*), read+write the workspace and shell-enabled rw grants, read ro
        grants and staged code, user/group lookup and logging. Network: only the broker's
        port (when this run has ALLOW_NETWORK_FETCH). Everything else is denied: user
        folders, temp folders, preferences, keychains, IPC, other processes, Apple Events,
        LaunchServices. Hard-deny paths and control files are denied again at the end
        (later file rules win)."""
        perms = self.perms
        caps = None if capabilities is None else set(capabilities)
        active = [g for g in perms.active_grants() if g.shell and grants and (caps is None or g.toggle in caps)]
        root = write_root or self.sandbox
        rw = [root] + [g.path for g in active if g.mode == "rw"]
        ro = [g.path for g in active if g.mode == "ro"] + [_real(p) for p in read_paths or []]
        ancestors = sorted({str(a) for r in rw + ro + [Path(x) for x in SYSTEM_READ] for a in [*Path(r).parents]}
                           | {"/", "/private", "/private/var", "/private/etc", "/var", "/etc", "/tmp"})
        sub = lambda paths: " ".join(f"(subpath {_q(Path(x))})" for x in paths)
        lit = lambda paths: " ".join(f"(literal {_q(Path(x))})" for x in paths)
        marks = [self.marks / "all"] + ([self.marks / run_id] if run_id else [])
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "dyld-support.sb")',
            "(allow process-fork)",
            "(allow process-info* (target same-sandbox))",
            "(allow signal (target same-sandbox))",
            f"(allow process-exec {sub(SYSTEM_EXEC)} {sub(ro)} {sub(rw)})",
            f"(allow file-read* {sub(SYSTEM_READ)} {lit(SYSTEM_FILES)})",
            f"(allow file-read-metadata {lit(ancestors)})",
            f"(allow file-map-executable {sub(SYSTEM_READ)} {sub(ro)} {sub(rw)})",
            '(allow file-read* file-write* (subpath "/dev/fd") (literal "/dev/null") (literal "/dev/zero")'
            ' (literal "/dev/dtracehelper") (regex #"^/dev/tty"))',
            '(allow file-ioctl (literal "/dev/dtracehelper") (regex #"^/dev/tty"))',
            f"(allow file-read* file-write* {sub(rw)})",
            *([f"(allow file-read* {sub(ro)})"] if ro else []),
            "(allow sysctl-read)",
            "(allow ipc-posix-sem)",
            "(allow mach-lookup " + " ".join(f'(global-name "{s}")' for s in MACH_ALLOW) + ")",
            # Scratch is denied, then re-opened only for this run's markers (how _reap finds
            # its processes; the probe marker stays denied), its staged code and, for an
            # isolated run, its throwaway workspace.
            f"(deny file-read* file-write* (subpath {_q(self.scratch)}))",
            f"(allow file-read* {lit(marks)})",
            *([f"(allow file-read* {sub(ro)})"] if ro and any(_inside(Path(x), self.scratch) for x in ro) else []),
            *([f"(allow file-read* file-write* {sub([root])})"] if _inside(root, self.scratch) else []),
            *[f"(deny file-read* file-write* (subpath {_q(p)}))" for p in perms.hard_deny + self.managed],
            *[f"(deny file-write* (subpath {_q(p)}))" for p in perms.write_deny],
            *[f"(deny file-read* file-write* (literal {_q(p)}))" for p in self.protected],
            *[f"(deny process-exec (literal {_q(Path(x))}))" for x in DENY_EXEC],
        ]
        if net_port:
            lines.append(f'(allow network-outbound (remote tcp "localhost:{int(net_port)}"))')
        return "\n".join(lines)


class _Capture:
    """Reads a pipe on a thread, keeps the last MAX_OUTPUT chars, kills the process group
    if the stream passes MAX_CAPTURE bytes."""

    def __init__(self, pipe, proc):
        self.buf = bytearray()
        self.total = 0
        self.over = False
        self._pipe, self._proc = pipe, proc
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        for chunk in iter(lambda: self._pipe.read1(65536), b""):
            self.total += len(chunk)
            self.buf += chunk
            if len(self.buf) > MAX_OUTPUT * 4:
                del self.buf[: len(self.buf) - MAX_OUTPUT * 4]
            if self.total > MAX_CAPTURE and not self.over:
                self.over = True
                _kill_group(self._proc)

    def join(self):
        self._t.join(timeout=5)

    @property
    def truncated(self) -> bool:
        return self.total > len(self.buf) or len(bytes(self.buf).decode(errors="replace")) > MAX_OUTPUT

    def text(self) -> str:
        return bytes(self.buf).decode(errors="replace")[-MAX_OUTPUT:]


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


try:
    _sandbox_check = ctypes.CDLL("/usr/lib/libSystem.dylib").sandbox_check
    _sandbox_check.argtypes, _sandbox_check.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
except (OSError, AttributeError):
    _sandbox_check = None


def _sandboxed(pid: int) -> bool:
    """True if macOS says the process runs under a sandbox profile (sandbox-exec children do)."""
    return bool(_sandbox_check and _sandbox_check(pid, None, 0) == 1)


def _sb_allows(pid: int, path: Path) -> bool:
    """Does this process's sandbox allow reading ``path``? (The path must exist.)"""
    return bool(_sandbox_check and _sandbox_check(pid, b"file-read-data", 1, ctypes.c_char_p(str(path).encode())) == 0)


try:
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
except OSError:
    _libproc = None


def _all_pids() -> list[int]:
    if _libproc is None:
        return []
    n = _libproc.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 256))()
    n = _libproc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [buf[i] for i in range(max(0, n)) if buf[i] > 0]


def _q(p: Path) -> str:
    s = str(p).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise PermissionDenied(f"cannot resolve {host}: {e}") from e
    return [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]


def _summarize(result: Any) -> Any:
    if isinstance(result, ShellResult):
        return {"exit_code": result.exit_code, "timed_out": result.timed_out, "over_limit": result.over_limit,
                "stdout_chars": len(result.stdout), "stderr_chars": len(result.stderr),
                "stdout_truncated": result.stdout_truncated}
    if isinstance(result, str):
        return {"chars": len(result)}
    if isinstance(result, Path):
        return {"path": str(result)}
    return None
