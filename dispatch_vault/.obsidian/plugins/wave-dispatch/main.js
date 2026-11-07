"use strict";
/*
 * Wave Dispatch panel for Obsidian (desktop). Plain JavaScript, no build step.
 *
 * Ask the team of small local models from a side panel, watch every step run (split, gates, specialists, review,
 * compile), and compare the team with ONE model given the same notes. The panel runs `wave_dispatch_app.py` (next to
 * this vault) with Node's spawn and an argument list,
 * never a shell; the prompt goes in on stdin. Everything the models write is shown as plain text (never rendered as
 * Markdown or HTML), and the run notes / receipts are written by the Python side through the Guard.
 */
const obsidian = require("obsidian");
const { Plugin, ItemView, PluginSettingTab, Setting, Notice, TFile } = obsidian;
const childProcess = require("child_process");
const nodePath = require("path");

const VIEW = "wave-dispatch-view";
const DEFAULTS = {
  python: "",                                                 // "" = find one (see pythonCandidates); or a command/path
  repo: "",                                                   // folder with wave_dispatch_app.py ("" = the folder above this vault)
  endpoint: "",                                               // optional: This Computer's endpoint for this panel
  provider: "",                                               // "" (from the Machine note) | ollama | openai
  soloModel: "",                                              // the model last picked for comparisons
  compare: false,
  useNotes: true,                                             // the Graph Reader may read notes the question links
  split: true,                                                // the Splitter splits the question into parts first
  review: true,                                               // the review loop checks code (off = ANSWERED, not VERIFIED)
};
const RUN_TIMEOUT_MS = 20 * 60 * 1000;

// ------------------------------------------------------------------------------------------- pure helpers (tested)

function repoDir(vaultBase, setting) {
  const s = typeof setting === "string" ? setting.trim() : "";
  return s || nodePath.dirname(vaultBase);
}

function appArgs(command, settings, vaultBase, extra) {
  if (!["status", "models", "run"].includes(command)) throw new Error("unknown command " + command);
  const s = settings || {};
  const args = [nodePath.join(repoDir(vaultBase, s.repo), "wave_dispatch_app.py"), command, "--json", "--vault", vaultBase];
  if (typeof s.endpoint === "string" && s.endpoint.trim()) args.push("--endpoint", s.endpoint.trim());
  if (s.provider === "ollama" || s.provider === "openai") args.push("--provider", s.provider);
  return args.concat(Array.isArray(extra) ? extra.map(String) : []);
}

/** The run switches as backend arguments (an argument list, never a shell string). */
function runExtra(settings, solo) {
  const s = settings || {};
  const out = [];
  if (typeof solo === "string" && solo) out.push("--solo-model", solo);
  if (s.useNotes === false) out.push("--no-notes");
  if (s.split === false) out.push("--no-split");
  if (s.review === false) out.push("--no-review");
  return out;
}

/** "notes off · split off · review off" for a result's mode (empty when all are on). */
function modeLabel(mode) {
  const m = mode || {};
  return [m.use_notes === false ? "notes off" : "", m.split === false ? "split off" : "",
          m.review === false ? "review off" : ""].filter(Boolean).join(" · ");
}

/** "1. write is_prime → Code Bot" lines for a result's parts and routes. */
function routeLines(parts, routes) {
  const units = Array.isArray(parts) && parts.length > 1 ? parts : ["(the whole question)"];
  const r = routes && typeof routes === "object" ? routes : {};
  return units.map((p, i) => `${i + 1}. ${String(p)} → ${(Array.isArray(r[String(i + 1)]) ? r[String(i + 1)] : []).join(", ") || "?"}`);
}

function lineSplitter(onLine) {
  let buf = "";
  return {
    push(chunk) {
      buf += chunk;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        if (line.trim()) onLine(line);
      }
      if (buf.length > 1000000) buf = "";                  // never grow without bound on a line with no newline
    },
    end() {
      if (buf.trim()) onLine(buf);
      buf = "";
    },
  };
}

function parseEvent(line) {
  try {
    const o = JSON.parse(line);
    return o && typeof o === "object" && !Array.isArray(o) && typeof o.event === "string" ? o : null;
  } catch (e) {
    return null;
  }
}

function badge(status) {
  const b = { VERIFIED: "✅ VERIFIED", ANSWERED: "💬 ANSWERED (not checked)", UNVERIFIED: "⚠️ UNVERIFIED", FAILED: "❌ FAILED" };
  return b[status] || "❔ " + String(status || "UNKNOWN");
}

const STAGES = { split: "Split", gate: "Gate", graph: "Notes", work: "Answer", calc: "Calculator", recheck: "Recheck",
                 review: "Review gate", reviewer: "Reviewer", fix: "Fix", compile: "Compile" };
function stageLabel(stage) {
  return STAGES[stage] || String(stage || "Step");
}

/** Where to look for Python 3.10+ with pyyaml. An explicit setting is the ONLY candidate. Otherwise the project's own
 *  virtualenv first, then the usual install places: an Obsidian started from the Dock gets a minimal PATH, so a bare
 *  "python3" is often the system Python 3.9 without pyyaml. */
function pythonCandidates(setting, repo, platform) {
  const s = typeof setting === "string" ? setting.trim() : "";
  if (s) return [s];
  if (platform === "win32") {
    return [nodePath.win32.join(repo, ".venv", "Scripts", "python.exe"), "py", "python", "python3"];
  }
  return [nodePath.posix.join(repo, ".venv", "bin", "python"), "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
          "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "python3"];
}

/** Try each candidate with the read-only `status` command; the first that answers with a status event wins. */
async function findPython(candidates, args, cwd, spawnFn) {
  let last = null;
  for (const py of candidates) {
    const r = await runApp({ python: py, args, cwd, input: "", spawnFn, timeoutMs: 60000 }).done;
    last = r;
    if (r.events.some((e) => e.event === "status")) return { python: py, result: r, tried: candidates };
  }
  return { python: null, result: last, tried: candidates };
}

/** One run at a time: tryEnter() is taken BEFORE any await, so a second click can never start a second process. */
class Gate {
  constructor() { this.busy = false; }
  tryEnter() { if (this.busy) return false; this.busy = true; return true; }
  leave() { this.busy = false; }
}

/** Latest wins: a Python search keeps its result only if no newer search or settings change happened meanwhile. */
class Ticket {
  constructor() { this.n = 0; }
  next() { this.n += 1; return this.n; }
  isCurrent(t) { return t === this.n; }
}

/** How a finished backend run ended badly, if it did: "" (fine, or an error event already explains it),
 *  "compare_unfinished" (the team answered but the comparison never completed) or "failed" (crash, timeout, stop,
 *  Python missing, no result). Astra review 47: a team result must not hide a later failure. */
function endProblem(r, compareWanted) {
  const has = (k) => r.events.some((e) => e.event === k);
  if (has("error")) return "";
  const compared = !compareWanted || has("compare_note");
  if (!(r.error || r.code !== 0 || !has("result") || !compared)) return "";
  return has("result") && !compared ? "compare_unfinished" : "failed";
}

/** Run the backend once. Resolves {code, events, stderr, error}; `kill()` stops it. Never uses a shell. */
function runApp(opts) {
  const spawnFn = opts.spawnFn || childProcess.spawn;
  let child = null;
  const done = new Promise((resolve) => {
    const events = [];
    let stderr = "";
    let finished = false;
    let timer = null;
    const finish = (code, error) => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      resolve({ code, events, stderr: stderr.slice(-4000), error: error || null });
    };
    try {
      child = spawnFn(opts.python, opts.args, {
        cwd: opts.cwd, shell: false, windowsHide: true,
        env: Object.assign({}, process.env, { PYTHONUNBUFFERED: "1", NO_COLOR: "1", PYTHONIOENCODING: "utf-8" }),
      });
    } catch (e) {
      finish(-1, String((e && e.message) || e));
      return;
    }
    timer = setTimeout(() => {
      try { child.kill(); } catch (e) { /* already gone */ }
      finish(-1, "timed out");
    }, opts.timeoutMs || RUN_TIMEOUT_MS);
    const split = lineSplitter((line) => {
      const ev = parseEvent(line);
      if (ev) {
        events.push(ev);
        if (opts.onEvent) {
          try { opts.onEvent(ev); } catch (e) { /* a UI error never stops the run */ }
        }
      }
    });
    child.on("error", (e) => { finish(-1, String((e && e.message) || e)); });   // e.g. the Python command not found
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (d) => { split.push(d); });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (d) => {
      stderr += d;
      if (stderr.length > 20000) stderr = stderr.slice(-8000);
    });
    child.on("close", (code) => {
      split.end();
      finish(code, null);
    });
    child.stdin.on("error", () => { /* the process ended before reading its input */ });
    child.stdin.end(typeof opts.input === "string" ? opts.input : "");
  });
  return { done, kill: () => { try { if (child) child.kill(); } catch (e) { /* already gone */ } } };
}

// ------------------------------------------------------------------------------------------- the panel

class DispatchView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.job = null;
    this.rows = [];
    this.gate = new Gate();
    this.checkTicket = new Ticket();
    this.closed = false;
  }

  getViewType() { return VIEW; }
  getDisplayText() { return "Wave Dispatch"; }
  getIcon() { return "waves"; }

  async onOpen() {
    this.closed = false;                                 // a reopened panel is live again
    this.render();
    this.refreshStatus().then(() => { this.loadModels(); });
    this.registerEvent(this.app.vault.on("create", (f) => { if (f.path.startsWith("Runs/")) this.renderRecent(); }));
  }

  async onClose() {
    this.closed = true;                                  // also cancels a run still finding Python (review 51)
    this.stopped = true;
    if (this.job) this.job.kill();
  }

  render() {
    const el = this.contentEl;
    el.empty();
    el.addClass("wave-dispatch");
    el.createEl("h3", { text: "Wave Dispatch" });
    this.hintEl = el.createDiv({ cls: "wave-dispatch-hint" });

    const status = el.createDiv({ cls: "wave-dispatch-status" });
    this.statusText = status.createSpan({ text: "Checking the team…" });
    const check = status.createEl("button", { text: "Check" });
    check.addEventListener("click", () => { this.refreshStatus().then(() => { this.loadModels(); }); });

    this.input = el.createEl("textarea", { cls: "wave-dispatch-input", attr: { rows: "4", placeholder: "e.g. Write is_prime(n) in Python, and how many primes are below 100?" } });
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); this.run(); }
    });

    const sw = el.createDiv({ cls: "wave-dispatch-switches" });
    const toggle = (key, text, tip, defaultOn = true) => {
      const l = sw.createEl("label", { cls: "wave-dispatch-switch", attr: { title: tip } });
      const box = l.createEl("input", { attr: { type: "checkbox" } });
      box.checked = defaultOn ? this.plugin.settings[key] !== false : this.plugin.settings[key] === true;
      l.appendText(" " + text);
      box.addEventListener("change", async () => {
        this.plugin.settings[key] = box.checked;
        await this.plugin.saveSettings();
        this.renderHint();
      });
      return box;
    };
    this.notesBox = toggle("useNotes", "Use my notes",
      "On: when your question links a [[note]], the Graph Reader reads it and passes on only the facts. Off: no vault reads; the team answers from the models' own knowledge.");
    this.splitBox = toggle("split", "Split the question",
      "On: the Splitter (tiny model wave-split) splits a question with several asks into parts, so each part goes to the right bot. Python refuses a split that loses a number, link or function name.");
    this.reviewBox = toggle("review", "Review loop",
      "On: the Code Checker (tiny model wave-review) decides whether the code needs a review; if so the Try Again Coding Bot says what to fix and the Code Bot rewrites. Off: the result is ANSWERED, not VERIFIED.");
    this.renderHint();

    const controls = el.createDiv({ cls: "wave-dispatch-controls" });
    const label = controls.createEl("label", { cls: "wave-dispatch-compare-label" });
    this.compareBox = label.createEl("input", { attr: { type: "checkbox" } });
    this.compareBox.checked = !!this.plugin.settings.compare;
    label.appendText(" Compare with one model");
    this.modelSel = controls.createEl("select", { cls: "dropdown" });
    this.modelSel.createEl("option", { text: "(loading models…)", value: "" });
    this.modelSel.disabled = !this.compareBox.checked;
    this.compareBox.addEventListener("change", async () => {
      this.modelSel.disabled = !this.compareBox.checked;
      this.plugin.settings.compare = this.compareBox.checked;
      await this.plugin.saveSettings();
    });
    this.modelSel.addEventListener("change", async () => {
      this.plugin.settings.soloModel = this.modelSel.value;
      await this.plugin.saveSettings();
    });
    this.runBtn = controls.createEl("button", { cls: "mod-cta", text: "Ask the team" });
    this.runBtn.addEventListener("click", () => { this.run(); });
    this.stopBtn = controls.createEl("button", { text: "Stop" });
    this.stopBtn.disabled = true;
    this.stopBtn.addEventListener("click", () => {
      if (this.gate.busy) {                                // also while Python is still being found
        this.stopped = true;
        if (this.job) this.job.kill();
        new Notice("Wave Dispatch: stopped. A team run that had already finished keeps its run note and receipt; nothing else is written.");
      }
    });

    this.progressEl = el.createDiv({ cls: "wave-dispatch-progress" });
    this.resultEl = el.createDiv({ cls: "wave-dispatch-results" });
    el.createEl("h4", { text: "Recent runs" });
    this.recentEl = el.createDiv({ cls: "wave-dispatch-recent" });
    this.renderRecent();
  }

  renderHint() {
    const s = this.plugin.settings;
    this.hintEl.setText(s.useNotes === false
      ? "Notes are OFF: the team answers from the models' own knowledge; nothing in your vault is read."
      : "Ask the team of small local models. Link a [[Note]] in your question and the Graph Reader reads it.");
  }

  opts(command, extra, input, onEvent) {
    const s = this.plugin.settings;
    const base = this.plugin.vaultBase();
    const repo = repoDir(base, s.repo);
    const python = this.plugin.python || pythonCandidates(s.python, repo, process.platform)[0];
    return { python, args: appArgs(command, s, base, extra), cwd: repo, input, onEvent, spawnFn: this.plugin.spawnFn };
  }

  async refreshStatus() {
    this.statusText.setText("Checking the team…");
    this.statusText.className = "";
    const s = this.plugin.settings, base = this.plugin.vaultBase(), repo = repoDir(base, s.repo);
    // Two tickets (review 51): `epoch` changes only when the Python/folder SETTINGS change, and only then is a search
    // result thrown away; a newer Check never invalidates an older search (same settings, equally valid). `shown` keeps
    // only the newest check's text on screen.
    const epoch = this.plugin.pythonTicket.n;
    const shown = this.checkTicket.next();
    const found = await findPython(pythonCandidates(s.python, repo, process.platform), appArgs("status", s, base), repo,
                                   this.plugin.spawnFn);
    if (!this.plugin.pythonTicket.isCurrent(epoch)) return null;   // the settings changed meanwhile: discard
    this.plugin.python = found.python;                   // null if nothing works any more (e.g. the .venv was removed)
    this.tried = found.tried;
    if (!this.checkTicket.isCurrent(shown)) return found.python;   // a newer check owns the status line
    const r = found.result;
    const st = r.events.find((e) => e.event === "status");
    const err = r.events.find((e) => e.event === "error");
    if (st && st.ready) {
      const m = (st.machines || []).map((x) => `${x.name}: ${x.provider}`).join(", ");
      this.statusText.setText(`● Ready · ${(st.bots || []).length} bots · ${m}`);
      this.statusText.className = "wave-dispatch-ok";
    } else if (st) {
      this.statusText.setText(`● Not ready: ${(st.problems || [])[0] || "unknown problem"}` + (st.hint ? ` (${st.hint})` : ""));
      this.statusText.className = "wave-dispatch-warn";
    } else {
      this.statusText.setText(this.startError(r, err));
      this.statusText.className = "wave-dispatch-warn";
    }
    return found.python;
  }

  startError(r, err) {
    if (err) return `● ${err.message}` + (err.hint ? ` (${err.hint})` : "");
    if (!this.plugin.python && this.tried) {
      return `● No usable Python 3.10+ with pyyaml found (tried: ${this.tried.join(", ")}). Run ./setup.sh, or set the Python command in Settings → Wave Dispatch.`;
    }
    if (r.error) return `● Can't start Python ("${this.plugin.python || this.plugin.settings.python}"): ${r.error}. Set the Python command in Settings → Wave Dispatch.`;
    return `● The backend stopped (exit ${r.code}). ${r.stderr ? r.stderr.split("\n").slice(-3).join(" ") : ""}`;
  }

  async loadModels() {
    if (!this.plugin.python) return;                       // never guess: only a Python the status check confirmed
    const r = await runApp(this.opts("models")).done;
    const ev = r.events.find((e) => e.event === "models");
    const list = ev && ev.machines && Array.isArray(ev.machines["This Computer"]) ? ev.machines["This Computer"] : [];
    this.modelSel.empty();
    if (!list.length) {
      this.modelSel.createEl("option", { text: "(no models found)", value: "" });
      return;
    }
    for (const m of list) {
      const size = typeof m.size_gb === "number" && m.size_gb > 0 ? ` · ${m.size_gb.toFixed(1)} GB` : "";
      this.modelSel.createEl("option", { text: String(m.name) + size, value: String(m.name) });
    }
    const keep = this.plugin.settings.soloModel;
    if (keep && list.some((m) => m.name === keep)) this.modelSel.value = keep;
  }

  setRunning(on) {
    this.runBtn.disabled = on;
    this.stopBtn.disabled = !on;
    this.input.disabled = on;
  }

  addRow(text) {
    for (const r of this.rows) this.markDone(r);
    const row = this.progressEl.createDiv({ cls: "wave-dispatch-row is-running" });
    row.createSpan({ cls: "wave-dispatch-dot", text: "◐" });
    row.createSpan({ text: " " + text });
    this.rows.push(row);
    return row;
  }

  markDone(row) {
    if (!row.hasClass("is-running")) return;
    row.removeClass("is-running");
    row.addClass("is-done");
    const dot = row.querySelector(".wave-dispatch-dot");
    if (dot) dot.textContent = "●";
  }

  async run() {
    const prompt = this.input.value.trim();
    if (!prompt) { new Notice("Wave Dispatch: type a question first."); return; }
    const solo = this.compareBox.checked ? this.modelSel.value : "";
    if (this.compareBox.checked && !solo) { new Notice("Wave Dispatch: pick a model to compare with (or untick Compare)."); return; }
    if (!this.gate.tryEnter()) return;                     // reserved BEFORE any await (Astra review 50)
    this.setRunning(true);
    this.stopped = false;
    let r;
    try {
      this.progressEl.empty();
      this.resultEl.empty();
      this.resultEl.toggleClass("is-compare", !!solo);
      this.rows = [];
      this.teamCard = null;
      this.soloCard = null;
      for (let i = 0; i < 2 && !this.plugin.python && !this.stopped; i++) {
        await this.refreshStatus();                      // find Python first (a Dock-started Obsidian has a minimal PATH);
      }                                                  // a second try only if the settings changed meanwhile
      if (this.stopped || this.closed) { if (!this.closed) this.errorCard("Stopped.", ""); return; }
      if (!this.plugin.python) {                         // never fall back to a guessed command (review 51)
        this.errorCard(this.startError({ code: null, error: null, stderr: "", events: [] }, null), "");
        return;
      }
      this.job = runApp(this.opts("run", runExtra(this.plugin.settings, solo), prompt, (ev) => { this.onEvent(ev); }));
      r = await this.job.done;
    } finally {
      this.job = null;
      this.gate.leave();
      this.setRunning(false);
    }
    for (const row of this.rows) this.markDone(row);
    // Report every way a run can end badly, also AFTER the team's result (a crash, timeout or Stop during the
    // comparison): Astra review 47. An error event already has its own card.
    const problem = endProblem(r, !!solo);
    if (problem) {
      const why = this.stopped ? "Stopped." : this.startError(r, null);
      this.errorCard(problem === "compare_unfinished" ? "The comparison did not finish. " + why : why, "");
    }
    this.renderRecent();
  }

  onEvent(ev) {
    if (ev.event === "step") {
      this.addRow(`${stageLabel(ev.stage)} · ${ev.bot} · ${ev.model} on ${ev.machine}`);
    } else if (ev.event === "result") {
      for (const row of this.rows) this.markDone(row);
      const links = [];
      if (ev.run_note) links.push(["Open run note", ev.run_note]);
      if (ev.receipt_note) links.push(["Open receipt", ev.receipt_note]);
      const mode = modeLabel(ev.mode);
      this.teamCard = this.card("Wave Dispatch team", `${badge(ev.status)} · ${ev.seconds} s` + (mode ? ` · ${mode}` : "")
                                + (ev.error ? ` · ${ev.error}` : ""),
                                ev.answer || ev.error, links);
      const route = routeLines(ev.parts, ev.routes).join("\n")
        + (ev.split_refused ? `\nSplit refused by Python (${ev.split_refused}): the question went on whole.` : "");
      this.teamCard.createEl("pre", { cls: "wave-dispatch-route", text: "Parts and bots:\n" + route });
    } else if (ev.event === "solo_start") {
      this.addRow(`One model · ${ev.model} on ${ev.machine}`);
    } else if (ev.event === "solo") {
      for (const row of this.rows) this.markDone(row);
      const sub = (ev.error ? "❌ error" : ev.truncated ? "⚠️ cut off (token limit)" : "◻️ not checked") + ` · ${ev.seconds} s`;
      this.soloCard = this.card(`One model · ${ev.model}`, sub, ev.answer || ev.error, []);
    } else if (ev.event === "compare_note") {
      const b = (this.soloCard || this.resultEl).createEl("button", { text: "Open comparison note" });
      b.addEventListener("click", () => { this.openNote(ev.path); });
    } else if (ev.event === "error") {
      this.errorCard(ev.message, ev.hint);
    }
  }

  card(title, sub, answer, links) {
    const c = this.resultEl.createDiv({ cls: "wave-dispatch-card" });
    c.createDiv({ cls: "wave-dispatch-card-title", text: title });
    if (sub) c.createDiv({ cls: "wave-dispatch-card-sub", text: sub });
    c.createEl("pre", { cls: "wave-dispatch-answer", text: answer || "(no answer)" });   // plain text: never rendered
    const row = c.createDiv({ cls: "wave-dispatch-links" });
    for (const [label, path] of links) {
      const b = row.createEl("button", { text: label });
      b.addEventListener("click", () => { this.openNote(path); });
    }
    return c;
  }

  errorCard(message, hint) {
    const c = this.resultEl.createDiv({ cls: "wave-dispatch-card wave-dispatch-error" });
    c.createDiv({ cls: "wave-dispatch-card-title", text: "❌ " + String(message || "error") });
    if (hint) c.createDiv({ cls: "wave-dispatch-card-sub", text: String(hint) });
  }

  async openNote(path) {
    const f = this.app.vault.getAbstractFileByPath(String(path));
    if (f instanceof TFile) {
      await this.app.workspace.getLeaf(false).openFile(f);
    } else {
      new Notice("Wave Dispatch: note not found: " + path);
    }
  }

  renderRecent() {
    if (!this.recentEl) return;
    this.recentEl.empty();
    const files = this.app.vault.getMarkdownFiles()
      .filter((f) => f.path.startsWith("Runs/Run ") || f.path.startsWith("Runs/Compare "))
      .sort((a, b) => b.basename.slice(-24).localeCompare(a.basename.slice(-24)) || b.basename.localeCompare(a.basename))
      .slice(0, 8);
    if (!files.length) {
      this.recentEl.createDiv({ cls: "wave-dispatch-hint", text: "No runs yet." });
      return;
    }
    for (const f of files) {
      const fm = (this.app.metadataCache.getFileCache(f) || {}).frontmatter || {};
      const status = fm.type === "compare" ? `${badge(fm.team_status)} vs ${fm.solo_model_id || "one model"}` : badge(fm.status);
      const row = this.recentEl.createDiv({ cls: "wave-dispatch-recent-row" });
      row.createSpan({ text: f.basename });
      row.createSpan({ cls: "wave-dispatch-card-sub", text: " · " + status });
      row.addEventListener("click", () => { this.openNote(f.path); });
    }
  }
}

// ------------------------------------------------------------------------------------------- settings + plugin

class DispatchSettings extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const el = this.containerEl;
    el.empty();
    const save = async () => { await this.plugin.saveSettings(); };
    new Setting(el).setName("Python command").setDesc("Leave empty to find Python 3.10+ automatically (the project's .venv first). Or give a command or full path, e.g. /opt/homebrew/bin/python3 or py.")
      .addText((t) => { t.setPlaceholder("(automatic)").setValue(this.plugin.settings.python).onChange(async (v) => { this.plugin.settings.python = v.trim(); this.plugin.resetPython(); await save(); }); });
    new Setting(el).setName("Wave Dispatch folder").setDesc("The folder with wave_dispatch_app.py. Leave empty to use the folder that contains this vault.")
      .addText((t) => { t.setPlaceholder("(the folder above this vault)").setValue(this.plugin.settings.repo).onChange(async (v) => { this.plugin.settings.repo = v.trim(); this.plugin.resetPython(); await save(); }); });
    new Setting(el).setName("Endpoint").setDesc("Optional: use another model server for This Computer from this panel (e.g. http://127.0.0.1:8080/v1). Empty = the Machine note.")
      .addText((t) => { t.setValue(this.plugin.settings.endpoint).onChange(async (v) => { this.plugin.settings.endpoint = v.trim(); await save(); }); });
    new Setting(el).setName("Provider").setDesc("Only with an endpoint above: Ollama or an OpenAI-compatible server.")
      .addDropdown((d) => { d.addOptions({ "": "from the Machine note", ollama: "ollama", openai: "openai" }).setValue(this.plugin.settings.provider || "").onChange(async (v) => { this.plugin.settings.provider = v; await save(); }); });
  }
}

class DispatchPlugin extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULTS, (await this.loadData()) || {});
    this.python = null;
    this.pythonTicket = new Ticket();
    this.registerView(VIEW, (leaf) => new DispatchView(leaf, this));
    this.addRibbonIcon("waves", "Wave Dispatch", () => { this.activate(); });
    this.addCommand({ id: "open-panel", name: "Open the Wave Dispatch panel", callback: () => { this.activate(); } });
    this.addSettingTab(new DispatchSettings(this.app, this));
  }

  resetPython() {                                        // a new Python/folder setting: forget the old one and
    this.python = null;                                  // invalidate any search still running (Astra review 50)
    this.pythonTicket.next();
  }

  vaultBase() {
    const a = this.app.vault.adapter;
    return a && typeof a.getBasePath === "function" ? a.getBasePath() : "";
  }

  async activate() {
    let leaf = this.app.workspace.getLeavesOfType(VIEW)[0];
    if (!leaf) {
      leaf = this.app.workspace.getRightLeaf(false) || this.app.workspace.getLeaf(true);
      await leaf.setViewState({ type: VIEW, active: true });
    }
    this.app.workspace.revealLeaf(leaf);
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }
}

module.exports = DispatchPlugin;
module.exports.default = DispatchPlugin;
module.exports.helpers = { repoDir, appArgs, lineSplitter, parseEvent, badge, stageLabel, runApp, endProblem, pythonCandidates,
                           findPython, Gate, Ticket, DispatchView, DEFAULTS, VIEW, runExtra, modeLabel, routeLines };
