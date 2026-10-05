#!/usr/bin/env python3
"""ccr - multi-select resume picker for Claude Code + Codex CLI sessions.

Native macOS/Linux port of Resume-CcSessions.ps1 (same data sources, same
rules, same features - the two are kept aligned change for change), with fzf
as the picker and real terminal tabs (iTerm2, Terminal.app) or tmux windows
as the launch backend. Python 3.9+, stdlib only; fzf for UI.

Keys in the picker (fzf conventions): type to fuzzy-filter, Tab marks,
Enter opens, Ctrl-E opens with a model / effort, Ctrl-N new conversation, Ctrl-P MAP (multi-agent panel), Ctrl-A accounts, Ctrl-O open the
marked rows under another account, Ctrl-T installs or updates claude / codex, Del deletes, Esc cancels.
"""
import argparse
import base64
import ctypes
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Shown in the picker hint line; bumped together with $script:CcrVersion in
# Resume-CcSessions.ps1 - the two scripts move in lockstep.
VERSION = "0.74"
UUID_IN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile("^" + UUID_IN + "$")
HOME = Path.home()
# CCR_FZF may be a full command (e.g. "fzf-tmux -p"); split like a shell would.
FZF = shlex.split(os.environ.get("CCR_FZF") or "fzf")

# Optional multi-account config: $CCR_CONFIG, else ~/.config/ccr/ccr.json
# (same keys as the PowerShell version's ccr.json: claudeRoots, codexRoots,
# defaultRoot).
CONFIG_PATH = Path(os.environ.get("CCR_CONFIG")
                   or Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "ccr" / "ccr.json")

ORANGE, CYAN, RED, YELLOW, DIM, RESET = "\033[38;5;208m", "\033[36m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
GREEN, BOLD, MAGENTA = "\033[32m", "\033[1m", "\033[35m"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")
TOOL_COLOR = {"claude": ORANGE, "codex": CYAN}
# The env var that selects a tool's data dir for one process.
ROOT_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
# Multi-account mode starts by giving the dirs the tools use today a label,
# so the sessions already there keep an account name: that label is fixed,
# "default", and nothing moves.
DEFAULT_LABEL = "default"
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
LABEL_HINT = "1-12 letters/digits/_/-"


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def right_align(line: str, tail: str) -> str:
    """Push tail to the right edge, counting only the characters that show.

    fzf indents header lines past the pointer/marker gutter, so the usable
    width is a few columns short of the terminal's."""
    width = shutil.get_terminal_size((100, 24)).columns - 4
    gap = max(3, width - visible_len(line) - visible_len(tail))
    return line + " " * gap + tail


def hint(*pairs, tail: str = "") -> str:
    """A 'key action' legend: keys carry the colour, actions stay quiet."""
    sep = f"{DIM}  ·  {RESET}"
    line = sep.join(f"{BOLD}{CYAN}{k}{RESET} {DIM}{v}{RESET}" for k, v in pairs)
    return right_align(line, f"{DIM}{tail}{RESET}") if tail else line


def hint_wrapped(pairs, width: int = 0) -> list:
    """A key legend broken into as many lines as the terminal needs: fzf cuts
    a header line that does not fit, which hid the keys at its end."""
    width = width or shutil.get_terminal_size((100, 24)).columns - 4
    lines, cur = [], []
    for pr in pairs:
        if cur and visible_len(hint(*(cur + [pr]))) > width:
            lines.append(hint(*cur))
            cur = [pr]
        else:
            cur = cur + [pr]
    if cur:
        lines.append(hint(*cur))
    return lines


def ask(prompt: str):
    """input() that returns None on Ctrl-C / EOF instead of raising."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def pause(msg: str = "ccr: press Enter to go back to the picker"):
    ask(f"{DIM}{msg}{RESET} ")


# ----------------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------------
def claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def tool_default_root(tool: str) -> str:
    return str(claude_root() if tool == "claude" else codex_root())


def read_window(path: Path, nbytes: int, tail: bool = False) -> str:
    """First/last nbytes of a file as text (claude/codex may be appending)."""
    with open(path, "rb") as f:
        if tail:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
        return f.read(nbytes).decode("utf-8", errors="replace")


def json_unescape(raw: str):
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def clean_title(raw):
    """One-line, control-char-free, length-capped title (or None)."""
    if not raw:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(raw))).strip()
    if not t:
        return None
    return t[:99] + "…" if len(t) > 100 else t


def parse_ts(s: str):
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def mtime_utc(p: Path):
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)


def history_table(path: Path, id_key: str) -> dict:
    """First-wins table of one parsed JSONL object per id (title accelerator)."""
    table = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                k = o.get(id_key)
                if k and k not in table:
                    table[k] = o
    except OSError:
        pass
    return table


def fmt_age(utc: datetime) -> str:
    span = datetime.now(timezone.utc) - utc
    m = span.total_seconds() / 60
    if m < 1:
        return "now"
    if m < 60:
        return f"{int(m)}m"
    if m < 1440:
        return f"{int(m // 60)}h"
    if span.days < 14:
        return f"{span.days}d"
    loc = utc.astimezone()
    return f"{loc:%b} {loc.day}"


def fmt_cwd(path: str, maxlen: int) -> str:
    if not path:
        return ""
    p = path
    home = str(HOME)
    if p.lower().startswith(home.lower()):
        p = "~" + p[len(home):]
    if len(p) > maxlen:
        segs = [s for s in re.split(r"[\\/]", p) if s]
        if len(segs) >= 2:
            p = "…" + os.sep + os.sep.join(segs[-2:])
    if len(p) > maxlen and maxlen >= 2:
        p = p[: maxlen - 1] + "…"
    return p


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) would TERMINATE the process on Windows.
        # A handle alone is not proof: a process that has exited stays
        # openable while anyone holds a handle to it. Ask for the exit code.
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def user_text(obj):
    """Text of a claude 'user' line's message content (str or content blocks)."""
    c = (obj.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    return None


class Session:
    __slots__ = ("tool", "id", "title", "cwd", "last", "running", "pid", "source",
                 "started_at", "started_by_clear", "head_bridge", "tail_bridge", "cleared",
                 "origin", "root", "root_path", "target_root",
                 # claude registry: open on another PC (host, "" otherwise), and the
                 # kind ("bg" = a background session) / status / last update of the run
                 "running_on", "run_kind", "run_status", "run_updated", "run_job",
                 # model / effort chosen with Ctrl-E ('' = no flag)
                 "model_override", "effort_override")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.cleared = bool(self.cleared)
        self.origin = self.origin or "cli"
        self.root = self.root or ""
        self.running_on = self.running_on or ""
        self.run_kind = self.run_kind or ""
        self.run_status = self.run_status or ""
        self.model_override = self.model_override or ""
        self.effort_override = self.effort_override or ""

    @property
    def key(self):
        return f"{self.tool}|{self.id}"


# ----------------------------------------------------------------------------
# accounts: one data dir per account and tool (ccr.json)
# ----------------------------------------------------------------------------
class Root:
    """One configured data dir of a tool: label ('' = the single default),
    absolute path, and whether it is the default account."""
    __slots__ = ("label", "path", "default")

    def __init__(self, label: str, path: str, default: bool):
        self.label, self.path, self.default = label, path, default


def acct_label(label: str, is_default: bool) -> str:
    """How an account label is shown: the default account always in parentheses."""
    return f"({label})" if is_default else label


def expand_path(p: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expandvars(os.path.expanduser(p))))


def load_config():
    """Parsed ccr.json (dict), or None when absent/unreadable."""
    if not CONFIG_PATH.is_file():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ccr: cannot read {CONFIG_PATH} ({e}) - using the single default config dirs", file=sys.stderr)
        return None


def portable_path(p: str) -> str:
    """A dir under the home folder written as "~/...": ccr.json then travels
    between PCs (a synced script dir) as long as the dirs sit at the same
    place under each home. get_roots expands "~" back per machine."""
    home = str(HOME)
    if p and p.lower().startswith(home.lower()) and (len(p) == len(home) or p[len(home)] in "\\/"):
        return "~" + p[len(home):]
    return p


def save_config(cfg: dict):
    """Write ccr.json back. Only the keys ccr owns are touched. A file that
    exists but does not parse is never overwritten: it may hold accounts
    the user can recover by fixing a stray character."""
    if CONFIG_PATH.is_file():
        try:
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"ccr: {CONFIG_PATH} exists but cannot be parsed ({e}) - fix or remove it first; "
                               "nothing overwritten")
    for key in ("claudeRoots", "codexRoots"):
        if isinstance(cfg.get(key), dict):
            cfg[key] = {k: portable_path(str(v)) for k, v in cfg[key].items()}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def get_roots(tool: str) -> list:
    """All config dirs ccr should scan for one tool. One account (no
    ccr.json, or no entry for the tool): a single unlabeled root = the
    tool's default dir, so behavior is unchanged. Several accounts: ccr.json
    lists one dir per account label, each with its own login inside:
      { "claudeRoots": { "default": "~/.claude", "work": "~/.claude-work" },
        "codexRoots":  { "default": "~/.codex",  "work": "~/.codex-work" },
        "defaultRoot": "default" }
    "~" and $VAR expand; a missing dir is kept (it may exist on another PC)."""
    single = [Root("", tool_default_root(tool), True)]
    cfg = load_config()
    if not cfg:
        return single
    m = cfg.get(tool + "Roots")
    if not isinstance(m, dict) or not m:
        return single
    default = str(cfg.get("defaultRoot") or "")
    roots = [Root(k, expand_path(str(v)), k == default) for k, v in m.items()]
    if not any(r.default for r in roots):
        roots[0].default = True
    return roots


def default_root(tool: str) -> Root:
    return next(r for r in get_roots(tool) if r.default)


def tool_env(tool: str, path: str) -> dict:
    """A copy of the environment with the tool's data dir selected."""
    env = dict(os.environ)
    env[ROOT_VAR[tool]] = path
    return env


def login_identity(tool: str, path: str) -> str:
    """Who is logged in inside a config dir, without touching the live
    default dir: run the tool's own status command with the dir selected.
    A status check that hangs on the network is cut after 60 s."""
    if not Path(path).is_dir():
        return "not on this PC"
    try:
        if tool == "claude":
            r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True,
                               env=tool_env(tool, path), timeout=60)
            try:
                j = json.loads(r.stdout)
            except Exception:
                return "(unknown)"
            if not j.get("loggedIn"):
                return "not logged in"
            who = j.get("email") or j.get("authMethod") or "?"
            return f"{who} ({j['subscriptionType']})" if j.get("subscriptionType") else str(who)
        # codex writes its status line to stderr.
        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True,
                           env=tool_env(tool, path), timeout=60)
        for line in (r.stderr or "").splitlines() + (r.stdout or "").splitlines():
            line = line.strip()
            if line and not line.startswith("WARNING"):
                return line
        return "logged in (auth.json present)" if (Path(path) / "auth.json").exists() else "not logged in"
    except subprocess.TimeoutExpired:
        return "(no answer in 60s)"
    except Exception:
        return "(unknown)"


def quick_identity(tool: str, path: str) -> str:
    """The logged-in email of a config dir, read from the files the tools
    keep there - no process spawned, so cheap enough for every picker start.
    Claude: .claude.json oauthAccount.emailAddress. Codex: the email claim
    of the OpenID token in auth.json. '' when there is no login there."""
    try:
        if tool == "claude":
            f = Path(path) / ".claude.json"
            if not f.is_file():
                return ""
            return str(((json.loads(f.read_text(encoding="utf-8")).get("oauthAccount") or {}).get("emailAddress")) or "")
        f = Path(path) / "auth.json"
        if not f.is_file():
            return ""
        jwt = str(((json.loads(f.read_text(encoding="utf-8")).get("tokens") or {}).get("id_token")) or "")
        if not jwt:
            return ""
        b = jwt.split(".")[1]
        b += "=" * ((4 - len(b) % 4) % 4)
        return str(json.loads(base64.urlsafe_b64decode(b)).get("email") or "")
    except Exception:
        return ""


def who_at(tool: str, path: str) -> str:
    """The login shown next to a dir everywhere in the picker: the email from
    the dir's own files, else why there is none."""
    return quick_identity(tool, path) or ("not on this PC" if not os.path.isdir(path) else "not logged in")


def show_accounts():
    cfg = load_config()
    if not cfg or not (cfg.get("claudeRoots") or cfg.get("codexRoots")):
        print("ccr: no accounts configured (single default config dirs). Add one with: ccr --add-account <label>")
        print(f"  config file: {CONFIG_PATH}")
        return
    print(f"accounts in {CONFIG_PATH}  (default: {cfg.get('defaultRoot')})")
    for tool in ("claude", "codex"):
        roots = get_roots(tool)
        if len(roots) == 1 and not roots[0].label:
            print(f"  {tool}: single default dir {roots[0].path}")
            continue
        for r in roots:
            who = login_identity(tool, r.path)
            print(f"  {tool:<6} {r.label:<12} {fmt_cwd(r.path, 45):<45} {who}")


def is_multi_account() -> bool:
    """Is multi-account mode on (ccr.json lists at least one account)?"""
    cfg = load_config()
    return bool(cfg and (cfg.get("claudeRoots") or cfg.get("codexRoots")))


def enable_multi_account() -> dict:
    """Turn multi-account mode on: record the current default dir of each
    tool under the "default" label. Returns the config; no-op when on.
    Seeded per tool: a by-hand ccr.json may list only one tool's roots, and
    the other tool must still keep its current dir as an account once it
    gets a second one (otherwise its real default would vanish)."""
    cfg = load_config() or {}
    for key in ("claudeRoots", "codexRoots"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    was_on = bool(cfg["claudeRoots"] or cfg["codexRoots"])
    seeded = []
    for tool in ("claude", "codex"):
        if not cfg[tool + "Roots"]:
            cfg[tool + "Roots"][DEFAULT_LABEL] = tool_default_root(tool)
            seeded.append(tool)
    if not cfg.get("defaultRoot"):
        cfg["defaultRoot"] = DEFAULT_LABEL
    if not seeded:
        return cfg
    save_config(cfg)
    if was_on:
        print(f"{GREEN}ccr: the dir {' and '.join(seeded)} uses today is recorded as the "
              f"'{DEFAULT_LABEL}' account (nothing moved).{RESET}")
    else:
        print(f"{GREEN}ccr: multi-account mode is on - the dirs claude and codex use today are the "
              f"'{DEFAULT_LABEL}' account (nothing moved).{RESET}")
    return cfg


def has_statusline(path: str) -> bool:
    """Does this claude config dir carry a status line (statusLine in settings.json)?"""
    sj = Path(path) / "settings.json"
    if not sj.is_file():
        return False
    try:
        return "statusLine" in json.loads(sj.read_text(encoding="utf-8"))
    except Exception:
        return False


def copy_statusline(from_path: str, to_path: str):
    """Give a claude account the status line of another one: the statusLine
    entry is merged into the target's settings.json (other keys untouched)
    and the statusline* script files next to it are copied over (logs
    excluded). Claude resolves the script through CLAUDE_CONFIG_DIR at run
    time, so the copy works unchanged in the new dir."""
    src = Path(from_path) / "settings.json"
    if not has_statusline(from_path):
        raise RuntimeError(f"ccr: no statusLine in {src} - nothing to copy")
    entry = json.loads(src.read_text(encoding="utf-8"))["statusLine"]
    Path(to_path).mkdir(parents=True, exist_ok=True)
    dst = Path(to_path) / "settings.json"
    cfg = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else {}
    cfg["statusLine"] = entry
    dst.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    files = [f for f in Path(from_path).glob("statusline*") if f.is_file() and f.suffix not in (".log", ".txt")]
    for f in files:
        shutil.copy2(f, Path(to_path) / f.name)
    print(f"ccr: status line copied to {fmt_cwd(to_path, 50)}: statusLine in settings.json"
          + (" + " + ", ".join(f.name for f in files) if files else ""))


def has_codex_config(path: str) -> bool:
    return (Path(path) / "config.toml").is_file()


def copy_codex_config(from_path: str, to_path: str):
    """Codex's counterpart of the status line copy: config.toml (model,
    effort, features, per-project trust) lives in CODEX_HOME and is plain to copy."""
    src = Path(from_path) / "config.toml"
    if not src.is_file():
        raise RuntimeError(f"ccr: no config.toml in {from_path} - nothing to copy")
    Path(to_path).mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, Path(to_path) / "config.toml")
    print(f"ccr: config.toml copied to {fmt_cwd(to_path, 50)}")


def settings_info(tool: str) -> dict:
    """What "copy settings from the default account" means per tool."""
    if tool == "claude":
        return {"text": "Copy statusline from default account",
                "note": "statusLine in settings.json + statusline* files", "what": "status line"}
    return {"text": "Copy config.toml from default account",
            "note": "model, effort, features, project trust", "what": "config.toml"}


def has_settings(tool: str, path: str) -> bool:
    return has_statusline(path) if tool == "claude" else has_codex_config(path)


def copy_settings(tool: str, from_path: str, to_path: str):
    if tool == "claude":
        copy_statusline(from_path, to_path)
    else:
        copy_codex_config(from_path, to_path)


def add_account(label: str, tool: str = "all", copy: bool = False):
    """Register a new account for one tool (or both): a config dir NEXT TO
    the tool's default dir (~/.claude-<label> next to ~/.claude), the tool's
    own interactive login run inside it - skipped when the dir already holds
    a login from an earlier life - and the entry recorded in ccr.json.
    Turns multi-account mode on first when needed."""
    if not LABEL_RE.match(label):
        raise RuntimeError(f"ccr: account label must be {LABEL_HINT} (got '{label}')")
    if label == DEFAULT_LABEL:
        raise RuntimeError(f"ccr: '{label}' is the label of the dirs in use today - pick another one")
    cfg = enable_multi_account()
    for t in (("claude", "codex") if tool == "all" else (tool,)):
        m = cfg[t + "Roots"]
        def_path = default_root(t).path
        # An account already configured (e.g. added on another PC through a
        # synced ccr.json) keeps its dir: created here when missing, the
        # login run inside it; with a login already inside, nothing to do.
        known = label in m
        d = expand_path(str(m[label])) if known else os.path.join(os.path.dirname(def_path), f"{os.path.basename(def_path)}-{label}")
        reused = os.path.isdir(d)
        Path(d).mkdir(parents=True, exist_ok=True)
        if not known:
            m[label] = d
            save_config(cfg)   # record first, so an aborted login still leaves a usable entry
        if t == "claude":
            # A fresh dir has no .claude.json, and the first interactive
            # claude there runs the first-start wizard (theme, login...)
            # even though `claude auth login` already stored credentials.
            # Seed the flag it checks, plus the default account's theme.
            cj = Path(d) / ".claude.json"
            if not cj.exists():
                seed = {"hasCompletedOnboarding": True}
                try:
                    def_cj = Path(def_path) / ".claude.json"
                    if def_cj.is_file():
                        dj = json.loads(def_cj.read_text(encoding="utf-8"))
                        for k in ("theme", "lastOnboardingVersion"):
                            if k in dj:
                                seed[k] = dj[k]
                except Exception:
                    pass
                cj.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
        if copy:
            try:
                copy_settings(t, def_path, d)
            except Exception as e:
                print(str(e), file=sys.stderr)
        has_login = (Path(d) / (".credentials.json" if t == "claude" else "auth.json")).exists()
        if reused and has_login:
            already = quick_identity(t, d)
            why = "already configured and logged in" if known else "existing dir, already logged in"
            print(f"{YELLOW}ccr: {t} account '{label}' -> {d}  - {why}"
                  f"{' as ' + already if already else ''}; no login needed{RESET}")
            continue
        why = ("configured but not on this PC yet; " if known and not reused
               else "configured but not logged in here; " if known else "")
        print(f"{YELLOW}ccr: {t} account '{label}' -> {d}  - {why}starting the {t} login flow in that dir{RESET}")
        argv = ["claude", "auth", "login"] if t == "claude" else ["codex", "login"]
        try:
            subprocess.run(argv, env=tool_env(t, d))
        except OSError as e:
            print(f"ccr: cannot start {argv[0]}: {e}", file=sys.stderr)
    print()
    show_accounts()


def remove_account(label: str, tool: str = "all"):
    """Forget an account: every session it holds moves into the tool's
    default account first (claude: transcript + sidecar into the same
    project slug; codex: rollout into the same sessions/YYYY/MM/DD path),
    then the entry leaves ccr.json. The dir and its login stay on disk.
    Refused while one of its sessions is running, and for the default."""
    cfg = load_config()
    if not cfg:
        raise RuntimeError("ccr: no accounts configured")
    plan = []
    for t in (("claude", "codex") if tool == "all" else (tool,)):
        roots = get_roots(t)
        src = [r for r in roots if r.label == label]
        if not src:
            continue
        dst = next(r for r in roots if r.default)
        if dst.label == label:
            raise RuntimeError(f"ccr: '{label}' is the default {t} account - it cannot be removed; "
                               "turn multi-account mode off instead")
        sessions = claude_sessions(src[0]) if t == "claude" else codex_sessions(src[0])
        running = [s for s in sessions if s.running or s.running_on]
        if running:
            raise RuntimeError(f"ccr: {len(running)} {t} session(s) of '{label}' are running (here or on another PC) "
                               "- close them first")
        plan.append((t, src[0], dst, sessions))
    if not plan:
        raise RuntimeError(f"ccr: no account '{label}' configured")
    for t, src, dst, sessions in plan:
        for s in sessions:
            move_session_to_root(s, dst)
        cfg[t + "Roots"].pop(label, None)
        print(f"ccr: {t} account '{label}' removed - {len(sessions)} session(s) moved to '{dst.label}' "
              f"({fmt_cwd(dst.path, 50)}); the dir {fmt_cwd(src.path, 50)} and its login stay on disk.")
    save_config(cfg)


def disable_multi_account():
    """Turn multi-account mode off: every other account's sessions move into
    the default account of its tool (see remove_account), then the account
    keys leave ccr.json (the file goes too when nothing else is in it), so
    both tools are back to their single default dir."""
    if not is_multi_account():
        print("ccr: multi-account mode is not on.")
        return
    for t in ("claude", "codex"):
        dst = default_root(t)
        plain = tool_default_root(t)
        if dst.label and expand_path(dst.path) != expand_path(plain):
            raise RuntimeError(f"ccr: the default {t} account lives in {dst.path}, but {t} itself uses {plain} "
                               "- the sessions would disappear from ccr. Make them the same dir first.")
    labels = []
    for r in get_roots("claude") + get_roots("codex"):
        if r.label and not r.default and r.label not in labels:
            labels.append(r.label)
    for lbl in labels:
        remove_account(lbl)
    cfg = load_config() or {}
    for key in ("claudeRoots", "codexRoots", "defaultRoot"):
        cfg.pop(key, None)
    if cfg:
        save_config(cfg)
    else:
        CONFIG_PATH.unlink(missing_ok=True)
    print(f"{GREEN}ccr: multi-account mode is off - claude and codex are back to their single default dirs.{RESET}")


def move_session_to_root(s: Session, target: Root) -> str:
    """Re-home a conversation: move its files into another account's data
    dir. Transcripts are not account-bound and both tools resume a moved
    file (a codex home indexes it on first resume). Returns the new path."""
    src = Path(s.source)
    if s.tool == "claude":
        dst_dir = Path(target.path) / "projects" / src.parent.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst_dir / src.name))
        side = src.parent / s.id   # custom title, tool results...
        if side.exists():
            shutil.move(str(side), str(dst_dir / side.name))
        return str(dst_dir / src.name)
    # codex: keep the sessions/YYYY/MM/DD layout relative to the source root.
    rel = src.relative_to(Path(s.root_path) / "sessions")
    dst = Path(target.path) / "sessions" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    # A /rename name lives in the source account's catalog, not in the
    # rollout. Carry it over through session_index.jsonl - the legacy index
    # codex still honours - rather than writing into its sqlite catalog.
    # Best effort: a name that cannot be read or written is just not moved.
    try:
        name = codex_curated_name(s.root_path, s.id)
        if name:
            add_codex_index_name(target.path, s.id, name)
    except Exception as e:
        if os.environ.get("CCR_DEBUG"):
            print(f"ccr: thread name of {s.id} not carried over: {e}", file=sys.stderr)
    return str(dst)


_CODEX_NAMES = {}   # root path -> codex_title_map(root path)


def codex_curated_name(root_path: str, sid: str) -> str:
    """The /rename name of one codex thread in one account (catalog + legacy
    index), from a per-root cache; '' when none."""
    if root_path not in _CODEX_NAMES:
        _CODEX_NAMES[root_path] = codex_title_map(root_path)
    return str(_CODEX_NAMES[root_path].get(sid) or "")


def add_codex_index_name(root_path: str, sid: str, name: str):
    """Append one {"id","thread_name","updated_at"} line to an account's
    session_index.jsonl, the format codex writes there (last entry wins)."""
    idx = Path(root_path) / "session_index.jsonl"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    line = json.dumps({"id": sid, "thread_name": name, "updated_at": stamp}, ensure_ascii=False) + "\n"
    if idx.is_file() and idx.stat().st_size > 0:
        with open(idx, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                line = "\n" + line
    with open(idx, "a", encoding="utf-8") as f:
        f.write(line)
    if root_path in _CODEX_NAMES:
        _CODEX_NAMES[root_path][sid] = name


# ----------------------------------------------------------------------------
# claude enumerator
# ----------------------------------------------------------------------------
# A conversation open on another PC is shown for this many days after the
# last update of its registry entry. Claude writes no heartbeat (the entry
# only changes with the status), so the cap is generous; a crashed session
# on the other PC ages out instead of lingering forever.
REMOTE_RUN_DAYS = 7


def host_name(name: str) -> str:
    return str(name or "").split(".")[0].lower()


def claude_running(root_path: str) -> dict:
    """sessionId -> who runs it, from claude's per-process registry
    (<dir>/sessions/<pid>.json: pid, sessionId, kind, status, updatedAt and
    pidDomain = "<platform>:<host>"). The dir may be synced between PCs, so
    an entry is only probed as a local pid when its host is this machine (or
    absent, older claude): a foreign pid can coincide with a local one.
    Returns {pid, local, host, kind, status, updated} per session id; stale
    local entries and expired remote ones are left out."""
    m = {}
    d = Path(root_path) / "sessions"
    if not d.is_dir():
        return m
    me = host_name(socket.gethostname())
    for f in d.glob("*.json"):
        if not f.stem.isdigit():
            continue
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
            pid, sid = int(o.get("pid") or 0), o.get("sessionId")
            if not sid or not pid:
                continue
            dom = str(o.get("pidDomain") or "")
            dom_host = host_name(dom.split(":", 1)[1]) if ":" in dom else ""
            updated = mtime_utc(f)
            for ms in (o.get("updatedAt"), o.get("statusUpdatedAt")):
                if ms:
                    t = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
                    updated = max(updated, t)
            info = {"pid": pid, "local": True, "host": dom_host, "kind": str(o.get("kind") or ""),
                    "status": str(o.get("status") or ""), "updated": updated, "job": str(o.get("jobId") or "")}
            if not dom_host or dom_host == me:
                if pid_alive(pid):
                    m[sid] = info
            elif datetime.now(timezone.utc) - updated <= timedelta(days=REMOTE_RUN_DAYS):
                info["local"] = False
                m.setdefault(sid, info)   # a local entry wins
        except Exception:
            pass
    return m


def claude_sessions(root: Root = None) -> list:
    root = root or Root("", str(claude_root()), True)
    proj = Path(root.path) / "projects"
    if not proj.is_dir():
        return []
    running = claude_running(root.path)
    cache = {}

    def history():
        if "t" not in cache:
            cache["t"] = history_table(Path(root.path) / "history.jsonl", "sessionId")
        return cache["t"]

    out = []
    for slug in sorted(proj.iterdir()):
        if not slug.is_dir():
            continue
        for f in slug.glob("*.jsonl"):  # depth 1 only: subfolders hold subagent transcripts
            if not UUID_RE.match(f.stem):
                continue
            try:
                s = claude_one(f, running, history, root)
                if s:
                    out.append(s)
            except Exception as e:
                if os.environ.get("CCR_DEBUG"):
                    print(f"ccr: skipping {f}: {e}", file=sys.stderr)
    mark_cleared(out)
    return out


def claude_one(f: Path, running: dict, history, root: Root):
    sid = f.stem
    head = read_window(f, 16 * 1024)
    # Exclude what the real /resume picker excludes (positive markers only).
    m = re.search(r'"isSidechain":(true|false)', head)
    if m and m.group(1) == "true":
        return None
    if '"entrypoint":"daemon"' in head:
        return None
    tail = read_window(f, 128 * 1024, tail=True)

    cwd = None
    m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', head)
    if m:
        cwd = json_unescape(m.group(1))
    if not cwd:
        cwd = (history().get(sid) or {}).get("project")
    if not cwd:
        cwd = str(HOME)

    # Title precedence of the real picker: customTitle, aiTitle (last wins,
    # tail over head), history first prompt, first user message / command.
    title = None
    for prop in ("customTitle", "aiTitle"):
        pat = '"' + prop + r'":"((?:[^"\\]|\\.)*)"'
        mt = re.findall(pat, tail)
        mh = re.findall(pat, head)
        raw = mt[-1] if mt else (mh[-1] if mh else None)
        if raw:
            title = clean_title(json_unescape(raw))
            if title:
                break
    if not title:
        title = clean_title((history().get(sid) or {}).get("display"))

    # First real user line: /clear marker, and the last-resort title.
    started_by_clear = False
    first_seen = False
    for line in head.split("\n"):
        if '"type":"user"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user" or o.get("isMeta"):
            continue
        t = user_text(o)
        if not t:
            continue
        if not first_seen:
            first_seen = True
            started_by_clear = "<command-name>/clear</command-name>" in t
        if not title:
            if t.startswith("<"):
                cm = re.search(r"<command-name>([^<]+)</command-name>", t)
                if cm:
                    title = clean_title(cm.group(1))
            else:
                title = clean_title(t)
        if title:
            break
    if not title:
        title = "(session)"

    # --resume sort rule: min(last message timestamp, file mtime).
    last = mtime_utc(f)
    tm = re.findall(r'"timestamp":"([^"]+)"', tail)
    if tm:
        ts = parse_ts(tm[-1])
        if ts and ts < last:
            last = ts

    bh = re.findall(r'"bridgeSessionId":"([^"]+)"', head)
    bt = re.findall(r'"bridgeSessionId":"([^"]+)"', tail)
    started = None
    fm = re.search(r'"timestamp":"([^"]+)"', head)
    if fm:
        started = parse_ts(fm.group(1))

    ri = running.get(sid) or {}
    return Session(tool="claude", id=sid, title=title, cwd=cwd, last=last,
                   running=bool(ri.get("local")), pid=ri.get("pid") if ri.get("local") else None,
                   running_on="" if ri.get("local", True) else ri.get("host", ""),
                   run_kind=ri.get("kind", ""), run_status=ri.get("status", ""), run_updated=ri.get("updated"),
                   run_job=ri.get("job", ""),
                   source=str(f),
                   started_at=started, started_by_clear=started_by_clear,
                   head_bridge=bh[0] if bh else None,
                   tail_bridge=bt[-1] if bt else (bh[-1] if bh else None),
                   root=root.label, root_path=root.path)


def mark_cleared(sessions: list):
    """Tag conversations replaced by a /clear: exact bridge-id link, or same
    folder + same name only for sessions that predate bridge ids."""
    for n in [s for s in sessions if s.started_by_clear and s.started_at]:
        limit = n.started_at + timedelta(minutes=1)
        if n.head_bridge:
            cands = [s for s in sessions if s.id != n.id and s.tail_bridge == n.head_bridge and s.last <= limit]
        else:
            cands = [s for s in sessions if s.id != n.id and s.title == n.title and s.cwd == n.cwd and s.last <= limit]
        if cands:
            max(cands, key=lambda s: s.last).cleared = True


# ----------------------------------------------------------------------------
# codex enumerator
# ----------------------------------------------------------------------------
def codex_running() -> dict:
    """sessionId -> live codex pid, from 'codex resume <uuid>' command lines."""
    m = {}
    if os.name == "nt" or not shutil.which("ps"):
        return m
    try:
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return m
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or "codex" not in parts[1]:
            continue
        mm = re.search(r"resume\s+(" + UUID_IN + ")", parts[1], re.I)
        if mm:
            m[mm.group(1)] = int(parts[0])
    return m


def codex_prompt(text: str) -> str:
    """The user's own words in a codex user message, '' when codex wrote it:
    the <environment_context> wrapper and other <tag> blocks, the project
    instructions (codex 0.159: "# AGENTS.md instructions for <dir>"). The IDE
    extension wraps the request itself: "# Context from my IDE setup: ... ##
    My request for Codex: <the request>"."""
    if text.startswith("<") or re.match(r"\s*#\s*AGENTS\.md instructions\b", text):
        return ""
    if re.match(r"\s*#\s*Context from my IDE setup\b", text):
        m = re.search(r"##\s*My request for Codex:?\s*([\s\S]*)$", text, re.I)
        return m.group(1).strip() if m else ""
    return text


def ts_key(raw: str) -> str:
    """An ISO time as codex writes it ("2026-09-22T08:28:53.704516Z") -> a key
    that sorts by time whatever the digits of the fraction; '' when unreadable."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", raw or "")
    return f"{m.group(1)}.{(m.group(2) or '').ljust(6, '0')[:6]}" if m else ""


def read_codex_names(paths) -> dict:
    """Names in session_index.jsonl-style files: id -> {name, ts, raw}. In one
    file the last line of an id wins (codex's rule); across files the newest."""
    out = {}
    for p in paths:
        one = {}
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(o, dict) or not o.get("id") or not str(o.get("thread_name") or ""):
                        continue
                    m = re.search(r'"updated_at"\s*:\s*"([^"]*)"', line)
                    raw = m.group(1) if m else ""
                    one[str(o["id"])] = {"name": str(o["thread_name"]), "ts": ts_key(raw), "raw": raw}
        except OSError:
            continue
        for k, v in one.items():
            if k not in out or v["ts"] > out[k]["ts"]:
                out[k] = v
    return out


def codex_shared_names_path(root_path: str):
    """Codex keeps a conversation's name - the /rename one, or the one it gives
    by itself - in its local catalog and session_index.jsonl, never in the
    rollout. With the sessions folder linked into a synced folder (OneDrive), a
    name given on one computer would never reach the other: ccr carries it in
    <synced folder>.names.jsonl, next to the folder, never inside codex's
    tree. None when the sessions folder is not a link."""
    s = Path(root_path) / "sessions"
    try:
        t = os.readlink(s)
    except (OSError, ValueError, NotImplementedError, AttributeError):
        return None
    t = re.sub(r"^\\\\\?\\", "", str(t))
    if not t:
        return None
    if not os.path.isabs(t):
        t = os.path.join(root_path, t)
    return t.rstrip("\\/") + ".names.jsonl"


def codex_title_map(root_path: str) -> dict:
    """Best-effort curated titles: codex catalog (state_N.sqlite: /rename
    name, or a title distinct from the first message) plus the legacy
    session_index.jsonl. Reads a private temp copy of the DB."""
    m = {}
    root = Path(root_path)
    try:
        dbs = sorted((p for p in root.glob("state_*.sqlite") if re.match(r"^state_\d+$", p.stem)),
                     key=lambda p: int(p.stem.split("_")[1]))
        if dbs:
            db = dbs[-1]
            with tempfile.TemporaryDirectory(prefix="ccr-") as td:
                tmp = Path(td) / db.name
                shutil.copy2(db, tmp)
                for ext in ("-wal", "-shm"):
                    side = Path(str(db) + ext)
                    if side.exists():
                        shutil.copy2(side, Path(str(tmp) + ext))
                con = sqlite3.connect(str(tmp))
                try:
                    for sid, t in con.execute(
                        "SELECT id, COALESCE(NULLIF(TRIM(name),''), CASE WHEN TRIM(title) <> '' "
                        "AND title <> first_user_message THEN title END) FROM threads"):
                        if sid and t:
                            m[sid] = t
                finally:
                    con.close()
    except Exception as e:
        if os.environ.get("CCR_DEBUG"):
            print(f"ccr: codex title overlay unavailable: {e}", file=sys.stderr)
    local_idx = root / "session_index.jsonl"
    idx = read_codex_names([local_idx])
    for k, v in idx.items():
        m.setdefault(k, v["name"])

    # Names from the other computer (codex_shared_names_path): the newest name
    # of a conversation wins, wherever it was given. This computer's names go
    # to the shared file; a newer one from there also goes into the local
    # session_index.jsonl, which codex honours too.
    shared = codex_shared_names_path(root_path)
    if shared:
        try:
            sp = Path(shared)
            leaf = sp.stem
            remote = read_codex_names(sorted(str(x) for x in sp.parent.glob(leaf + "*.jsonl") if x.is_file()))
            mine = {k: (idx[k] if k in idx and idx[k]["name"] == v else {"ts": "", "raw": ""}) for k, v in m.items()}
            out = [json.dumps({"id": k, "thread_name": v, "updated_at": mine[k]["raw"]}, ensure_ascii=False,
                              separators=(",", ":"))
                   for k, v in m.items() if k not in remote or mine[k]["ts"] > remote[k]["ts"]]
            inn = []
            for k, r in remote.items():
                if k not in mine or r["ts"] > mine[k]["ts"]:
                    m[k] = r["name"]
                    if r["raw"]:   # into codex's own index only with a time, as codex writes it
                            inn.append(json.dumps({"id": k, "thread_name": r["name"], "updated_at": r["raw"]},
                                              ensure_ascii=False, separators=(",", ":")))
            if out:
                with open(sp, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(out) + "\n")
            if inn:
                with open(local_idx, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(inn) + "\n")
        except OSError as e:
            if os.environ.get("CCR_DEBUG"):
                print(f"ccr: codex names not shared: {e}", file=sys.stderr)
    return m


APP_ORIGINATORS = ("codex desktop", "codex_app", "codex-app")


def codex_origin(originator) -> str:
    """'app' for threads started in the Codex desktop app, else 'cli'.

    session_meta records who opened the thread (`originator`): the desktop app
    writes 'Codex Desktop', the CLI a 'codex_cli_*' token. Unknown/missing
    (older rollouts) is treated as 'cli' - that is how ccr always resumed."""
    o = (originator or "").strip().lower()
    return "app" if any(k in o for k in APP_ORIGINATORS) else "cli"


def codex_sessions(root: Root = None) -> list:
    root = root or Root("", str(codex_root()), True)
    sess_root = Path(root.path) / "sessions"
    if not sess_root.is_dir():
        return []
    running = codex_running()
    curated = codex_title_map(root.path)
    cache = {}

    def history():
        if "t" not in cache:
            cache["t"] = history_table(Path(root.path) / "history.jsonl", "session_id")
        return cache["t"]

    out = []
    for f in sess_root.rglob("rollout-*.jsonl"):
        mm = re.search(r"-(" + UUID_IN + r")\.jsonl$", f.name)
        if not mm:
            continue
        sid, cwd, title, skip, origin = mm.group(1), None, None, False, "cli"
        try:
            # Bounded streaming head read: rollouts reach hundreds of MB.
            with open(f, encoding="utf-8", errors="replace") as fh:
                i = consumed = 0
                for line in fh:
                    i += 1
                    consumed += len(line)
                    if i > 60 or consumed > 512 * 1024:
                        break
                    if i == 1:
                        try:
                            meta = json.loads(line).get("payload") or {}
                            if meta.get("thread_source") and meta["thread_source"] != "user":
                                skip = True
                                break
                            cwd = meta.get("cwd") or cwd
                            sid = meta.get("session_id") or meta.get("id") or sid
                            origin = codex_origin(meta.get("originator"))
                        except Exception:
                            pass
                    elif '"response_item"' in line and '"role":"user"' in line:
                        try:
                            p = json.loads(line).get("payload") or {}
                            if p.get("type") == "message" and p.get("role") == "user":
                                for c in p.get("content") or []:
                                    if isinstance(c, dict) and c.get("text"):
                                        own = codex_prompt(c["text"])
                                        if own:
                                            title = clean_title(own)
                                        break
                                if title:
                                    break
                        except Exception:
                            pass
        except OSError:
            continue
        if skip:
            continue
        if cwd:
            cwd = re.sub(r"^\\\\\?\\UNC\\", r"\\\\", cwd)
            cwd = re.sub(r"^\\\\\?\\", "", cwd)
        else:
            cwd = str(HOME)
        if sid in curated:
            title = clean_title(curated[sid])
        if not title:
            title = clean_title((history().get(sid) or {}).get("text"))
        if not title:
            title = "(session)"
        out.append(Session(tool="codex", id=sid, title=title, cwd=cwd, last=mtime_utc(f),
                           running=sid in running, pid=running.get(sid), source=str(f),
                           origin=origin, root=root.label, root_path=root.path))
    return out


# ----------------------------------------------------------------------------
# fzf picker
# ----------------------------------------------------------------------------
FZF_VER = None


def fzf_version() -> tuple:
    """(major, minor) of the picker, (0, 0) when it will not say."""
    global FZF_VER
    if FZF_VER is None:
        FZF_VER = (0, 0)
        try:
            out = subprocess.run(FZF[:1] + ["--version"], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.match(r"(\d+)\.(\d+)", out.strip())
            if m:
                FZF_VER = (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
    return FZF_VER


# Counts in the picker's own words: "49/128 · 2 marked", marked in green.
INFO_CMD = (r'printf "%s" "$FZF_MATCH_COUNT/$FZF_TOTAL_COUNT"; '
            r'[ "${FZF_SELECT_COUNT:-0}" -gt 0 ] && '
            r'printf " \033[32m·\033[0m \033[1;32m%s marked\033[0m" "$FZF_SELECT_COUNT"; :')


def run_fzf(rows, header, query="", multi=True, expect=None, preview=True, prompt="filter> ", listen=0):
    """Rows are '<id>\\t<display>\\t<preview>'. Returns (key, [ids]) or None on
    Esc/Ctrl-C; key is '' for Enter or one of `expect`. listen: a port for
    fzf --listen (actions sent while it runs, see Limits)."""
    if not shutil.which(FZF[0]):
        sys.exit("ccr: fzf not found - install it first (brew install fzf)")
    ver = fzf_version()
    colors = ["marker:green:bold", "pointer:cyan", "prompt:cyan", "info:dim"]
    args = FZF + ["--ansi", "--no-sort", "--layout=reverse", "--delimiter=\t", "--with-nth=2",
                  "--header=" + header, "--prompt=" + prompt, "--info=inline",
                  "--marker=●", "--pointer=❯"]
    if ver >= (0, 21):
        args.append("--header-first")   # legend above the prompt, not buried under it
    if ver >= (0, 42):
        args.append("--highlight-line")
        colors += ["selected-bg:-1", "selected-fg:green"]
    args.append("--color=" + ",".join(colors))
    if multi:
        args.append("--multi")
        if ver >= (0, 46):
            args += ["--info-command=" + INFO_CMD]
    if query:
        args += ["--query", query]
    if expect:
        args += ["--expect", ",".join(expect)]
    if listen:
        args.append(f"--listen=127.0.0.1:{listen}")
    if preview:
        args += ["--preview", "printf '%b\\n' {3}", "--preview-window=down,5,wrap"]
    p = subprocess.run(args, input="\n".join(rows) + "\n", text=True, stdout=subprocess.PIPE)
    if p.returncode not in (0, 1):
        return None
    lines = p.stdout.splitlines()
    key = ""
    if expect:
        key = lines[0] if lines else ""
        lines = lines[1:]
    return key, [l.split("\t", 1)[0] for l in lines if l.strip()]


class Ctx:
    """Accounts in play for one run: the full lists (Ctrl-A must see every
    account even when --root narrows the listing), the listed ones, and
    whether a tool needs its data dir set per launched process (several
    accounts configured for it, listed or not: a session must always start
    under its own account's dir)."""

    def __init__(self, root_filter: str = ""):
        self.all_claude = get_roots("claude")
        self.all_codex = get_roots("codex")
        self.multi = {"claude": len(self.all_claude) > 1, "codex": len(self.all_codex) > 1}
        self.claude, self.codex = self.all_claude, self.all_codex
        if root_filter:
            known = []
            for r in self.all_claude + self.all_codex:
                if r.label and r.label not in known:
                    known.append(r.label)
            if root_filter not in known:
                sys.exit(f"ccr: no account '{root_filter}' in {CONFIG_PATH} (known: {', '.join(known)})")
            # Copies, not the same objects: the narrowed entry is shown as
            # the default of the listing, the full lists keep the real one.
            self.claude = [Root(r.label, r.path, True) for r in self.all_claude if r.label == root_filter]
            self.codex = [Root(r.label, r.path, True) for r in self.all_codex if r.label == root_filter]
        # Account mode = multi-account is on (several dirs listed for a tool).
        self.multi_root = len(self.claude) > 1 or len(self.codex) > 1
        self.def_label = {"claude": next((r.label for r in self.claude if r.default), ""),
                          "codex": next((r.label for r in self.codex if r.default), "")}
        # Legend entries: one per tool and account - claude first, then
        # codex, in config order - numbered straight through, so a row only
        # ever targets its own tool's numbers.
        self.entries = []
        for t in ("claude", "codex"):
            for r in self.roots(t):
                if r.label:
                    self.entries.append((len(self.entries) + 1, t, r))
        self.quick = {}   # "tool|label" -> email from the dir's own files

    def roots(self, tool: str, all_: bool = False) -> list:
        if tool == "codex":
            return self.all_codex if all_ else self.codex
        return self.all_claude if all_ else self.claude

    def root_var(self, tool: str):
        """The env var to set for a launch, or None when the tool has one dir."""
        return ROOT_VAR[tool] if self.multi[tool] else None


# ----------------------------------------------------------------------------
# account limits: the table above the picker
# ----------------------------------------------------------------------------
# How far each account is into its usage limits, as its own tool reports them
# live: Claude Code's /usage (claude -p "/usage" - a local command: no model
# call, no session, no cost) and Codex's app-server (account/rateLimits/read,
# JSON). Neither needs ccr to read a credential. The picker asks the accounts
# whose answer is older than LIMITS_TTL seconds in background threads and,
# with fzf 0.45 or later, rewrites fzf's header when an answer lands (fzf
# --listen); answers are kept in limits.json next to the state file, so a
# picker opened again soon asks nothing. Ctrl-L asks again. CCR_LIMITS=0 or
# ccr.json "limits": false turns the asking off.
LIMITS_TTL = 300
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_limits_lock = threading.Lock()


def limits_on() -> bool:
    if os.environ.get("CCR_LIMITS") == "0":
        return False
    return (load_config() or {}).get("limits") is not False


def limits_path() -> Path:
    return state_path().parent / "limits.json"


def load_limits() -> dict:
    """"tool|dir" -> the last answer: ok, note, plan, windows (name, pct,
    reset = unix seconds or None), at (when it came), tried / error (a later
    failed ask)."""
    try:
        c = json.loads(limits_path().read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def save_limit(key: str, entry: dict):
    with _limits_lock:
        try:
            c = load_limits()
            c[key] = entry
            limits_path().parent.mkdir(parents=True, exist_ok=True)
            limits_path().write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass


def short_note(text: str) -> str:
    """The first non-empty line, at most 60 characters."""
    lines = [l for l in re.split(r"\r?\n", text or "") if l.strip()]
    t = lines[0].strip() if lines else ""
    return t[:59] + "…" if len(t) > 60 else t


def reset_epoch(text: str, now: int):
    """A reset time as /usage prints it ("Oct 7, 9:59am (Europe/Rome)",
    "12:19am") -> unix seconds. The time is this computer's local time (the
    zone in parentheses is the one Claude Code runs in, i.e. this
    computer's); the year is the one that puts the date in the coming days.
    None when it cannot be read."""
    m = re.match(r"\s*(?:([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b",
                 text or "", re.I)
    if not m:
        return None
    now_l = datetime.fromtimestamp(now)
    h, mi = int(m.group(3)), int(m.group(4) or 0)
    if m.group(5):
        if not 1 <= h <= 12:
            return None
        h = (0 if h == 12 else h) + (12 if m.group(5).lower().startswith("p") else 0)
    if h > 23 or mi > 59:
        return None
    try:
        if m.group(1):
            mon = MONTHS.index(m.group(1).lower()) + 1 if m.group(1).lower() in MONTHS else 0
            if not mon:
                return None
            t = datetime(now_l.year, mon, int(m.group(2)), h, mi)
            if t < now_l - timedelta(days=1):
                try:
                    t = t.replace(year=t.year + 1)
                except ValueError:   # 29 February: .NET's AddYears gives the 28th
                    t = t.replace(year=t.year + 1, day=28)
        else:
            t = datetime(now_l.year, now_l.month, now_l.day, h, mi)
            if t < now_l - timedelta(minutes=1):
                t += timedelta(days=1)
    except ValueError:
        return None
    return int(t.timestamp())


def _round_pct(x) -> int:
    return int(math.floor(float(x) + 0.5))


def parse_claude_limits(text: str, is_error: bool, now: int) -> dict:
    """claude -p "/usage" (its result text) -> limits. The lines read:
      Current session: 8% used · resets Oct 4, 12:20am (Europe/Rome)
      Current week (all models): 50% used · resets Oct 7, 10am (Europe/Rome)
      Current week (Fable): 40% used · resets Oct 7, 10am (Europe/Rome)
    An account without subscription limits (an API key, access turned off by
    its organisation) prints none of them."""
    wins = []
    if not is_error:
        for l in re.split(r"\r?\n", text or ""):
            m = re.match(r"^\s*Current session\s*:\s*(\d+(?:\.\d+)?)\s*%\s*used(?:.*?\bresets\s+(.+?))?\s*$", l, re.I)
            if m:
                name, pct, rs = "5h", m.group(1), m.group(2) or ""
            else:
                m = re.match(r"^\s*Current week\s*\(([^)]*)\)\s*:\s*(\d+(?:\.\d+)?)\s*%\s*used(?:.*?\bresets\s+(.+?))?\s*$",
                             l, re.I)
                if not m:
                    continue
                n = m.group(1).strip()
                name = "week" if re.match(r"^all\s+models$", n, re.I) else re.sub(r"\s+only$", "", n)
                pct, rs = m.group(2), m.group(3) or ""
            wins.append({"name": name, "pct": _round_pct(pct), "reset": reset_epoch(rs, now) if rs else None})
    if wins:
        return {"ok": True, "note": "", "plan": "", "windows": wins}
    note = short_note(text) if is_error else "no limits reported"
    return {"ok": False, "note": note or "no answer", "plan": "", "windows": []}


def parse_codex_limits(line: str, now: int) -> dict:
    """codex app-server's reply to account/rateLimits/read (one JSON-RPC
    line) -> limits. A window is named by its length, not its slot: on a plan
    without the 5-hour window (prolite) the week is "primary"."""
    def none(n):
        return {"ok": False, "note": n, "plan": "", "windows": []}
    try:
        j = json.loads(line or "")
    except ValueError:
        return none("no answer")
    if not isinstance(j, dict):
        return none("no limits reported")
    if j.get("error"):
        err = j["error"]
        return none(short_note(str(err.get("message") or "") if isinstance(err, dict) else "") or "no answer")
    rl = (j.get("result") or {}).get("rateLimits") if isinstance(j.get("result"), dict) else None
    if not isinstance(rl, dict):
        return none("no limits reported")
    found = []
    for w in (rl.get("primary"), rl.get("secondary")):
        if not isinstance(w, dict):
            continue
        mins = _as_int(w.get("windowDurationMins"))
        name = ("5h" if mins == 300 else "week" if mins == 10080 else f"{mins // 1440}d" if mins > 0 and mins % 1440 == 0
                else f"{int(math.floor(mins / 60 + 0.5))}h")
        pct = _round_pct(w.get("usedPercent") or 0)
        reset = int(w["resetsAt"]) if w.get("resetsAt") else None
        found.append({"name": name, "pct": pct, "reset": reset})
    wins = ([w for w in found if w["name"] == "5h"] + [w for w in found if w["name"] == "week"]
            + [w for w in found if w["name"] not in ("5h", "week")])
    if not wins:
        return none("no limits reported")
    return {"ok": True, "note": "", "plan": str(rl.get("planType") or ""), "windows": wins}


def limits_from_fetch(tool: str, res: dict, now: int) -> dict:
    """What one background ask brought back (text / error / fail) -> limits."""
    if not res or res.get("fail"):
        n = short_note(str(res.get("fail") or "")) if res else ""
        return {"ok": False, "note": n or "no answer", "plan": "", "windows": []}
    if tool == "claude":
        return parse_claude_limits(str(res.get("text") or ""), bool(res.get("error")), now)
    return parse_codex_limits(str(res.get("text") or ""), now)


def fmt_reset(epoch: int, now: int) -> str:
    """A reset in the coming hours as 09:59, a later one as Wed 7 09:59."""
    t = datetime.fromtimestamp(epoch)
    return f"{t:%H:%M}" if epoch - now < 20 * 3600 else f"{WEEKDAYS[t.weekday()]} {t.day} {t:%H:%M}"


def fmt_limits(e, now: int, pending: bool = False) -> str:
    """The table's limits cell: "5h 9% → 00:19 · week 50% → Wed 7 09:59 ·
    Fable 40%". Percentages from 70 yellow, from 90 red; a window whose reset
    has passed reads 0%; at 0% the reset is left out (nothing to wait for - and
    codex gives a window nobody has used a reset of its own making); a per-model week shows its reset only when it differs
    from the week's. Old figures say since when; "…" = an ask is under way."""
    more = f" {DIM}…{RESET}" if pending else ""
    if not isinstance(e, dict):
        return f"{DIM}limits…{RESET}" if pending else ""
    if not e.get("ok"):
        return f"{DIM}{e.get('note') or 'no answer'}{RESET}{more}"
    wins = e.get("windows") or []
    week = next((w for w in wins if w.get("name") == "week"), None)
    parts = []
    for w in wins:
        pct, reset = _as_int(w.get("pct")), w.get("reset")
        if reset is not None and int(reset) <= now:
            pct, reset = 0, None
        if pct == 0:
            reset = None
        c = RED if pct >= 90 else YELLOW if pct >= 70 else ""
        s = f"{w.get('name')} {c}{pct}%{RESET if c else ''}"
        same = (w.get("name") not in ("5h", "week") and week and week.get("reset") is not None and reset is not None
                and abs(int(reset) - int(week["reset"])) <= 300)
        if reset is not None and not same:
            s += f" {DIM}→ {fmt_reset(int(reset), now)}{RESET}"
        parts.append(s)
    out = " · ".join(parts)
    at = _as_int(e.get("at"))
    if now - at > LIMITS_TTL + 60:
        out += f" {DIM}(as of {datetime.fromtimestamp(at):%H:%M}){RESET}"
    return out + more


def fetch_limits(tool: str, path: str, exe: str) -> dict:
    """One background ask under one account. Returns {text, error, fail}."""
    env = tool_env(tool, path)
    cwd = tempfile.gettempdir()
    try:
        if tool == "claude":
            # /usage is a local command: no model call. Haiku only in case it
            # ever reached a model.
            p = subprocess.run([exe, "-p", "/usage", "--no-session-persistence", "--output-format", "json",
                                "--model", "haiku"], stdin=subprocess.DEVNULL, capture_output=True, env=env, cwd=cwd,
                               timeout=60)
            try:
                j = json.loads(p.stdout.decode("utf-8", "replace"))
            except ValueError:
                return {"text": "", "error": False, "fail": "no answer"}
            return {"text": str(j.get("result") or ""), "error": bool(j.get("is_error")), "fail": ""}
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, cwd=cwd)
        found = []

        def pump():
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace")
                if re.search(r'"id"\s*:\s*2\b', line):
                    found.append(line.strip())
                    return

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            p.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"ccr","version":"1"}}}\n'
                          b'{"jsonrpc":"2.0","method":"initialized"}\n'
                          b'{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":null}\n')
            p.stdin.flush()
        except OSError:
            pass
        th.join(30)
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
        if found:
            return {"text": found[0], "error": False, "fail": ""}
        return {"text": "", "error": False, "fail": "no answer in 30 s" if p.returncode is None or th.is_alive()
                else "codex app-server ended"}
    except subprocess.TimeoutExpired:
        return {"text": "", "error": False, "fail": "no answer in 60 s"}
    except OSError as e:
        return {"text": "", "error": False, "fail": str(e)}


def fzf_post(port: int, action: str) -> bool:
    """Send an action to a running fzf (--listen). False when it is not there."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}", data=action.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Limits:
    """The picker's account limits: the cache, the asks under way, and the
    fzf to tell when an answer lands (the port of the fzf showing the table,
    and the header to give it)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.on = limits_on()
        self.cache = load_limits() if self.on else {}
        self.pending = set()
        self.port = 0
        self.header = None   # () -> the picker's header, for change-header

    def refresh(self, force: bool = False):
        """Ask the accounts whose answer is old (all of them with force)."""
        if not self.on:
            return
        now = int(time.time())
        for t in ("claude", "codex"):
            for r in self.ctx.roots(t):
                key = f"{t}|{r.path}"
                if key in self.pending:
                    continue
                old = self.cache.get(key)
                if (not force and isinstance(old, dict)
                        and now - max(_as_int(old.get("at")), _as_int(old.get("tried"))) < LIMITS_TTL):
                    continue
                exe = tool_path(t)
                if not exe or not os.path.isdir(r.path):
                    continue
                self.pending.add(key)
                threading.Thread(target=self._ask, args=(t, r.path, exe, key), daemon=True).start()

    def _ask(self, tool: str, path: str, exe: str, key: str):
        res = fetch_limits(tool, path, exe)
        now = int(time.time())
        e = limits_from_fetch(tool, res, now)
        old = self.cache.get(key)
        if e["ok"] or not (isinstance(old, dict) and old.get("ok")):
            e["at"] = now
            self.cache[key] = e
        else:   # keep the last good figures
            old["tried"], old["error"] = now, e["note"]
        save_limit(key, self.cache[key])
        self.pending.discard(key)
        port = self.port
        if port and self.header:
            # fzf may still be starting: a few tries
            for _ in range(10):
                if self.port != port or fzf_post(port, "change-header:" + self.header()):
                    break
                time.sleep(0.3)


def account_table(ctx, lim: Limits = None) -> list:
    """The table above the list: one line per tool and account - the tool
    (once per group), in multi-account mode the number Ctrl-O offers and the
    label, the dir, who is logged in there and how far it is into its usage
    limits. Dirs go first, then the emails, when the lines would not fit."""
    width = shutil.get_terminal_size((100, 24)).columns - 4
    now = int(time.time())
    rows = []
    for t in ("claude", "codex"):
        for r in ctx.roots(t):
            qk = f"{t}|{r.label}"
            if qk not in ctx.quick:
                ctx.quick[qk] = quick_identity(t, r.path)
            n = next((str(k) for k, tt, rr in ctx.entries if tt == t and rr.label == r.label), "") \
                if ctx.multi_root and r.label else ""
            lbl = acct_label(r.label, r.default) if ctx.multi_root and r.label else ""
            key = f"{t}|{r.path}"
            cell = fmt_limits(lim.cache.get(key), now, key in lim.pending) if lim and lim.on else ""
            rows.append((t, n, lbl, fmt_cwd(r.path, 28), ctx.quick[qk] or who_at(t, r.path), cell))
    plain = lambda s: len(re.sub(r"\x1b\[[0-9;]*m", "", s))
    n_w = max((len(x[1]) for x in rows), default=0)
    lbl_w = max((len(x[2]) for x in rows), default=0)
    dir_w = max((len(x[3]) for x in rows), default=0)
    who_w = max((len(x[4]) for x in rows), default=0)
    lim_w = max((plain(x[5]) for x in rows), default=0)
    base = 2 + 6 + (2 + n_w + 1 + lbl_w if ctx.multi_root else 0)
    lim_part = 2 + lim_w if lim_w else 0
    with_dirs = base + 2 + dir_w + 2 + who_w + lim_part <= width
    with_who = with_dirs or base + 2 + who_w + lim_part <= width
    lines = [f"{BOLD}{MAGENTA}Multi-account mode active.{RESET}"] if ctx.multi_root else []
    prev = ""
    for t, n, lbl, d, who, cell in rows:
        tool_txt = f"{t:<6}" if t != prev else " " * 6
        prev = t
        line = f"  {TOOL_COLOR[t]}{tool_txt}{RESET}"
        if ctx.multi_root:
            line += f"  {TOOL_COLOR[t]}{BOLD}{n:>{n_w}}{RESET} {MAGENTA}{lbl:<{lbl_w}}{RESET}"
        if with_dirs:
            line += f"  {d:<{dir_w}}"
        if with_who:
            line += f"  {DIM}{who:<{who_w}}{RESET}"
        if cell:
            line += f"  {cell}"
        lines.append(line)
    return lines


def session_rows(sessions, index, ctx: Ctx, usage: dict = None):
    """usage: "tool|id" -> Usage or None, when the Ctrl-K column is on."""
    rows = []
    root_w = min(14, max((len(acct_label(r.label, r.default)) for r in ctx.claude + ctx.codex), default=0)) \
        if ctx.multi_root else 0
    usage_sum = sum(u.total for u in usage.values() if u) if usage else 0
    for i, s in enumerate(sessions):
        index[str(i)] = s
        color = TOOL_COLOR[s.tool]
        # Age column: red "run" = running here ("bg" = a claude background
        # session), yellow "@host" = open on another PC.
        if s.running:
            age = f"{RED}{'bg' if s.run_kind == 'bg' else 'run':>6}{RESET}"
        elif s.running_on:
            age = f"{YELLOW}{('@' + s.running_on)[:6]:>6}{RESET}"
        else:
            age = f"{fmt_age(s.last):>6}"
        title = s.title if len(s.title) <= 50 else s.title[:49] + "…"
        tag = f" {YELLOW}(cleared){RESET}" if s.cleared else ""
        # Tool column, padded on the plain text (the colors are zero-width).
        app = s.origin == "app"
        tool = f"{color}{s.tool}{RESET}" + (f"{DIM} app{RESET}" if app else "")
        tool += " " * max(1, 10 - len(s.tool) - (4 if app else 0))
        # Account column (multi-account only): the config dir this session
        # lives in; the label is part of the text fzf matches on.
        acct = ""
        if ctx.multi_root:
            lbl = acct_label(s.root, s.root == ctx.def_label[s.tool])[:root_w]
            acct = f"{MAGENTA}{lbl:<{root_w}}{RESET} "
        # Usage column (Ctrl-K): total tokens in the window and, in
        # parentheses, the session's share of the window's tokens across
        # the listed sessions; blank when the session had no turn in it.
        use = ""
        if usage is not None:
            u = usage.get(s.key)
            use = f" {YELLOW}{fmt_tokens(u.total):>6} {fmt_share(u.total, usage_sum):>6}{RESET}" if u else " " * 14
        disp = f"{tool}{acct}{age}{use}  {title}{tag}  {DIM}{fmt_cwd(s.cwd, 50)}{RESET}"
        extra = ((" cleared" if s.cleared else "") + (" run" if s.running else "")
                 + (" bg" if s.running and s.run_kind == "bg" else "")
                 + (f" elsewhere @{s.running_on}" if s.running_on else "")
                 + (" app" if app else ""))  # filter words
        how = (f"opens in: Codex app (codex://threads/{s.id})" if app
               else f"opens in: terminal ({resume_argv(s)[0]} …)")
        where = f"\\naccount: {s.root} ({fmt_cwd(s.root_path, 40)})" if ctx.multi_root else ""
        if s.running_on:
            where += (f"\\nopen on: {s.running_on} ({s.run_status or 'open'}"
                      + (f", updated {fmt_age(s.run_updated)} ago" if s.run_updated else "") + ")")
        prev = (f"{s.tool} · {s.title}\\nfolder: {s.cwd}{where}\\n{how}\\nlast: "
                f"{s.last.astimezone():%Y-%m-%d %H:%M}   id: {s.id}").replace("\t", " ")
        rows.append(f"{i}\t{disp}{DIM}{extra}{RESET}\t{prev}")
    return rows


def picker_hint(ctx: Ctx, upd_note: str = "", lim: Limits = None) -> str:
    lines = []
    if ">" in upd_note:
        f, t = upd_note.split(">", 1)
        lines.append(f"{BOLD}{GREEN}ccr updated v{f} -> v{t}{RESET}")
    lines += account_table(ctx, lim)
    keys = [("↑↓", "move"), ("Tab", "mark"), ("Enter", "open"), ("Ctrl-E", "model/effort")]
    if ctx.multi_root:
        keys.append(("Ctrl-O", "open under another account"))
    keys += [("Ctrl-N", "new"), ("Ctrl-P", "MAP"), ("Ctrl-A", "accounts"), ("Ctrl-K", "usage"), ("Ctrl-J", "details")]
    if lim and lim.on:
        keys.append(("Ctrl-L", "limits"))
    keys += [("Ctrl-X", "close"), ("Ctrl-T", "install"), ("Del", "delete"), ("Esc", "cancel")]
    lines += hint_wrapped(keys)
    words = f"{DIM},{RESET} ".join(f"{CYAN}{w}{RESET}" for w in ("run", "cleared", "app"))
    filters = f"{DIM}type to filter — {RESET}{words}{DIM} match as words{RESET}"
    lines.append(right_align(filters, f"{DIM}ccr v{VERSION}{RESET}"))
    return "\n".join(lines)


def choose_tool(title: str, claude_note: str, codex_note: str, extra: list = None):
    """Tool chooser: claude or codex (plus optional extra rows). Returns the
    chosen row id ('claude', 'codex' or an extra id), or None on Esc."""
    rows = [f"claude\t{ORANGE}claude{RESET}{' ' * 5}{DIM}{claude_note}{RESET}\tclaude",
            f"codex\t{CYAN}codex{RESET}{' ' * 6}{DIM}{codex_note}{RESET}\tcodex"]
    rows += extra or []
    res = run_fzf(rows, hint(("Enter", "choose"), ("Esc", "back"), tail=title),
                  multi=False, preview=False, prompt="tool> ")
    if not res or not res[1]:
        return None
    return res[1][0]


def choose_account(items: list, title: str):
    """Account chooser over (label, text, is_default) rows. Returns the
    label, or None on Esc."""
    rows = [f"{lbl}\t{MAGENTA}{acct_label(lbl, d):<14}{RESET} {DIM}{text}{RESET}\t{text}" for lbl, text, d in items]
    res = run_fzf(rows, hint(("Enter", "choose"), ("Esc", "back"), tail=title),
                  multi=False, preview=False, prompt="account> ")
    if not res or not res[1]:
        return None
    return res[1][0]


# ----------------------------------------------------------------------------
# account page (Ctrl-A in the picker)
# ----------------------------------------------------------------------------
def read_new_account(ctx: Ctx, dry: bool):
    """tool, then label, then options -> action dict, or None on Esc."""
    tool = choose_tool("tool for the new account", "fresh dir + claude auth login", "fresh dir + codex login")
    if tool not in ("claude", "codex"):
        return None
    taken = [r.label for r in ctx.roots(tool, all_=True) if r.label]
    while True:
        label = ask(f"label for the new {tool} account (e.g. work; {LABEL_HINT}; empty = back)> ")
        if label is None or not label.strip():
            return None
        label = label.strip()
        if not LABEL_RE.match(label):
            print(f"{YELLOW}ccr: invalid label '{label}' ({LABEL_HINT}){RESET}")
            continue
        if label == DEFAULT_LABEL or label in taken:
            print(f"{YELLOW}ccr: {tool} account '{label}' already exists{RESET}")
            continue
        # Options for the new dir; only offered when the default account
        # has something to copy (claude: status line, codex: config.toml).
        copy = False
        def_root = next((r for r in ctx.roots(tool, all_=True) if r.default), None)
        def_path = def_root.path if def_root else tool_default_root(tool)
        if has_settings(tool, def_path):
            info = settings_info(tool)
            ans = ask(f"  {info['text']}? {DIM}({info['note']}){RESET}  {GREEN}[Y]{RESET} yes  n: no  Esc: back > ")
            if ans is None:
                return None
            copy = ans.strip().lower() in ("", "y", "yes")
        return {"action": "add", "tool": tool, "label": label, "copy": copy}


def account_page(ctx: Ctx, dry: bool):
    """The first time it explains what turning multi-account mode on does
    and goes straight to adding the first extra account (tool, then label).
    Afterwards it lists the accounts - with who is logged in where - and
    offers: Enter on '+' = add an account, Del = remove the highlighted one
    (its sessions go to the default account), Ctrl-S = copy the default
    account's settings to it, Enter on 'X' = turn multi-account mode off.
    Returns an action dict or None."""
    rows = []
    labels = []
    for r in ctx.all_claude + ctx.all_codex:
        if r.label and r.label not in labels:
            labels.append(r.label)
    entries = [(t, r) for lbl in labels for t in ("claude", "codex")
               for r in ctx.roots(t, all_=True) if r.label == lbl]

    if not entries:
        # --- activation page ---
        print(f"\n{BOLD}Multi-account mode{RESET}\n")
        print("  You are about to turn multi-account mode on. Nothing is moved or logged out:")
        print(f"  the dirs claude and codex use today become the '{DEFAULT_LABEL}' account, and every")
        print("  session you see now belongs to it.")
        print(f"    claude   {fmt_cwd(str(claude_root()), 60)}")
        print(f"    codex    {fmt_cwd(str(codex_root()), 60)}\n")
        print("  Next you choose a tool and a label for the additional account. ccr creates a")
        print("  fresh dir for it next to the one the tool uses today (.claude-<label> or")
        print("  .codex-<label>) and runs that tool's own login there, so each account keeps")
        print("  its own credentials and settings.")
        print("  Afterwards Ctrl-A lists the accounts, adds more, or turns the mode off again.\n")
        ans = ask(f"  {GREEN}[Enter]{RESET} continue    {DIM}anything else: back, nothing changes{RESET} > ")
        if ans is None or ans.strip():
            return None
        return read_new_account(ctx, dry)

    # --- list page ---
    who = {}
    for t, r in entries:
        who[f"{t}|{r.label}"] = who_at(t, r.path)
    lbl_w = max(7, max(len(acct_label(r.label, r.default)) for _, r in entries))
    dir_w = min(40, max(len(fmt_cwd(r.path, 40)) for _, r in entries))
    for i, (t, r) in enumerate(entries):
        disp = (f"{MAGENTA}{acct_label(r.label, r.default):<{lbl_w}}{RESET}  {TOOL_COLOR[t]}{t:<6}{RESET}  "
                f"{fmt_cwd(r.path, 40):<{dir_w}}  {DIM}{who[f'{t}|{r.label}']}{RESET}")
        rows.append(f"{i}\t{disp}\t{r.path}")
    rows.append(f"add\t{GREEN}+ add an account{RESET}  {DIM}tool, label, then that tool's login{RESET}\tadd")
    rows.append(f"off\t{RED}X turn multi-account mode off{RESET}  {DIM}every session goes to the '{DEFAULT_LABEL}' account{RESET}\toff")
    header = (f"{BOLD}Accounts{RESET}  {DIM}{CONFIG_PATH}{RESET}\n"
              + hint(("Enter", "choose"), ("Ctrl-L", "log in here"), ("Del", "remove"), ("Ctrl-S", "copy settings from default"),
                     ("Esc", "back"))
              + f"\n{DIM}A session always resumes under the account whose dir it lives in. "
              f"In the picker, Ctrl-O opens the marked rows under another account.{RESET}")
    res = run_fzf(rows, header, multi=False, preview=False, prompt="accounts> ", expect=["del", "ctrl-s", "ctrl-l"])
    if not res or not res[1]:
        return None
    key, picked = res[0], res[1][0]
    if key == "":
        if picked == "add":
            return read_new_account(ctx, dry)
        if picked == "off":
            print(f"\n{BOLD}Turn multi-account mode off{RESET}\n")
            print(f"  Every session of every other account moves to the '{DEFAULT_LABEL}' account (the dirs")
            print("  claude and codex use today) and keeps working there. The other dirs and their")
            print("  logins stay on disk; ccr forgets them and goes back to a single account.")
            print("  Refused while a session of another account is running.\n")
            ans = ask(f"  {RED}[y]{RESET} turn off    {DIM}anything else: cancel{RESET} > ")
            return {"action": "disable"} if ans and ans.strip().lower() == "y" else None
        return None
    if not picked.isdigit():
        return None
    t, r = entries[int(picked)]
    if key == "del":
        if r.default:
            print(f"{YELLOW}ccr: '{r.label}' is the default {t} account - turn multi-account mode off (X) instead{RESET}")
            pause()
            return None
        print(f"\n{BOLD}Remove account{RESET}\n")
        print(f"    {t} · {BOLD}{r.label}{RESET}  {fmt_cwd(r.path, 60)}\n")
        print(f"  Every {t} session of this account moves to the '{DEFAULT_LABEL}' account and")
        print("  keeps working there. The dir and its login stay on disk; ccr just forgets it.")
        print("  Refused while one of its sessions is running.\n")
        ans = ask(f"  {RED}[y]{RESET} remove    {DIM}anything else: cancel{RESET} > ")
        return {"action": "remove", "tool": t, "label": r.label} if ans and ans.strip().lower() == "y" else None
    if key == "ctrl-l":
        # Log in on this PC: an account added elsewhere (synced ccr.json)
        # has no dir or no login here yet.
        state = ("does not exist on this PC yet" if not os.path.isdir(r.path)
                 else "is already logged in here" if quick_identity(t, r.path) else "exists here but holds no login")
        cmd = "claude auth login" if t == "claude" else "codex login"
        print(f"\n{BOLD}Log in on this PC{RESET}\n")
        print(f"    {t} · {BOLD}{r.label}{RESET}  {fmt_cwd(r.path, 60)}\n")
        print(f"  The dir {state}. ccr creates it if needed and runs '{cmd}' inside it,")
        print("  so this PC gets its own credentials for the account (nothing else changes).\n")
        ans = ask(f"  {GREEN}[y]{RESET} log in    {DIM}anything else: cancel{RESET} > ")
        return {"action": "login", "tool": t, "label": r.label} if ans and ans.strip().lower() == "y" else None
    if key == "ctrl-s":
        if r.default:
            print(f"{YELLOW}ccr: '{r.label}' is the default account - it is the source, not a target{RESET}")
            pause()
            return None
        src = next((x for tt, x in entries if tt == t and x.default), None)
        info = settings_info(t)
        if not src or not has_settings(t, src.path):
            print(f"{YELLOW}ccr: the default {t} account has no {info['what']} to copy{RESET}")
            pause()
            return None
        print(f"\n{BOLD}Copy {info['what']}{RESET}\n")
        print(f"    from  {src.label}  {fmt_cwd(src.path, 60)}")
        print(f"    to    {r.label}  {fmt_cwd(r.path, 60)}\n")
        if t == "claude":
            print("  The statusLine entry is merged into the target's settings.json (other keys stay)")
            print("  and the statusline* script files next to it are copied over.\n")
        else:
            print("  config.toml (model, effort, features, per-project trust) replaces the target's one.\n")
        ans = ask(f"  {GREEN}[y]{RESET} copy    {DIM}anything else: cancel{RESET} > ")
        if ans and ans.strip().lower() == "y":
            return {"action": "settings", "tool": t, "label": r.label, "from": src.path, "to": r.path}
    return None


def run_account_action(act: dict, dry: bool):
    """add / remove / settings / disable, on the main screen."""
    what = f"{act.get('tool')} account '{act.get('label')}'" if act.get("label") else "multi-account mode"
    if dry:
        print(f"{YELLOW}dry-run: would {act['action']} {what}"
              f"{' (copying the default account settings)' if act.get('copy') else ''}.{RESET}")
        return
    try:
        if act["action"] == "add":
            add_account(act["label"], act["tool"], act["copy"])
        elif act["action"] == "settings":
            copy_settings(act["tool"], act["from"], act["to"])
        elif act["action"] == "login":
            add_account(act["label"], act["tool"], False)
        elif act["action"] == "remove":
            remove_account(act["label"], act["tool"])
        elif act["action"] == "disable":
            disable_multi_account()
    except Exception as e:
        print(f"ccr: {act['action']} failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# token usage (Ctrl-K column, Ctrl-J details)
# ----------------------------------------------------------------------------
# Per-session token consumption inside a time window, read from the files
# the tools write anyway: claude appends a usage block to every assistant
# turn (deduplicated by message id - content blocks repeat it), codex a
# token_count event per turn with last_token_usage (that turn's delta) and
# the rate-limit meter it saw. Full streaming reads, so only on demand.
class Usage:
    __slots__ = ("turns", "input", "cache_write", "cache_read", "output", "thinking", "total",
                 "models", "buckets", "limit_5h", "limit_7d", "reset_5h", "reset_7d")

    def __init__(self):
        self.turns = self.input = self.cache_write = self.cache_read = self.output = self.thinking = self.total = 0
        self.models, self.buckets = {}, {}
        self.limit_5h = self.limit_7d = self.reset_5h = self.reset_7d = None

    def add(self, ts: datetime, model: str, inp: int, cw: int, cr: int, out: int, think: int):
        self.turns += 1
        self.input += inp
        self.cache_write += cw
        self.cache_read += cr
        self.output += out
        self.thinking += think
        total = inp + cw + cr + out
        self.total += total
        model = model or "?"
        self.models[model] = self.models.get(model, 0) + total
        b = int(ts.timestamp()) // 1800 * 1800   # 30-minute buckets
        self.buckets[b] = self.buckets.get(b, 0) + total


def _num(text: str, key: str) -> int:
    m = re.search('"' + key + r'":(\d+)', text)   # first occurrence = the top-level value
    return int(m.group(1)) if m else 0


def read_claude_usage(path: Path, since: datetime, u: Usage):
    turns = {}   # message id -> last seen values (final usage of the message)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"assistant"' not in line or '"usage":{' not in line:
                continue
            m = re.search(r'"timestamp":"([^"]+)"', line)
            ts = parse_ts(m.group(1)) if m else None
            if not ts or ts < since:
                continue
            m = re.search(r'"id":"(msg_[^"]+)"', line)
            mid = m.group(1) if m else f"line{len(turns)}"
            m = re.search(r'"model":"([^"]+)"', line)
            rest = line[line.index('"usage":{'):]
            turns[mid] = (ts, m.group(1) if m else "", _num(rest, "input_tokens"), _num(rest, "cache_creation_input_tokens"),
                          _num(rest, "cache_read_input_tokens"), _num(rest, "output_tokens"), _num(rest, "thinking_tokens"))
    for t in turns.values():
        u.add(*t)


def read_codex_usage(path: Path, since: datetime, u: Usage):
    model = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"turn_context"' in line:
                m = re.search(r'"model":"([^"]+)"', line)
                if m:
                    model = m.group(1)
                continue
            if '"token_count"' not in line:
                continue
            m = re.search(r'"timestamp":"([^"]+)"', line)
            ts = parse_ts(m.group(1)) if m else None
            if not ts or ts < since:
                continue
            i = line.find('"last_token_usage":{')
            if i < 0:
                continue
            rest = line[i:]
            # codex counts cached input inside input_tokens; split it out.
            cached = _num(rest, "cached_input_tokens")
            u.add(ts, model, _num(rest, "input_tokens") - cached, _num(rest, "cache_write_input_tokens"), cached,
                  _num(rest, "output_tokens"), _num(rest, "reasoning_output_tokens"))
            # The meter: each window by its length, not its slot - on a plan
            # without the 5-hour window (prolite) the week is "primary".
            for slot in ("primary", "secondary"):
                m = re.search(r'"' + slot + r'":\{"used_percent":([0-9.]+),"window_minutes":(\d+),"resets_at":(\d+)', line)
                if not m:
                    continue
                if int(m.group(2)) <= 360:
                    u.limit_5h, u.reset_5h = float(m.group(1)), int(m.group(3))
                else:
                    u.limit_7d, u.reset_7d = float(m.group(1)), int(m.group(3))


def session_usage(s: Session, since: datetime):
    """Usage of one session since `since`, or None when it had no turn in the
    window. Only files written inside the window are read (claude: the
    transcript and the subagent transcripts in its sidecar dir)."""
    files = []
    if s.source and Path(s.source).exists():
        files.append(Path(s.source))
    if s.tool == "claude" and s.source:
        side = Path(s.source).parent / s.id
        if side.is_dir():
            files += [p for p in side.rglob("*.jsonl") if p.is_file()]
    files = [p for p in files if mtime_utc(p) >= since]
    if not files:
        return None
    u = Usage()
    for p in files:
        try:
            (read_claude_usage if s.tool == "claude" else read_codex_usage)(p, since, u)
        except Exception as e:
            if os.environ.get("CCR_DEBUG"):
                print(f"ccr: usage of {p} unreadable: {e}", file=sys.stderr)
    return u if u.turns else None


def fmt_tokens(n: int) -> str:
    """1234 -> "1.2k", 845321 -> "845k", 1234567 -> "1.2M"."""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    if n >= 1_000:
        return f"{n / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def fmt_share(n: int, total: int) -> str:
    """The share of the window's tokens: "(52%)", "(<1%)" for a rounded 0."""
    if not total or not n:
        return "(0%)"
    pct = round(100 * n / total)
    return "(<1%)" if pct == 0 else f"({pct}%)"


def usage_page(s: Session, u, hours: int, since: datetime, all_: list = None):
    """Details page (Ctrl-J): the split of the tokens, the models, a 30-minute
    timeline of the window, and - codex only - the rate-limit meter the
    session saw at its last turn. Claude Code does not record its meter.
    all_: every listed session with turns in the window, as (Session, Usage),
    for the split of the window session by session."""
    print(f"\n{BOLD}Token usage{RESET}  {s.tool} · {s.title}")
    print(f"{DIM}last {hours} h (since {since.astimezone():%Y-%m-%d %H:%M}){RESET}")
    print(f"  folder:  {fmt_cwd(s.cwd, 70)}" + (f"   account: {s.root}" if s.root else ""))
    if not u:
        print(f"\n  {DIM}no turn in this window{RESET}")
        return
    models = ", ".join(f"{m} ({fmt_tokens(t)})" for m, t in sorted(u.models.items(), key=lambda kv: -kv[1]))
    print(f"  turns:   {u.turns} · models: {models}\n")
    print(f"  fresh input   {u.input:>12,}")
    print(f"  cache write   {u.cache_write:>12,}")
    print(f"  cache read    {u.cache_read:>12,}")
    print(f"  output        {u.output:>12,}  {DIM}(thinking {u.thinking:,}){RESET}")
    print(f"  {BOLD}total         {u.total:>12,}{RESET}\n")
    print(f"  {DIM}timeline, 30-minute buckets (local time):{RESET}")
    mx = max(u.buckets.values())
    for b in sorted(u.buckets):
        t = datetime.fromtimestamp(b, tz=timezone.utc).astimezone()
        bar = "█" * max(1, round(30 * u.buckets[b] / mx))
        print(f"  {t:%H:%M}  {YELLOW}{bar}{RESET} {fmt_tokens(u.buckets[b])}")
    print()
    if s.tool == "codex":
        if u.limit_5h is not None or u.limit_7d is not None:
            parts = []
            if u.limit_5h is not None:
                parts.append(f"5 h {u.limit_5h:.0f}%" + (f" (resets {datetime.fromtimestamp(u.reset_5h):%H:%M})" if u.reset_5h else ""))
            if u.limit_7d is not None:
                parts.append(f"7 d {u.limit_7d:.0f}%" + (f" (resets {fmt_reset(u.reset_7d, 0)})" if u.reset_7d else ""))
            print(f"  rate limit seen at the last turn: {' · '.join(parts)}")
        else:
            print(f"  {DIM}rate limit: not recorded in this rollout{RESET}")
    else:
        print(f"  {DIM}rate limit: Claude Code does not record its meter in the transcript{RESET}")
    usage_split(s, all_ or [])


def usage_split(s: Session, all_: list):
    """The window split session by session: every listed session with turns
    in it, largest first, with its share; the highlighted one is marked.
    Which session ate the quota is the question this answers."""
    rows = sorted([(x, ux) for x, ux in all_ if ux], key=lambda e: -e[1].total)
    if not rows:
        return
    total = sum(ux.total for _, ux in rows)
    print(f"\n  {BOLD}the window, session by session{RESET}  {DIM}{len(rows)} session{'s' if len(rows) != 1 else ''}"
          f" · {fmt_tokens(total)} tokens{RESET}")
    for x, ux in rows:
        me = x.tool == s.tool and x.id == s.id
        tc = ORANGE if x.tool == "claude" else CYAN
        acct = f" {MAGENTA}{x.root}{RESET}" if x.root else ""
        print(f"  {GREEN + '>' + RESET if me else ' '} {fmt_share(ux.total, total)[1:-1]:>4}  {YELLOW}{fmt_tokens(ux.total):>6}{RESET}  "
              f"{tc}{x.tool:<6}{RESET}{acct}  {x.title}")


# ----------------------------------------------------------------------------
# close a running conversation (Ctrl-X), open-elsewhere confirmation
# ----------------------------------------------------------------------------
def _gone(pid: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def stop_session(s: Session) -> bool:
    """Close a conversation that runs on this machine; the transcript is
    never touched. A claude background session goes through claude's own
    `claude stop <id>` (its account dir selected); anything else - and a stop
    that did not work - by ending the process, politely first, then by force
    after 3 s. True when the process is gone."""
    pid = s.pid
    if s.tool == "claude" and s.run_kind == "bg":
        try:
            # `claude stop` takes the short job id (what `claude --bg` prints and
            # the registry keeps as jobId), not the session id: the first eight
            # characters of it when the entry predates jobId.
            env = tool_env("claude", s.root_path) if s.root_path else None
            r = subprocess.run(["claude", "stop", s.run_job or str(s.id)[:8]], capture_output=True, env=env, timeout=30)
            if r.returncode == 0 and (not pid or _gone(pid, 3)):
                return True
        except Exception:
            pass
    if not pid:
        return False
    try:
        if os.name == "nt":
            if subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True).returncode == 0 and _gone(pid, 3):
                return True
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            if _gone(pid, 3):
                return True
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    return _gone(pid, 3)


def confirm_close(s: Session) -> bool:
    kind = "background session" if s.run_kind == "bg" else (f"{s.run_kind} session" if s.run_kind else "session")
    how = ("'claude stop' first, then the process" if s.tool == "claude" and s.run_kind == "bg"
           else "ends the process (politely first, by force after 3 s)")
    print(f"\n{YELLOW}Close this running conversation?{RESET}\n")
    print(f"    {s.tool} · {BOLD}{s.title}{RESET}")
    print(f"    folder:  {s.cwd}")
    print(f"    process: pid {s.pid} · {kind}" + (f" · status {s.run_status}" if s.run_status else ""))
    if s.run_status == "busy":
        print(f"    {RED}it is working right now - closing interrupts that turn{RESET}")
    print(f"\n  {DIM}{how}. The conversation is kept: ccr lists it by age again and it can be resumed.{RESET}")
    ans = ask(f"  {YELLOW}[y]{RESET} close    {DIM}anything else: cancel{RESET} > ")
    return bool(ans) and ans.strip().lower() == "y"


def confirm_elsewhere(sessions: list) -> bool:
    """Before opening conversations that are open on another PC."""
    print(f"\n{YELLOW}Open on another PC{RESET}\n")
    for s in sessions:
        ago = f", last update {fmt_age(s.run_updated)} ago" if s.run_updated else ""
        print(f"    {s.tool} · {BOLD}{s.title}{RESET}  {YELLOW}@{s.running_on}{RESET} {DIM}({s.run_status or 'open'}{ago}){RESET}")
    print("\n  Opening it here too makes two processes append to the same transcript.")
    print("  Close it on the other PC first, unless that entry is a leftover of a crash.\n")
    ans = ask(f"  {YELLOW}[y]{RESET} open anyway    {DIM}anything else: back to the list{RESET} > ")
    return bool(ans) and ans.strip().lower() == "y"


# ----------------------------------------------------------------------------
# delete
# ----------------------------------------------------------------------------
def remove_session_data(s: Session) -> bool:
    if s.tool == "codex":
        # Codex keeps a catalog besides the rollout file, so let its own CLI
        # do the delete, with the session's own account dir as CODEX_HOME
        # (or codex looks in the default dir and misses it, leaving the
        # account's catalog stale); fall back to removing the rollout.
        # Since codex 0.159 `codex delete <id>` asks for a confirmation and,
        # with its output captured, refuses ("cannot confirm session deletion
        # without an interactive terminal"): --force skips it, and ccr has
        # asked already. A codex without --force gets the plain command, with
        # nothing on stdin, so it can never wait for an answer.
        try:
            env = tool_env("codex", s.root_path) if s.root_path else None
            exe = tool_path("codex") or "codex"
            for argv in ([exe, "delete", "--force", s.id], [exe, "delete", s.id]):
                if subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, env=env).returncode == 0:
                    return True
        except Exception:
            pass
        if s.source and Path(s.source).exists():
            Path(s.source).unlink()
            return True
        return False
    p = Path(s.source)
    if not p.exists():
        return False
    p.unlink()
    side = p.parent / s.id
    if side.is_dir():
        shutil.rmtree(side, ignore_errors=True)
    return True


def confirm_delete(s: Session) -> bool:
    size, preview = "", None
    try:
        p = Path(s.source)
        n = p.stat().st_size
        size = f"{n/1048576:.1f} MB" if n >= 1048576 else (f"{n//1024} KB" if n >= 1024 else f"{n} B")
        tail = read_window(p, 64 * 1024, tail=True)
        pat = r'"lastPrompt":"((?:[^"\\]|\\.)*)"' if s.tool == "claude" else r'"last_agent_message":"((?:[^"\\]|\\.)*)"'
        mm = re.findall(pat, tail)
        if mm:
            preview = clean_title(json_unescape(mm[-1]))
    except Exception:
        pass
    print(f"\n{RED}Delete this conversation permanently?{RESET}\n")
    print(f"    {s.tool} · \033[1m{s.title}\033[22m")
    print(f"    folder:    {s.cwd}")
    if s.root:
        print(f"    account:   {s.root}  {DIM}{fmt_cwd(s.root_path, 50)}{RESET}")
    print(f"    last used: {s.last.astimezone():%Y-%m-%d %H:%M}  ({fmt_age(s.last)})")
    if size:
        print(f"    size:      {size}")
    if preview:
        print(f"    {'last prompt' if s.tool == 'claude' else 'last reply '}: {DIM}{preview}{RESET}")
    print(f"\n  {DIM}removed from disk, no undo (codex: via 'codex delete'){RESET}")
    if s.tool == "codex" and s.origin == "app" and not shutil.which("codex"):
        print(f"  {YELLOW}the Codex app keeps its own copy: without the 'codex' CLI this only drops "
              f"the transcript, the thread stays in the app{RESET}")
    ans = ask(f"  {RED}[y]{RESET} delete    {DIM}anything else: cancel{RESET} > ")
    return bool(ans) and ans.strip().lower() == "y"


# ----------------------------------------------------------------------------
# launch backends
# ----------------------------------------------------------------------------
def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_osascript(script: str):
    """(ok, message). osascript reports refusals on stderr with a non-zero exit."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except OSError as e:
        return False, str(e)
    err = (r.stderr or "").strip().splitlines()
    msg = err[-1] if err else ""
    msg = msg.split("execution error:", 1)[-1].strip() or msg
    if "-1743" in msg:
        # The tab keystroke needs Automation rights the user just declined.
        msg += "  (System Settings › Privacy & Security › Automation › Terminal › System Events)"
    return r.returncode == 0, msg[:200]


def open_tab(cwd: str, cmd: str, new_window: bool, dry: bool, tabs: bool = False, detached: bool = False) -> bool:
    """Run cmd in a new terminal surface: a tmux window when inside tmux,
    else an iTerm2 / Terminal.app tab (or window). False = no backend.

    Terminal.app tabs cost an Automation prompt (see below), so there a window
    is the default and `tabs` is the opt-in; iTerm2 and tmux pay nothing for a
    tab and keep it."""
    shell_cmd = f"cd {shlex.quote(cwd)} && {cmd}"
    tp = os.environ.get("TERM_PROGRAM", "")
    if os.environ.get("TMUX"):
        argv = ["tmux", "new-window"] + (["-d"] if detached else []) + ["-c", cwd, cmd]
        if dry:
            print("  " + " ".join(shlex.quote(a) for a in argv))
        else:
            subprocess.run(argv)
        return True
    if tp == "iTerm.app":
        window = ('tell application "iTerm2"\n  tell current session of (create window with default profile) '
                  f'to write text {applescript_str(shell_cmd)}\nend tell')
        tab = ('tell application "iTerm2"\n  tell current window\n    tell current session of '
               f'(create tab with default profile) to write text {applescript_str(shell_cmd)}\n'
               '  end tell\nend tell')
    elif tp == "Apple_Terminal":
        # `do script` opens a window, and Terminal sending itself an Apple
        # event needs no rights. A tab has no verb in the dictionary at all:
        # it takes a Cmd-T keystroke through System Events, another app, which
        # macOS gates behind Automation rights - so it stays opt-in.
        window = f'tell application "Terminal" to do script {applescript_str(shell_cmd)}'
        tab = ('tell application "Terminal"\n  activate\n  tell application "System Events" to keystroke "t" '
               f'using command down\n  delay 0.3\n  do script {applescript_str(shell_cmd)} in selected tab of '
               'front window\nend tell')
        if not tabs:
            tab = None
    else:
        return False
    scripts = [window] if (new_window or tab is None) else [tab, window]
    if dry:
        print("  osascript:\n    " + scripts[0].replace("\n", "\n    "))
        if len(scripts) > 1:
            print(f"    {DIM}(a new window instead, if the tab is refused){RESET}")
        return True
    ok, msg = run_osascript(scripts[0])
    if not ok and len(scripts) > 1:
        print(f"ccr: no new tab - {msg or 'the terminal refused it'}\n"
              f"     opening a window instead", file=sys.stderr)
        ok, msg = run_osascript(scripts[1])
    if not ok:
        print(f"ccr: could not start '{cmd}' - {msg or 'the terminal refused it'}", file=sys.stderr)
    return True


def exec_inline(cwd: str, argv: list, title: str, dry: bool, env_prefix: str = "", env: dict = None):
    """Hand this terminal over to the agent (the shell gets it back on exit).
    With several accounts the tool's data dir is set on this process only."""
    if dry:
        print(f"this tab: {env_prefix}{' '.join(shlex.quote(a) for a in argv)}   (cd {cwd})")
        return
    os.chdir(cwd)
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()
    try:
        if env:
            os.execvpe(argv[0], argv, env)
        os.execvp(argv[0], argv)
    except OSError as e:
        sys.exit(f"ccr: cannot start {argv[0]}: {e}")


# ----------------------------------------------------------------------------
# installing the tools: Ctrl-T in the picker (Ctrl+I or Ctrl+T in the
# PowerShell picker; fzf sees Ctrl-I as Tab), --install <tool>
# ----------------------------------------------------------------------------
# The vendors' published installers, fetched from their official URLs at the
# moment of the install and run as published: ccr keeps no copy of them. Both
# check what they download (claude: the SHA256 listed in a signed manifest;
# codex: the release's SHA256 sums), need no admin rights and no Node.js, and
# put their tool on the PATH (a link in ~/.local/bin plus the shell rc file).
INSTALLER_URL = {"claude": {"nt": "https://claude.ai/install.ps1", "posix": "https://claude.ai/install.sh"},
                 "codex": {"nt": "https://chatgpt.com/codex/install.ps1", "posix": "https://chatgpt.com/codex/install.sh"}}


def installer_url(tool: str) -> str:
    return INSTALLER_URL[tool]["nt" if os.name == "nt" else "posix"]


def tool_path(tool: str) -> str:
    """The tool's executable, '' when it is not installed here. Right after an
    install the installers' link dir may be missing from this process's PATH
    (they write the shell rc file for new shells); found there, the dir is
    added to this process's PATH so launches and exec find it too."""
    found = shutil.which(tool)
    if found:
        return found
    exe = tool + (".exe" if os.name == "nt" else "")
    dirs = [HOME / ".local" / "bin"]
    if os.name == "nt" and tool == "codex" and os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OpenAI" / "Codex" / "bin")
    for d in dirs:
        if (d / exe).is_file():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return str(d / exe)
    return ""


def tool_install(tool: str) -> dict:
    """Where a tool comes from on this machine: path, version, and how it was
    installed - 'native' (claude's own installer), 'standalone' (codex's own
    installer), 'npm', 'winget', 'brew', 'other', or '' when it is not
    installed."""
    path = tool_path(tool)
    if not path:
        return {"tool": tool, "path": "", "real": "", "version": "", "method": ""}
    real = os.path.realpath(path)
    ver = ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60).stdout or ""
        m = re.search(r"\d+\.\d+\.\d+[^\s)]*", out)
        ver = m.group(0) if m else ""
    except (OSError, subprocess.SubprocessError):
        pass
    r = real.replace("\\", "/").lower()
    home = str(HOME).replace("\\", "/").lower()
    local = (os.environ.get("LOCALAPPDATA") or "").replace("\\", "/").lower()
    roam = (os.environ.get("APPDATA") or "").replace("\\", "/").lower()
    if tool == "claude" and (r in (f"{home}/.local/bin/claude", f"{home}/.local/bin/claude.exe")
                             or "/.local/share/claude/versions/" in r):
        method = "native"
    elif tool == "codex" and ((local and r.startswith(f"{local}/programs/openai/codex/")) or "/packages/standalone/" in r):
        method = "standalone"
    elif (roam and r.startswith(f"{roam}/npm/")) or "/node_modules/" in r:
        method = "npm"
    elif "/microsoft/winget/" in r:
        method = "winget"
    elif "/caskroom/" in r:
        method = "brew"
    else:
        method = "other"
    return {"tool": tool, "path": path, "real": real, "version": ver, "method": method}


def install_plan(st: dict) -> dict:
    """What installing or updating a tool means here. A missing tool gets its
    vendor's published installer; an installed one is updated the way it was
    installed (claude: `claude update`; codex has no update command, so its
    installer again, which updates in place; npm / winget / brew their own
    upgrade); one installed another way is left alone."""
    tool, method = st["tool"], st["method"]
    data_dir = default_root(tool).path
    plan = {"tool": tool, "verb": "", "kind": "none", "text": "", "argv": [], "data_dir": data_dir, "url": "", "note": ""}
    if not method or (tool == "codex" and method == "standalone"):
        url = installer_url(tool)
        # Claude's installer makes the channel it installs from the one it
        # auto-updates from, and settings.json may be shared across machines:
        # keep "stable" when that is the chosen channel.
        target = ""
        if tool == "claude":
            sj = _json_at(Path(data_dir) / "settings.json")
            if isinstance(sj, dict) and str(sj.get("autoUpdatesChannel") or "") == "stable":
                target = "stable"
        if os.name == "nt":
            script = f"& ([scriptblock]::Create((irm {url}))) {target}" if target else f"irm {url} | iex"
            plan["argv"] = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
                                 / "powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
        else:
            script = (f"curl -fsSL {url} | bash" + (f" -s {target}" if target else "")) if tool == "claude" \
                else f"curl -fsSL {url} | sh"
            plan["argv"] = ["sh", "-c", script]
        plan.update(kind="installer", verb="update" if method else "install", text=script, url=url)
        if tool == "claude":
            plan["note"] = ("Claude's published installer: it checks the download against a signed manifest and puts "
                            f"claude on your PATH ({target or 'latest'} channel, as in settings.json).")
        else:
            plan["note"] = ("Codex's published installer: it checks the download against the release's SHA256 sums, "
                            f"keeps its package in the default account's data dir ({fmt_cwd(data_dir, 40)}) and puts "
                            "codex on your PATH" + ("; run again it updates in place." if method else "."))
        return plan
    argv, note = [], ""
    if tool == "claude" and method == "native":
        argv, note = [st["path"], "update"], "Claude updates itself in place, on the channel set in settings.json."
    elif method == "npm":
        pkg = "@anthropic-ai/claude-code" if tool == "claude" else "@openai/codex"
        argv, note = ["npm", "install", "-g", f"{pkg}@latest"], "Installed with npm, so updated with npm (the vendor's documented command)."
    elif method == "winget" and tool == "claude":
        argv, note = ["winget", "upgrade", "Anthropic.ClaudeCode"], "Installed with WinGet, so updated with WinGet."
    elif method == "brew":
        m = re.search(r"/caskroom/([^/]+)/", st["real"].replace("\\", "/"), re.I)
        cask = m.group(1) if m else ("claude-code" if tool == "claude" else "codex")
        argv, note = ["brew", "upgrade", "--cask", cask], "Installed with Homebrew, so updated with Homebrew."
    if not argv:
        plan.update(verb="leave", note=f"{tool} is installed at {fmt_cwd(st['path'], 60)} by a method ccr does not "
                                       "manage - update it the way you installed it.")
        return plan
    shown = ["claude"] + argv[1:] if argv[0] == st["path"] else argv
    plan.update(kind="command", verb="update", argv=argv, text=" ".join(shown), note=note)
    return plan


def account_missing(tool: str, root) -> str:
    """'' when the account's dir exists on this PC, else the error with the
    command that fixes it: codex refuses a CODEX_HOME that does not exist,
    and claude would start from an empty dir. An account added on another
    PC has none here until this PC logs in (ccr.json may be synced, the
    codex dirs are not)."""
    if not root or not root.path or Path(root.path).is_dir():
        return ""
    fix = ("claude auth login" if tool == "claude" else "codex login") if root.default \
        else f"ccr --add-account {root.label} --tool {tool}   (or Ctrl-A in the picker, then Ctrl-L on its row)"
    return f"ccr: {tool} account '{root.label}' is not on this PC - {root.path} does not exist. Fix: {fix}"


def add_user_path(d: str, key_path: str = "Environment") -> bool:
    """Windows: a folder on the User PATH, so new windows and tabs find what is
    in it. Written to the registry as it is (an expandable string keeps its
    %VARS%), then Windows is told the environment changed. True when it was
    added. Claude Code's installer puts claude.exe in ~/.local/bin and only
    says so when that folder is not on the PATH (codex's installer adds its
    own folder)."""
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                raw, kind = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                raw, kind = "", winreg.REG_EXPAND_SZ
            raw = str(raw or "")
            have = [os.path.expandvars(x).rstrip("\\").lower() for x in raw.split(";") if x]
            if d.rstrip("\\").lower() in have:
                return False
            winreg.SetValueEx(k, "Path", 0, kind, ";".join(x for x in (raw.rstrip(";"), d) if x))
    except OSError:
        return False
    if key_path == "Environment":
        res = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(res))
    return True


def run_install(tool: str, dry: bool) -> bool:
    """Install or update one tool. The installer runs as a child process with
    the tool's data dir set to the default account's (codex keeps its package
    there) and the terminal attached, so its own questions reach you."""
    before = tool_install(tool)
    plan = install_plan(before)
    if plan["kind"] == "none":
        print(f"{YELLOW}ccr: {plan['note']}{RESET}", file=sys.stderr)
        return False
    if dry:
        print(f"dry-run: would run  {plan['text']}   ({tool}: {before['version'] + ' ' + before['method'] if before['method'] else 'not installed'})")
        return True
    argv = list(plan["argv"])
    exe = shutil.which(argv[0]) or argv[0]   # npm.cmd / powershell.exe on Windows
    print(f"{YELLOW}ccr: {plan['verb']} {tool} - running: {plan['text']}{RESET}")
    env = tool_env(tool, plan["data_dir"])
    if os.name == "nt" and plan["kind"] == "installer":
        # System32 first, as in a stock shell: codex's installer calls a bare
        # `tar`, and the GNU tar of Git for Windows reads "C:\..." as a
        # remote host and fails.
        env["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32") + os.pathsep + env.get("PATH", "")
    try:
        code = subprocess.run([exe] + argv[1:], env=env).returncode
    except OSError as e:
        print(f"ccr: cannot run {argv[0]}: {e}", file=sys.stderr)
        return False
    lb = HOME / ".local" / "bin"
    if tool == "claude" and os.name == "nt" and (lb / "claude.exe").is_file() and add_user_path(str(lb)):
        print(f"{YELLOW}ccr: {lb} added to your PATH (claude's installer does not add it) - new windows find claude{RESET}")
    after = tool_install(tool)
    if after["path"]:
        was = f" (was {before['version']})" if before["version"] and after["version"] and before["version"] != after["version"] else ""
        print(f"{GREEN}ccr: {tool} {after['version']} is ready{was} - {fmt_cwd(after['path'], 60)}{RESET}")
        if not shutil.which(tool):
            print(f"{DIM}ccr: new shells find it once they read the updated rc file - open a new terminal.{RESET}")
        return code == 0
    print(f"ccr: {tool} is still not found after the installer (exit code {code}) - open a new terminal, "
          "or see the vendor's instructions.", file=sys.stderr)
    return False


def install_page():
    """Ctrl-T: claude and codex, their state here and what Enter runs.
    Returns the tool picked (after a confirmation), or None."""
    print(f"{DIM}ccr: looking at claude and codex on this computer...{RESET}")
    rows, plans = [], {}
    for t in ("claude", "codex"):
        st = tool_install(t)
        pl = install_plan(st)
        plans[t] = (st, pl)
        state = f"{st['version']}  {st['method']}" if st["method"] else "not installed"
        state_col = f"{state:<26}" if st["method"] else f"{YELLOW}{state:<26}{RESET}"
        what = {"install": "Enter installs it", "update": "Enter updates it"}.get(pl["verb"], "ccr leaves it alone")
        runs = f"   {DIM}{pl['text']}{RESET}" if pl["kind"] != "none" else ""
        rows.append(f"{t}\t{TOOL_COLOR[t]}{t:<7}{RESET} {state_col}  {DIM}{what}{RESET}{runs}")
    res = run_fzf(rows, hint(("Enter", "install / update"), ("Esc", "back"),
                             tail="the vendors' published installers, fetched now and run as published"),
                  multi=False, preview=False, prompt="install> ")
    if not res or not res[1]:
        return None
    t = res[1][0]
    st, pl = plans[t]
    if pl["kind"] == "none":
        print(f"{YELLOW}ccr: {pl['note']}{RESET}")
        pause("(Enter to continue)")
        return None
    print(f"\n{BOLD}{pl['verb'].capitalize()} {t}{RESET}\n    runs    {BOLD}{pl['text']}{RESET}")
    if pl["kind"] == "installer":
        print(f"    from    {pl['url']}  {DIM}(downloaded now, run as published){RESET}")
    print(f"\n  {DIM}{pl['note']}{RESET}\n")
    ans = ask("  run it? [y/N] ")
    return t if ans and ans.strip().lower() in ("y", "yes") else None


# ----------------------------------------------------------------------------
# launch options: a model and an effort for the conversations being resumed
# (Ctrl-E here; Shift+Enter in the PowerShell picker, which fzf cannot see)
# ----------------------------------------------------------------------------
# Values go on command lines ccr writes: model ids and effort levels only
# (letters, digits, . _ - : and a [1m] suffix).
OPT_VALUE_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,64}$")
# Claude Code's effort levels (claude --help: --effort <level>).
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Codex levels for a model its cache does not describe.
CODEX_EFFORTS = ["low", "medium", "high", "xhigh"]
CLAUDE_ALIASES = [("fable", "latest Fable"), ("opus", "latest Opus"), ("opus[1m]", "latest Opus, 1M context"),
                  ("sonnet", "latest Sonnet"), ("sonnet[1m]", "latest Sonnet, 1M context"), ("haiku", "latest Haiku")]


def _json_at(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def configured_model(tool: str, root_paths: list) -> dict:
    """What a tool is set to use when no flag is passed, read from the data
    dirs the conversations will run under: claude settings.json (model,
    effortLevel; per_model when modelSettings sets efforts per model), codex
    config.toml (top-level model, model_reasoning_effort). A value is '*'
    when those dirs disagree and '' when none sets it."""
    models, efforts, per_model = [], [], False
    for p in dict.fromkeys(root_paths):
        m = e = ""
        if tool == "claude":
            j = _json_at(Path(p) / "settings.json")
            if isinstance(j, dict):
                m, e = str(j.get("model") or ""), str(j.get("effortLevel") or "")
                per_model = per_model or bool(j.get("modelSettings"))
        else:
            try:
                for line in (Path(p) / "config.toml").read_text(encoding="utf-8").splitlines():
                    if re.match(r"\s*\[", line):
                        break   # top-level keys only
                    mm = re.match(r'\s*model\s*=\s*"([^"]*)"', line)
                    if mm:
                        m = mm.group(1)
                        continue
                    mm = re.match(r'\s*model_reasoning_effort\s*=\s*"([^"]*)"', line)
                    if mm:
                        e = mm.group(1)
            except OSError:
                pass
        models.append(m)
        efforts.append(e)

    def one(xs):
        u = list(dict.fromkeys(xs))
        return u[0] if len(u) == 1 else ("*" if u else "")
    return {"model": one(models), "effort": one(efforts), "per_model": per_model}


def model_choices(tool: str, root_paths: list) -> list:
    """The models offered for a tool, as (value, note, efforts). Codex: the
    models its own /model menu lists (models_cache.json, visibility "list",
    in its priority order), each with the effort levels it supports. Claude
    keeps no such list on disk, so: the aliases `claude --model` resolves to
    the latest version, the extra options Claude Code caches in .claude.json
    (additionalModelOptionsCache), and the model settings.json names."""
    out, seen = [], set()

    def add(value, note, efforts):
        value = str(value or "")
        if value and OPT_VALUE_RE.match(value) and value.lower() not in seen:
            seen.add(value.lower())
            out.append((value, note, list(efforts)))

    def prio(m):
        try:
            return int(m.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    def levels(m):
        return [str(x["effort"]) for x in (m.get("supported_reasoning_levels") or [])
                if isinstance(x, dict) and x.get("effort")]

    paths = list(dict.fromkeys(root_paths))
    cfg = configured_model(tool, paths)
    if tool == "claude":
        for v, note in CLAUDE_ALIASES:
            add(v, note, CLAUDE_EFFORTS)
        for p in paths:
            j = _json_at(Path(p) / ".claude.json")
            opts = j.get("additionalModelOptionsCache") if isinstance(j, dict) else None
            for o in ([opts] if isinstance(opts, dict) else opts if isinstance(opts, list) else []):
                # a disabled entry is a notice ("Update to 2.1.280+ to use
                # Opus 5.5"), not a model
                if isinstance(o, dict) and o.get("value") and not o.get("disabled"):
                    note = " · ".join(x for x in (str(o.get("label") or ""), str(o.get("description") or "")) if x)
                    add(o["value"], note, CLAUDE_EFFORTS)
        if cfg["model"] and cfg["model"] != "*":
            add(cfg["model"], "the default in settings.json", CLAUDE_EFFORTS)
    else:
        models = []
        for p in paths:
            j = _json_at(Path(p) / "models_cache.json")
            if isinstance(j, dict) and isinstance(j.get("models"), list):
                models += [m for m in j["models"] if isinstance(m, dict)]
        for m in sorted((m for m in models if m.get("visibility") == "list"), key=prio):
            add(m.get("slug"), str(m.get("display_name") or ""), levels(m))
        if cfg["model"] and cfg["model"] != "*":
            known = next((m for m in models if m.get("slug") == cfg["model"]), None)
            add(cfg["model"], "the default in config.toml", levels(known) if known else CODEX_EFFORTS)
    return out


def effort_choices(tool: str, choices: list, model: str, configured: str) -> list:
    """Effort levels for a model choice ('' = the configured model): the ones
    that model supports when known, else the tool's whole list."""
    m = model or (configured if configured != "*" else "")
    hit = next((c for c in choices if m and c[0].lower() == m.lower()), None)
    if hit and hit[2]:
        return list(hit[2])
    if tool == "claude":
        return list(CLAUDE_EFFORTS)
    return list(dict.fromkeys(e for c in choices for e in c[2])) or list(CODEX_EFFORTS)


def override_args(tool: str, model: str, effort: str) -> list:
    """The flags that hand a model / effort to a resumed session: claude
    --model / --effort, codex -m / -c model_reasoning_effort=."""
    a = []
    if model and OPT_VALUE_RE.match(model):
        a += ["--model", model] if tool == "claude" else ["-m", model]
    if effort and OPT_VALUE_RE.match(effort):
        a += ["--effort", effort] if tool == "claude" else ["-c", f"model_reasoning_effort={effort}"]
    return a


def run_root_path(ctx, s) -> str:
    """The data dir a picked row will run under: its target account when the
    picker re-homes it, else its own."""
    if s.target_root and s.target_root != s.root:
        r = next((r for r in ctx.roots(s.tool, all_=True) if r.label == s.target_root), None)
        if r:
            return r.path
    if s.root_path:
        return s.root_path
    return str(claude_root() if s.tool == "claude" else codex_root())


def _default_note(tool: str, val: str) -> str:
    """Where a tool's own setting comes from, for a "(no override)" row."""
    src = "settings" if tool == "claude" else "config.toml"
    if val == "*":
        return f"{src}: differs per account"
    return f"{src}: {val}" if val else f"{src}: not set"


def choose_model(tool: str, choices: list, cfg: dict, title: str, back: str = "back"):
    """A tool's models as an fzf menu. Returns the model ('' = no override,
    the tool's own setting), or None on Esc."""
    rows = [f"0\t{'(no override)':<26}{DIM}{_default_note(tool, cfg['model'])}{RESET}"]
    rows += [f"{i}\t{v:<26}{DIM}{note}{RESET}" for i, (v, note, _) in enumerate(choices, 1)]
    res = run_fzf(rows, title + "\n" + hint(("Enter", "choose"), ("Esc", back), tail="model"),
                  multi=False, preview=False, prompt=f"{tool} model> ")
    if not res or not res[1]:
        return None
    i = int(res[1][0])
    return choices[i - 1][0] if i else ""


def choose_effort(tool: str, choices: list, cfg: dict, model: str, title: str, back: str = "back"):
    """The effort levels of a model ('' = the configured one) as an fzf menu.
    Returns the level ('' = no override), or None on Esc."""
    levels = effort_choices(tool, choices, model, cfg["model"])
    note = "settings: per model" if tool == "claude" and cfg["per_model"] else _default_note(tool, cfg["effort"])
    rows = [f"0\t{'(no override)':<26}{DIM}{note}{RESET}"]
    rows += [f"{i}\t{lv}" for i, lv in enumerate(levels, 1)]
    res = run_fzf(rows, title + "\n" + hint(("Enter", "choose"), ("Esc", back),
                                           tail=f"effort{f' for {model}' if model else ''}"),
                  multi=False, preview=False, prompt=f"{tool} effort> ")
    if not res or not res[1]:
        return None
    i = int(res[1][0])
    return levels[i - 1] if i else ""


def launch_options(picked: list, ctx):
    """Ctrl-E: a model and an effort for the rows about to be resumed - the
    PowerShell picker's Shift+Enter page, as fzf menus: model, then effort,
    for each tool in the selection. Both start at "no override" (no flag,
    the tool's own setting). Returns {tool: (model, effort)}, or None when
    backed out."""
    out = {}
    for tool in ("claude", "codex"):
        rows_t = [x for x in picked if x.tool == tool]
        if not rows_t:
            continue
        paths = list(dict.fromkeys(run_root_path(ctx, x) for x in rows_t))
        cfg = configured_model(tool, paths)
        choices = model_choices(tool, paths)
        n = len(rows_t)
        title = (f"{BOLD}Resume with a model / effort{RESET}  {TOOL_COLOR[tool]}{tool}{RESET} "
                 f"{DIM}· {n} conversation{'' if n == 1 else 's'}{RESET}")
        model = choose_model(tool, choices, cfg, title, "back to the list")
        if model is None:
            return None
        effort = choose_effort(tool, choices, cfg, model, title, "back to the list")
        if effort is None:
            return None
        out[tool] = (model, effort)
    return out


def resume_argv(s: Session) -> list:
    argv = ["claude", "--resume", s.id] if s.tool == "claude" else ["codex", "resume", s.id]
    return argv + override_args(s.tool, s.model_override, s.effort_override)


def root_prefix(ctx: Ctx, tool: str, root_path: str) -> str:
    """`VAR='dir' ` for a shell command line, '' when the tool has one dir."""
    var = ctx.root_var(tool)
    return f"{var}={shlex.quote(root_path)} " if var and root_path else ""


def root_env(ctx: Ctx, tool: str, root_path: str):
    var = ctx.root_var(tool)
    return tool_env(tool, root_path) if var and root_path else None


def url_opener() -> list:
    """Command prefix that hands a URL to the desktop handler, or []."""
    if sys.platform == "darwin":
        return ["open"]
    if os.name == "nt":
        return ["cmd", "/c", "start", ""]
    x = shutil.which("xdg-open")
    return [x] if x else []


def open_in_codex_app(s: Session, opener: list, dry: bool) -> bool:
    """Focus the thread in the Codex desktop app through its codex:// deeplink."""
    argv = opener + [f"codex://threads/{s.id}"]
    if dry:
        print("  " + " ".join(shlex.quote(a) for a in argv))
        return True
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        print(f"ccr: cannot reach the Codex app: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"ccr: 'codex app · {s.title}' - deeplink refused: "
              f"{(r.stderr or '').strip() or 'exit ' + str(r.returncode)}", file=sys.stderr)
        return False
    return True


def launch(picked: list, new_window: bool, dry: bool, ctx: Ctx, terminal: bool = False, tabs: bool = False):
    opener = [] if terminal else url_opener()
    app_list, launch_list = [], []
    for s in picked:
        if not UUID_RE.match(s.id):
            print(f"ccr: skipping '{s.title}' - unexpected session id", file=sys.stderr)
            continue
        # Desktop-app threads go back to the app, not to a terminal tab. The
        # app reads the default dir and cannot be given one per launch, so
        # no account change applies to them.
        if s.tool == "codex" and s.origin == "app" and not terminal:
            if opener:
                if s.target_root and s.target_root != s.root:
                    print(f"ccr: 'codex app · {s.title}' opens in the Codex app, which always runs as the "
                          f"default account - not moved to '{s.target_root}'", file=sys.stderr)
                if s.model_override or s.effort_override:
                    print(f"ccr: 'codex app · {s.title}' opens in the Codex app, which takes no model or effort "
                          f"from ccr - pick them there, or resume it in a terminal with --terminal", file=sys.stderr)
                app_list.append(s)
                continue
            print(f"ccr: no URL handler here - resuming 'codex app · {s.title}' in a terminal instead",
                  file=sys.stderr)
        # A tool that is not installed here (a codex conversation of the IDE
        # extension, say, on a machine without the CLI) cannot resume anything.
        if not tool_path(s.tool):
            print(f"ccr: skipping '{s.tool} · {s.title}' - {s.tool} is not installed on this computer "
                  f"(ccr --install {s.tool}, or Ctrl-T in the picker)", file=sys.stderr)
            continue
        cwd = s.cwd
        if not cwd or not Path(cwd).is_dir():
            if s.tool == "claude":
                print(f"ccr: skipping 'claude · {s.title}' - recorded folder no longer exists: {cwd}",
                      file=sys.stderr)
                continue
            print(f"ccr: 'codex · {s.title}' - recorded folder missing ({cwd}), starting in {HOME}",
                  file=sys.stderr)
            cwd = str(HOME)
        # Account mode: a target label different from the row's own account
        # means "move this conversation there, then open it there".
        root_path, move_to = s.root_path, None
        tgt = s.target_root or ""
        if tgt and tgt != s.root:
            move_to = next((r for r in ctx.roots(s.tool, all_=True) if r.label == tgt), None)
            if not move_to:
                print(f"ccr: no {s.tool} dir for account '{tgt}' - '{s.title}' stays under '{s.root}'", file=sys.stderr)
            elif s.running:
                print(f"ccr: '{s.title}' is running - close it before moving it to '{tgt}'; skipped", file=sys.stderr)
                continue
            elif s.running_on:
                print(f"ccr: '{s.title}' is open on {s.running_on} - close it there before moving it to '{tgt}'; skipped",
                      file=sys.stderr)
                continue
            elif account_missing(s.tool, move_to):
                print(f"{account_missing(s.tool, move_to)}   '{s.title}' skipped, not moved.", file=sys.stderr)
                continue
            else:
                root_path = move_to.path
        launch_list.append({"s": s, "cwd": cwd, "argv": resume_argv(s), "root_path": root_path, "move_to": move_to})
    if not app_list and not launch_list:
        print("ccr: nothing to open.")
        return
    # Deeplinks first: exec_inline below never returns.
    for s in app_list:
        open_in_codex_app(s, opener, dry)
    if app_list and not dry:
        n = len(app_list)
        print(f"ccr: {n} conversation{'' if n == 1 else 's'} handed to the Codex app.")
    if not launch_list:
        return
    # Re-home first, so the resume finds the transcript in its new dir. A
    # failed move leaves the transcript where it was, so the launch falls
    # back to the source account instead of resuming in a dir without it.
    for e in launch_list:
        if not e["move_to"]:
            continue
        s, m = e["s"], e["move_to"]
        if dry:
            print(f"move: {s.tool} · {s.title}  ->  account '{m.label}' ({m.path})")
            continue
        try:
            move_session_to_root(s, m)
            print(f"ccr: moved '{s.title}' to account '{m.label}'")
        except Exception as ex:
            print(f"ccr: could not move '{s.title}' to '{m.label}': {ex} - it opens under '{s.root}' instead",
                  file=sys.stderr)
            e["root_path"], e["move_to"] = s.root_path, None
    # First terminal selection takes over this terminal; the rest open as tabs.
    # (`rest`, not `tabs`: that name is the --tabs flag passed to open_tab.)
    inline, rest = (None, launch_list) if new_window else (launch_list[0], launch_list[1:])
    for e in rest:
        cmd = root_prefix(ctx, e["s"].tool, e["root_path"]) + " ".join(shlex.quote(a) for a in e["argv"])
        if not open_tab(e["cwd"], cmd, new_window, dry, tabs):
            print("ccr: opening several sessions needs iTerm2, Terminal.app or tmux - only the first starts.",
                  file=sys.stderr)
            break
    if inline:
        s = inline["s"]
        exec_inline(inline["cwd"], inline["argv"], f"{s.tool} · {s.title}", dry,
                    root_prefix(ctx, s.tool, inline["root_path"]), root_env(ctx, s.tool, inline["root_path"]))


# ----------------------------------------------------------------------------
# new conversation (Ctrl-N / -n)
# ----------------------------------------------------------------------------
CODEX_APP_BUNDLE = b"com.openai.codex"


def codex_app_installed(sessions: list) -> bool:
    """True when the Codex desktop app can take a codex:// link here.

    Having opened app threads before is proof enough and costs nothing. A fresh
    install has none, so fall back to looking for the bundle (Spotlight is not
    always available, so read the Info.plist bytes) or the registered handler."""
    if any(s.tool == "codex" and s.origin == "app" for s in sessions):
        return True
    try:
        if sys.platform == "darwin":
            for d in ("/Applications", "/System/Applications", str(HOME / "Applications")):
                for plist in Path(d).glob("*.app/Contents/Info.plist"):
                    try:
                        if CODEX_APP_BUNDLE in plist.read_bytes():
                            return True
                    except OSError:
                        continue
            return False
        if shutil.which("xdg-mime"):
            out = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/codex"],
                                 capture_output=True, text=True, timeout=5).stdout
            return bool(out.strip())
    except Exception:
        pass
    return False


def ask_new_folder(seed: str, dry: bool):
    """Read a folder path for a brand-new project, creating it on request.

    Relative paths resolve against the directory ccr runs in; ~ expands.
    Returns an existing directory, or None when the user backs out."""
    while True:
        raw = ask(f"new project folder{' [' + seed + ']' if seed else ''}> ")
        if raw is None:
            return None
        raw = raw.strip() or seed
        if not raw:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not p.is_absolute():
            p = Path.cwd() / p
        p = Path(os.path.normpath(str(p)))
        if p.is_dir():
            return str(p)
        if p.exists():
            print(f"ccr: {p} exists but is not a folder.", file=sys.stderr)
            continue
        if dry:
            print(f"dry-run: would create {p}")
            return str(p)
        ans = ask(f"  {p} does not exist.  {CYAN}[y]{RESET} create it"
                  f"    {DIM}anything else: type another path{RESET} > ")
        if ans is None:
            return None
        if ans.strip().lower() != "y":
            continue
        try:
            p.mkdir(parents=True)
        except OSError as e:
            print(f"ccr: cannot create {p}: {e}", file=sys.stderr)
            continue
        print(f"  {DIM}created {p}{RESET}")
        return str(p)


def open_new_app_thread(folder: str, prompt_text: str, opener: list, dry: bool) -> bool:
    """New thread in the Codex desktop app, rooted at folder.

    The app resolves ?path= to a workspace root and registers it as a project
    when it is not one yet - which is what makes 'new project folder' land
    somewhere useful. ?prompt= only prefills the composer, it sends nothing."""
    q = {"path": folder}
    if prompt_text:
        q["prompt"] = prompt_text
    # safe="/" keeps the folder readable in --dry-run; the app decodes either way.
    query = urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe="/")
    argv = opener + ["codex://threads/new?" + query]
    if dry:
        print("  " + " ".join(shlex.quote(x) for x in argv))
        return True
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        print(f"ccr: cannot reach the Codex app: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"ccr: the Codex app refused the new-thread link: "
              f"{(r.stderr or '').strip() or 'exit ' + str(r.returncode)}", file=sys.stderr)
        return False
    print(f"ccr: new Codex app conversation in {folder}")
    return True


def choose_folder(sessions: list, seed: str, dry: bool, tail: str, prompt: str = "new session in> "):
    """The folder step of a new conversation or a panel: "here" (the folder ccr
    runs in) first, "+ new folder", then every folder past sessions used, most
    recent first. Returns a folder (created on request), or None on Esc."""
    here = str(Path.cwd())
    groups = {}
    for s in sessions:
        if s.cwd.rstrip("\\/").lower() == here.rstrip("\\/").lower():
            continue
        g = groups.setdefault(s.cwd.lower(), {"path": s.cwd, "last": s.last, "count": 0})
        g["count"] += 1
        if s.last > g["last"]:
            g["last"] = s.last
    folders = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
    index, rows = {}, []
    # The folder ccr was started from is always the first row ("here"),
    # known or not, and stays there whatever the filter says.
    index["here"] = {"path": here}
    rows.append(f"here\t{GREEN}{'here':>6}{RESET}        {fmt_cwd(here, 70)}\t{here}")
    rows.append(f"new\t{ORANGE}+ new folder{RESET}  {DIM}type a path, ccr creates it{RESET}"
                f"\tstart a project in a folder no session has used yet")
    for i, g in enumerate(folders):
        index[str(i)] = g
        rows.append(f"{i}\t{fmt_age(g['last']):>6}  {DIM}{g['count']:>3}×{RESET}  "
                    f"{fmt_cwd(g['path'], 70)}\t{g['path']}")
    res = run_fzf(rows, hint(("Enter", "next"), ("Ctrl-O", "new folder"), ("Esc", "back"), tail=tail),
                  multi=False, prompt=prompt, expect=["ctrl-o"])
    if not res or not (res[0] or res[1]):
        return None
    if res[0] == "ctrl-o" or (res[1] and res[1][0] == "new"):
        return ask_new_folder(seed if os.sep in seed else "", dry) or None
    return index[res[1][0]]["path"]


def new_conversation(sessions: list, initial_name: str, dry: bool, ctx: Ctx, terminal: bool = False) -> bool:
    """Folder -> tool -> (claude) name -> (several accounts for that tool)
    account, then the tool takes over this terminal. Or folder -> panel ->
    question -> agents, and the panel runs here."""
    folder = choose_folder(sessions, initial_name, dry, "new conversation · step 1: folder")
    if not folder:
        return False
    opener = [] if terminal else url_opener()
    app_ok = bool(opener) and codex_app_installed(sessions)
    extra = []
    if app_ok:
        extra.append(f"codex app\t{CYAN}codex{RESET}{DIM} app{RESET}{' ' * 2}{DIM}a new thread in the Codex "
                     f"desktop app, rooted at this folder{RESET}\tcodex app desktop")
    extra.append(f"panel\t{MAGENTA}MAP{RESET}{' ' * 8}{DIM}multi-agent panel: several agents discuss one question "
                 f"until they agree{RESET}\tMAP multi-agent panel")
    tool = choose_tool(f"step 2: tool · {fmt_cwd(folder, 46)}", "asks for a session name",
                       "a terminal tab · no start name - /rename inside", extra)
    if not tool:
        return False
    if tool == "panel":
        # A panel runs its agents here, in this terminal, without handing it
        # over: the question, then the agents; Esc on the agents goes back to
        # the question.
        if not dry and not Path(folder).is_dir():
            print(f"ccr: folder no longer exists: {folder}", file=sys.stderr)
            return False
        q = ""
        while True:
            q = read_panel_question(panel_default(ctx), folder, q)
            if q is None:
                return False
            choice = panel_page(folder, ctx, None, q)
            if choice:
                if not dry:
                    save_panel_default(choice)
                return run_panel(choice, q, folder, ctx, dry)
    if tool != "codex app" and not dry and not Path(folder).is_dir():
        print(f"ccr: folder no longer exists: {folder}", file=sys.stderr)
        return False
    if tool == "codex app":
        seed = f" [{initial_name}]" if initial_name else ""
        first = ask(f"first message for codex (empty = just open the folder){seed}> ")
        if first is None:
            return False
        return open_new_app_thread(folder, first.strip() or initial_name, opener, dry)
    name = ""
    if tool == "claude":
        seed = f" [{initial_name}]" if initial_name else ""
        name = ask(f"session name for claude (empty = auto title){seed}> ")
        if name is None:
            return False
        name = name.strip() or initial_name
    # Account: only asked for a tool with several accounts configured.
    roots = ctx.roots(tool)
    root = None
    if ctx.root_var(tool):
        if len(roots) > 1:
            # Label, dir and who is logged in there, from the dir's own files.
            dir_w = max(len(fmt_cwd(r.path, 40)) for r in roots)
            lbl = choose_account([(r.label, f"{fmt_cwd(r.path, 40):<{dir_w}}  {who_at(tool, r.path)}", r.default) for r in roots],
                                 "step 3: account for the new conversation")
            if lbl is None:
                return False
            root = next(r for r in roots if r.label == lbl)
        else:
            root = roots[0] if roots else default_root(tool)
    if not tool_path(tool):
        print(f"ccr: {tool} is not installed on this PC. Fix: ccr --install {tool}   (or Ctrl-T in the picker)",
              file=sys.stderr)
        return False
    miss = account_missing(tool, root) if ctx.root_var(tool) else ""
    if miss:
        print(miss, file=sys.stderr)
        return False
    argv = ["claude", "--name", name] if (tool == "claude" and name) else [tool]
    rp = root.path if root else ""
    exec_inline(folder, argv, f"{tool} · {name or 'new'}", dry, root_prefix(ctx, tool, rp), root_env(ctx, tool, rp))
    return True


# ----------------------------------------------------------------------------
# panel: several fresh agents discuss one question until they agree
# (Ctrl-N -> folder -> panel, or ccr --panel "question")
# ----------------------------------------------------------------------------
# Limits: agents per panel, characters of one forwarded message.
PANEL_MAX_AGENTS = 4
PANEL_FORWARD_CAP = 20000
# The claude tools a reviewer does not get (it changes no file).
PANEL_EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
CONSENSUS_RE = re.compile(r"^\W*CONSENSUS\W*:?\W*(AGREE|CONTINUE)\b", re.I)
# Markdown around a CONSENSUS line (**CONSENSUS: AGREE**, > CONSENSUS: ...).
MARKDOWN_RE = re.compile(r"[*_>#`]")
# What a reply may not print: C0 controls but newline and tab, DEL, C1 - an
# escape sequence in a reply must not reach the terminal.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class PanelAgent:
    """One agent of a panel: tool, model and effort ('' = what the tool is
    configured to use), account ('' = the tool's default), master. run_panel
    fills in the rest."""

    def __init__(self, tool: str = "claude", model: str = "", effort: str = "", account: str = "",
                 master: bool = False):
        self.tool, self.model, self.effort, self.account, self.master = tool, model, effort, account, bool(master)
        self.index, self.root, self.env, self.exe, self.name = 0, None, None, "", ""
        self.eff_model, self.eff_effort, self.master_mode = "", "", "acceptEdits"
        self.web_live = True   # codex: the web live unless the account's config.toml chooses
        self.session_id, self.thread_id = "", ""
        self.active, self.last, self.turns, self.inp, self.out, self.denials = True, None, 0, 0, 0, 0
        self.starts, self.multi = 0, False
        self.prev, self.prev_failed, self.done = None, "", 0   # a saved MAP going on: its state of this agent

    def copy(self):
        c = PanelAgent(self.tool, self.model, self.effort, self.account, self.master)
        c.prev = self.prev
        return c


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def panel_default(ctx) -> dict:
    """The panel used last time (ccr.json "panel"), else codex + claude on
    their configured models. ccr.json syncs between PCs while account dirs
    may not: an account that is not on this PC falls back to the default."""
    agents, rounds, tabs = [], 4, True
    p = (load_config() or {}).get("panel")
    if isinstance(p, dict):
        for x in p.get("agents") or []:
            if not isinstance(x, dict) or x.get("tool") not in ("claude", "codex"):
                continue
            acct = str(x.get("account") or "")
            if acct:
                r = next((r for r in ctx.roots(x["tool"], all_=True) if r.label == acct), None)
                if not r or not os.path.isdir(r.path):
                    acct = ""
            model, effort = str(x.get("model") or ""), str(x.get("effort") or "")
            agents.append(PanelAgent(x["tool"], model if OPT_VALUE_RE.match(model) else "",
                                     effort if OPT_VALUE_RE.match(effort) else "", acct, bool(x.get("master"))))
        if _as_int(p.get("rounds")) >= 2:
            rounds = min(8, _as_int(p.get("rounds")))
        if p.get("tabs") is not None:
            tabs = bool(p.get("tabs"))
    if not 2 <= len(agents) <= PANEL_MAX_AGENTS:
        agents = [PanelAgent("codex"), PanelAgent("claude")]
    seen = False
    for a in agents:
        if a.master:
            a.master, seen = not seen, True
    return {"agents": agents, "rounds": rounds, "tabs": tabs}


def save_panel_default(panel: dict):
    """Remember the panel for next time (ccr.json "panel"; other keys untouched)."""
    try:
        cfg = load_config() or {}
        old = cfg.get("panel") if isinstance(cfg.get("panel"), dict) else {}
        val = {"rounds": int(panel["rounds"]),
               "agents": [{"tool": a.tool, "model": a.model, "effort": a.effort, "account": a.account,
                           "master": a.master} for a in panel["agents"]]}
        val["tabs"] = bool(panel.get("tabs", True))
        # keys the page does not show stay as the user wrote them
        if old.get("askAfterMinutes") is not None:
            val["askAfterMinutes"] = old["askAfterMinutes"]
        if _as_int(old.get("turnTimeoutMinutes")) > 0:
            val["turnTimeoutMinutes"] = _as_int(old.get("turnTimeoutMinutes"))
        cfg["panel"] = val
        save_config(cfg)
    except Exception:
        pass   # not remembered: the panel still runs


def panel_root(ctx, a) -> Root:
    """The account an agent runs under: its own, else the tool's default."""
    roots = ctx.roots(a.tool, all_=True)
    return (next((r for r in roots if a.account and r.label == a.account), None)
            or next((r for r in roots if r.default), None) or Root("", tool_default_root(a.tool), True))


def panel_effective(a, conf: dict) -> tuple:
    """The model and effort an agent really runs with: its override, else
    what its account is configured to use. Always passed on the command
    line: a read-only claude agent runs with --restricted, which ignores
    settings.json."""
    m = a.model or (conf["model"] if conf["model"] not in ("", "*") else "")
    e = a.effort or (conf["effort"] if conf["effort"] not in ("", "*") else "")
    return m, e


def panel_agent_label(a) -> str:
    """"codex gpt-6-astra max": tool, model and effort an agent runs with."""
    return " ".join(x for x in (a.tool, a.eff_model or a.model or "its default model", a.eff_effort or a.effort) if x)


def panel_short(question: str, max_: int = 50) -> str:
    """First line of the question, safe for a command line and a file name."""
    first = next((l for l in re.split(r"\r?\n", question or "") if l.strip()), "").strip()
    s = re.sub(r"\s+", " ", re.sub(r'["&|<>^%\\`$]', "", first)).strip()
    if len(s) > max_:
        s = s[: max_ - 1].rstrip() + "…"
    return s


def panel_master_mode(root_path: str, master: bool = True) -> str:
    """The permission mode of a claude agent, as its CLI would run: auto when
    the account's settings.json default mode is auto; else acceptEdits for the
    master (it must change files) and default for a reviewer (reads and the
    web, which ccr always allows; whatever would ask is denied, nobody can
    answer)."""
    j = _json_at(Path(root_path) / "settings.json")
    perms = j.get("permissions") if isinstance(j, dict) else None
    mode = str(perms.get("defaultMode") or "") if isinstance(perms, dict) else ""
    if mode == "auto":
        return "auto"
    return "acceptEdits" if master else "default"


def codex_sets_web_search(root_path: str) -> bool:
    """Whether the account's config.toml chooses codex's web search itself
    (top-level web_search = "live" / "cached" / "disabled"); if not, a codex
    agent gets it live."""
    try:
        for line in (Path(root_path) / "config.toml").read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*\[", line):
                break   # top-level keys only
            if re.match(r"\s*web_search\s*=", line):
                return True
    except OSError:
        pass
    return False


def panel_protocol(index: int, agents: list, folder: str, question: str) -> str:
    """The rules ccr puts in front of the question: fixed, shown before the
    question is asked, saved with the transcript."""
    me = agents[index - 1]
    has_master = any(a.master for a in agents)
    others = "; ".join(f"agent {j}: {panel_agent_label(a)}{' (master)' if a.master else ''}"
                       for j, a in enumerate(agents, 1) if j != index)
    if me.master:
        role = ("You are the master: the only agent allowed to change files, and only inside this folder. "
                "Do not change files in round 1. From round 2 you may change files to try or implement what "
                "the panel converges on; list every change. Never commit or push.")
    elif has_master:
        role = ("You are a reviewer: read the files, search the web and run what you need, but do not change any "
                "file. The master's changes are already in the folder when you read: review them.")
    else:
        role = ("You are a reviewer: read the files, search the web and run what you need, but do not change any "
                "file; nobody on the panel changes files.")
    text = f"""[ccr panel] {panel_short(question, 120)}

You are agent {index} of {len(agents)} on a panel of AI agents working in {folder}.
The other agents: {others}.
{role}
How the panel works:
- Precision comes first, speed does not matter: check every fact in primary sources -
  search the web for laws, official documents and documentation, read the files,
  compute with code - and cite the sources you rely on.
- Project conventions: read the folder's CLAUDE.md and AGENTS.md, if any, unless
  they are already in your context.
- Round 1: answer on your own and end with CONSENSUS: CONTINUE.
- Every later round: you get the other agents' latest messages. Check them against
  the code, say plainly what is wrong or missing, adopt what is better, and state the
  current best solution in full.
- Nobody on the panel can ask the user anything until it ends: state your assumptions.
- Keep each message under about 700 words.
End every message with exactly one final line, always in English:
CONSENSUS: AGREE     only if your message changes nothing in the common solution
CONSENSUS: CONTINUE  otherwise
Do not agree just to be agreeable. Answer in the language of the question.

QUESTION:
{question}"""
    return text.replace("\r\n", "\n")


def panel_consensus(text: str) -> str:
    """AGREE, CONTINUE or '' (no marker): the last marker among the last
    three non-empty lines, markdown stripped."""
    res = ""
    for l in [l for l in re.split(r"\r?\n", text or "") if l.strip()][-3:]:
        m = CONSENSUS_RE.match(MARKDOWN_RE.sub("", l))
        if m:
            res = m.group(1).upper()
    return res


def remove_consensus_line(text: str) -> str:
    """The text without its CONSENSUS line (forwarded messages carry it in the tag)."""
    lines = re.split(r"\r?\n", text or "")
    seen = 0
    for i in range(len(lines) - 1, -1, -1):
        if seen >= 3:
            break
        if not lines[i].strip():
            continue
        seen += 1
        if CONSENSUS_RE.match(MARKDOWN_RE.sub("", lines[i])):
            del lines[i]
    return "\n".join(lines).rstrip()


def panel_item(a) -> dict:
    """One agent's latest message, as forwarded to the others."""
    return {"index": a.index, "tool": a.tool, "model": a.eff_model or "default",
            "consensus": a.last["consensus"] if a.last else "", "text": a.last["text"] if a.last else ""}


def format_panel_messages(items: list) -> str:
    """The other agents' messages as <agent> blocks, cut at the forward cap."""
    out = []
    for it in items:
        t = remove_consensus_line(it["text"])
        if len(t) > PANEL_FORWARD_CAP:
            t = t[:PANEL_FORWARD_CAP] + "\n[truncated by ccr]"
        out.append(f'<agent n="{it["index"]}" tool="{it["tool"]}" model="{it["model"]}" '
                   f'consensus="{it["consensus"] or "none"}">\n{t.rstrip()}\n</agent>\n\n')
    return "".join(out)


def panel_round_message(k: int, items: list, remind: bool) -> str:
    s = f"Round {k}. Latest messages from the other agents:\n\n" + format_panel_messages(items)
    if remind:
        s += "Reminder: your last message had no CONSENSUS line; end this one with it.\n"
    return s + "Reply following the panel rules."


def panel_final_message(agreed: bool, items: list) -> str:
    """The last turn of the panel, for the master (else the first active agent)."""
    s = ("The panel has reached consensus." if agreed else "The panel ends without consensus.") + "\n"
    if items:
        s += "Latest messages from the other agents:\n\n" + format_panel_messages(items)
    return s + ("Write the agreed solution in full, as the final answer for the user. No new proposals, and no "
                "CONSENSUS line." if agreed else
                "Write the final summary for the user: first the points all agents agree on, then the open "
                "disagreements, agent by agent. No CONSENSUS line.")


def panel_argv(a, first: bool, session_id: str, name: str = "", master_mode: str = "acceptEdits") -> list:
    """The arguments of one agent turn (no executable). Pure: the tests and
    --dry-run use the very same lines. session_id: claude = the uuid ccr
    chose; codex = the thread id of an earlier turn (unused on the first)."""
    model, effort = a.eff_model or a.model, a.eff_effort or a.effort
    if a.tool == "claude":
        # stream-json: its init event says which permission mode and tools
        # the agent really got (a model without auto mode falls back to default).
        argv = ["-p", "--output-format", "stream-json", "--verbose", "--session-id" if first else "--resume", session_id]
        if name:
            argv += ["--name", name]
        argv += override_args("claude", model, effort)
        # Like the CLI in auto mode: the account's settings, CLAUDE.md, MCP
        # servers and every tool; the web always allowed, so a model without
        # auto mode still searches. A reviewer loses the file-editing tools only.
        argv += ["--permission-mode", master_mode, "--permission-prompts", "none", "--allowedTools", "WebSearch,WebFetch"]
        if not a.master:
            argv += ["--disallowedTools", ",".join(PANEL_EDIT_TOOLS)]
        return argv
    # codex as its CLI (plugins, apps, rules): the sandbox keeps a reviewer
    # from writing anything (read-only), the master writes in the folder.
    argv = ["exec", "--skip-git-repo-check", "-s", "workspace-write" if a.master else "read-only"]
    if not first:
        argv += ["resume", session_id]
    argv += ["--json"] + override_args("codex", model, effort)
    # the web live, like codex --search (exec has no such flag), unless the
    # account's config.toml chooses its own web_search mode
    if getattr(a, "web_live", True):
        argv += ["-c", "web_search=live"]
    return argv + ["-"]


def parse_agent_output(tool: str, stdout: str, stderr: str, code) -> dict:
    """What one turn produced: claude's stream-json events (init: the
    permission mode and tools it really got; result: the reply) or codex's
    JSONL events."""
    r = {"ok": False, "text": "", "session_id": "", "error": "", "in": 0, "out": 0, "denials": 0,
         "mode": "", "tools": []}
    if tool == "claude":
        j = None
        for l in re.split(r"\r?\n", stdout or ""):
            if not l.lstrip().startswith("{"):
                continue
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            if e.get("type") == "system" and e.get("subtype") == "init":
                r["mode"] = str(e.get("permissionMode") or "")
                t = e.get("tools")
                r["tools"] = [str(x) for x in t if x] if isinstance(t, list) else []
            elif e.get("type") == "result":
                j = e
        if isinstance(j, dict):
            r["session_id"], r["text"] = str(j.get("session_id") or ""), str(j.get("result") or "")
            u = j.get("usage")
            if isinstance(u, dict):
                r["in"] = sum(_as_int(u.get(k)) for k in
                              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                r["out"] = _as_int(u.get("output_tokens"))
            d = j.get("permission_denials")
            r["denials"] = len([x for x in d if x]) if isinstance(d, list) else 0
            sub = str(j.get("subtype") or "")
            if j.get("is_error") or (sub and sub != "success"):
                r["error"] = str(j.get("result") or "") or sub
        else:
            r["error"] = "no JSON result"
    else:
        failed = last_err = ""
        for l in re.split(r"\r?\n", stdout or ""):
            if not l.lstrip().startswith("{"):
                continue
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            t = str(e.get("type") or "")
            if t == "thread.started":
                r["session_id"] = str(e.get("thread_id") or "")
            elif t == "item.completed":
                it = e.get("item")
                if isinstance(it, dict) and it.get("type") == "agent_message":
                    r["text"] = str(it.get("text") or "")
            elif t == "turn.completed":
                u = e.get("usage")
                if isinstance(u, dict):
                    r["in"] += _as_int(u.get("input_tokens"))
                    r["out"] += _as_int(u.get("output_tokens"))
            elif t == "turn.failed":
                err = e.get("error")
                failed = (str(err.get("message") or "") if isinstance(err, dict) else "") or "turn failed"
            elif t == "error":
                last_err = str(e.get("message") or "")
        if failed:
            r["error"] = failed
        elif not r["text"].strip() and last_err:
            r["error"] = last_err
    if not r["error"] and code is not None and code != 0:
        r["error"] = f"exit code {code}"
    if not r["error"] and not r["text"].strip():
        r["error"] = "empty reply"
    if r["error"]:
        tail = next((l.strip() for l in reversed(re.split(r"\r?\n", stderr or "")) if l.strip()), "")
        if tail and tail not in r["error"]:
            r["error"] += f" - {tail}"
    r["ok"] = not r["error"]
    return r


def agent_text(text: str) -> str:
    """Agent text made safe to print (CONTROL_RE)."""
    return CONTROL_RE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))


def codex_child_path() -> str:
    """The PATH for a codex agent: on Windows without the WindowsApps entries.
    codex's sandbox (a restricted token) cannot start programs kept there -
    the Microsoft Store PowerShell above all, which a Store pwsh host adds to
    PATH; without them codex finds a PowerShell it can start (PowerShell 7
    under Program Files, found even off PATH). '' = PATH stays."""
    if os.name != "nt":
        return ""
    parts = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x]
    keep = [x for x in parts if not re.search(r"\\WindowsApps(\\|$)", x, re.I)]
    return "" if len(keep) == len(parts) else os.pathsep.join(keep)


def _read_text(path) -> str:
    try:
        return Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _cmdline(argv: list) -> str:
    """A command line as this system's shell would take it (shown, not run)."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(x) for x in argv)


def _drain_input():
    """Drop the keys typed while agents worked, so they answer nothing."""
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        elif sys.stdin.isatty():
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _tty_echo(on: bool):
    """Unix: turn the terminal's echo on or off (keys typed while agents work
    must not scribble over the status line). Returns the settings before, for
    _tty_restore; None when nothing changed."""
    if os.name == "nt" or not sys.stdin.isatty():
        return None
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        attr = termios.tcgetattr(fd)
        attr[3] = (attr[3] | termios.ECHO) if on else (attr[3] & ~termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, attr)
        return saved
    except Exception:
        return None


def _tty_restore(saved):
    if saved is not None:
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
        except Exception:
            pass


def yes_no(text: str) -> bool:
    """A y/N question; keys typed while agents worked are dropped first.
    Without a terminal the answer is no."""
    if not sys.stdin.isatty():
        print(f"{text} [y/N] n (no terminal to ask)")
        return False
    _drain_input()
    saved = _tty_echo(True)
    try:
        ans = ask(f"{text} [y/N] ")
    finally:
        _tty_restore(saved)
    return bool(ans) and ans.strip().lower() in ("y", "yes")


def start_agent_turn(a, argv: list, prompt: str, folder: str, tmp: str, tag: str) -> dict:
    """Start one agent turn as a child process: prompt on stdin, output in
    files (a file never blocks the way a full pipe does). The child gets its
    own process group, so Ctrl-C reaches ccr only and ccr ends the whole tree;
    a.env: the account's dir (several accounts) and codex's PATH on Windows;
    None = this process's environment."""
    inp, out, err = (Path(tmp) / f"{tag}.{x}.txt" for x in ("in", "out", "err"))
    inp.write_bytes(prompt.encode("utf-8"))
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
          else {"start_new_session": True})
    with open(inp, "rb") as fi, open(out, "wb") as fo, open(err, "wb") as fe:
        p = subprocess.Popen([a.exe] + list(argv), cwd=folder, stdin=fi, stdout=fo, stderr=fe, env=a.env, **kw)
    return {"agent": a, "proc": p, "out": out, "err": err, "started": time.monotonic()}


def stop_agent_turn(t: dict):
    """End a turn's whole process tree."""
    p = t["proc"]
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    try:
        p.wait(5)
    except Exception:
        pass


def panel_status(text: str):
    """The one status line, rewritten in place."""
    w = max(20, shutil.get_terminal_size((120, 24)).columns - 1)
    if len(text) > w:
        text = text[: w - 1] + "…"
    sys.stdout.write(f"\r\033[K{DIM}{text}{RESET}")
    sys.stdout.flush()


def print_panel_message(a, title: str, res: dict):
    head = f"{TOOL_COLOR[a.tool]}── agent {a.index}{' ★' if a.master else ''} · {panel_agent_label(a)} · {title}{RESET}"
    if res["ok"]:
        mark = ""
        if title.startswith("round"):
            c = panel_consensus(res["text"])
            mark = f" · {GREEN}AGREE{RESET}" if c == "AGREE" else f" · {YELLOW}{c or 'no marker'}{RESET}"
        sys.stdout.write(f"\r\033[K{head}{mark}\n{agent_text(res['text']).rstrip()}\n\n")
    else:
        sys.stdout.write(f"\r\033[K{head} · {RED}failed: {agent_text(res['error'])}{RESET}\n\n")
    sys.stdout.flush()


class PanelRun:
    """One MAP run: its folder, agents, time mark (minutes of one turn before
    ccr asks whether to keep waiting; 0 = never) and what happened."""

    def __init__(self, folder: str, agents: list, ask_after: float):
        self.folder, self.agents, self.ask_after = folder, agents, ask_after
        self.asking = None
        self.tmp = tempfile.mkdtemp(prefix=f"ccr-panel-{os.getpid()}-")
        self.stopped, self.round, self.status, self.log, self.running = False, 0, "", [], []
        self.tabs = False


def _poll_keys() -> str:
    """What was typed since the last call, without waiting. On Unix the
    terminal stays in canonical mode, so a key arrives with its Enter."""
    try:
        if os.name == "nt":
            import msvcrt
            s = ""
            while msvcrt.kbhit():
                s += msvcrt.getwch()
            return s
        if not sys.stdin.isatty():
            return ""
        import select
        s = ""
        while select.select([sys.stdin], [], [], 0)[0]:
            chunk = os.read(sys.stdin.fileno(), 1024)
            if not chunk:
                break
            s += chunk.decode("utf-8", errors="replace")
        return s
    except Exception:
        return ""


def run_panel_batch(batch: list, prompts: dict, title: str, pc: PanelRun):
    """Run one batch of turns in parallel (a round, the master alone, or the
    reviewers after it), printing each reply as it lands. A turn past the
    time mark makes ccr ask - Enter keeps waiting, s stops that agent - and
    is never stopped otherwise. Ctrl-C raises KeyboardInterrupt out of here;
    run_panel's cleanup ends the processes."""
    turns = []
    for n, a in enumerate(batch):
        if n:
            # staggered starts: several processes on one config dir would
            # all rewrite it at the same moment
            time.sleep(1.5)
        argv = panel_argv(a, a.turns == 0, a.session_id if a.tool == "claude" else a.thread_id, a.name, a.master_mode)
        try:
            t = start_agent_turn(a, argv, prompts[a.index], pc.folder, pc.tmp, f"a{a.index}-t{a.starts + 1}")
        except OSError as e:
            a.active = False
            print_panel_message(a, title, {"ok": False, "error": f"cannot start {a.exe}: {e}"})
            panel_event(pc, a.index, {"ev": "done", "title": title, "ok": False, "error": f"cannot start {a.exe}: {e}"})
            continue
        # the time mark: ccr asks then, it never stops a turn on its own
        t["ask_at"] = t["started"] + pc.ask_after * 60 if pc.ask_after > 0 else float("inf")
        t["stop"] = False
        turns.append(t)
        pc.running.append(t)
        a.starts += 1
        panel_event(pc, a.index, {"ev": "turn", "turn": a.starts, "title": title, "out": os.path.basename(str(t["out"]))})
    pending = list(turns)
    while pending:
        for t in list(pending):
            a, p = t["agent"], t["proc"]
            done = p.poll() is not None
            if not done and t["stop"]:
                stop_agent_turn(t)
                done = True
            if not done:
                continue
            pending.remove(t)
            pc.running.remove(t)
            if pc.asking is t:
                pc.asking = None
            res = parse_agent_output(a.tool, _read_text(t["out"]), _read_text(t["err"]), None if t["stop"] else p.returncode)
            if t["stop"]:
                res.update(ok=False, error=f"stopped by you after {int((time.monotonic() - t['started']) // 60)} min")
            # What a claude agent really got: a reviewer no file-editing tool;
            # a model without auto mode falls back to default mode - a master
            # then asks for acceptEdits from its next turn (round 1, where it
            # changes nothing, finds this out), a reviewer keeps reads and web.
            note = ""
            if res["ok"] and a.tool == "claude":
                edit = [x for x in res["tools"] if x in PANEL_EDIT_TOOLS]
                if not a.master and edit:
                    res.update(ok=False, error=f"a reviewer got file-editing tools: {', '.join(edit)}")
                elif res["mode"] and res["mode"] != a.master_mode:
                    note = f"claude {a.eff_model or 'default model'} ran in {res['mode']} mode, not {a.master_mode}"
                    if a.master_mode == "auto":
                        if a.master:
                            a.master_mode = "acceptEdits"
                            note += (" (no auto mode for this model): from its next turn it works in acceptEdits - it "
                                     "can change files, not run commands")
                        else:
                            a.master_mode = "default"
                            note += " (no auto mode for this model): it reads and searches the web, no commands"
            # The reply must come from this agent's own session.
            if res["ok"]:
                if a.tool == "claude":
                    if res["session_id"] and res["session_id"] != a.session_id:
                        res.update(ok=False, error=f"answered in another session ({res['session_id']})")
                elif not a.thread_id:
                    if res["session_id"]:
                        a.thread_id = res["session_id"]
                        try:
                            add_codex_index_name(a.root.path, a.thread_id, a.name)
                        except OSError:
                            pass
                    else:
                        res.update(ok=False, error="no thread id in the codex output")
                elif res["session_id"] and res["session_id"] != a.thread_id:
                    res.update(ok=False, error=f"codex opened a new thread ({res['session_id']}) "
                                               f"instead of resuming {a.thread_id}")
            a.turns += 1
            a.done += 1
            a.inp += res["in"]
            a.out += res["out"]
            a.denials += res["denials"]
            pc.log.append({"agent": a, "title": title, "res": res, "note": note})
            if res["ok"]:
                a.last = {"round": pc.round, "text": res["text"], "consensus": panel_consensus(res["text"])}
            else:
                a.active = False
            print_panel_message(a, title, res)
            if note:
                sys.stdout.write(f"{YELLOW}ccr: agent {a.index}: {note}.{RESET}\n\n")
                sys.stdout.flush()
            panel_event(pc, a.index, {"ev": "done", "title": title, "ok": bool(res["ok"]), "error": res["error"], "note": note,
                                      "consensus": panel_consensus(res["text"]) if res["ok"] and title.startswith("round") else "",
                                      "session": a.session_id if a.tool == "claude" else a.thread_id})
        if not pending:
            break
        # A turn past the time mark: ask, with a bell - never stop it on our own.
        if pc.asking is None and pc.ask_after > 0:
            late = next((t for t in turns if t in pending and time.monotonic() >= t["ask_at"]), None)
            if late:
                pc.asking = late
                sys.stdout.write("\a")
        keys = _poll_keys()
        if pc.tabs and "t" in keys.lower():
            # the agents' tabs again: one was closed, or the terminal dropped them
            why = open_panel_tabs(pc.agents, pc.tmp, pc.folder)
            msg = f"tabs: {why}" if why else "the agents' tabs are open"
            sys.stdout.write(f"\r\033[K{YELLOW}ccr: {msg}.{RESET}\n")
            sys.stdout.flush()
        if pc.asking is not None and keys:
            if "s" in keys.lower():
                pc.asking["stop"], pc.asking = True, None
            elif "\n" in keys or "\r" in keys:
                pc.asking["ask_at"], pc.asking = time.monotonic() + pc.ask_after * 60, None
        if pc.asking is not None:
            s = int(time.monotonic() - pc.asking["started"])
            panel_status(f"? agent {pc.asking['agent'].index} has been working for {s // 60}:{s % 60:02d} · Enter: keep "
                         f"waiting (asks again in {pc.ask_after:g} min) · s + Enter: stop it")
        else:
            parts = []
            for t in turns:
                if t in pending:
                    s = int(time.monotonic() - t["started"])
                    parts.append(f"agent {t['agent'].index} {s // 60}:{s % 60:02d}")
            tok = sum(x.inp + x.out for x in pc.agents)
            panel_status(f"{pc.status} · thinking: {', '.join(parts)} · {fmt_tokens(tok)} tokens · {'t + Enter reopens tabs · ' if pc.tabs else ''}Ctrl-C stops")
        time.sleep(0.25)
    pc.asking = None
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def panel_transcript(agents: list, question: str, folder: str, log: list, outcome: str, started) -> str:
    """The whole discussion as markdown: final answer first, then the rounds,
    then the rules agent 1 received."""
    out = [f"# ccr MAP · {panel_short(question, 80)}\n\n",
           f"- date: {started:%Y-%m-%d %H:%M}\n- folder: {folder}\n- outcome: {outcome}\n"]
    for a in agents:
        sid = a.session_id if a.tool == "claude" else a.thread_id
        acct = f", account {a.root.label}" if a.root and a.root.label else ""
        out.append(f"- agent {a.index}{' (master)' if a.master else ''}: {panel_agent_label(a)}{acct}, "
                   f"session {sid if a.turns and sid else '-'}, {fmt_tokens(a.inp)} in / {fmt_tokens(a.out)} out\n")
    out.append(f"\n## Question\n\n{question.strip()}\n\n")
    final = next((e for e in log if e["title"].startswith("final") and e["res"]["ok"]), None)
    if final:
        out.append(f"## {final['title'].title()}\n\n{final['res']['text'].strip()}\n\n")
    cur = ""
    for e in log:
        if e["title"].startswith("final"):
            continue
        if e["title"] != cur:
            cur = e["title"]
            out.append(f"## {cur.title()}\n\n")
        c = (panel_consensus(e["res"]["text"]) or "no marker") if e["res"]["ok"] else "failed"
        out.append(f"### agent {e['agent'].index} · {panel_agent_label(e['agent'])} · {c}\n\n")
        out.append((e["res"]["text"].strip() if e["res"]["ok"] else f"_failed: {e['res']['error']}_") + "\n\n")
        if e.get("note"):
            out.append(f"_ccr: {e['note']}._\n\n")
    out.append(f"## Rules sent to agent 1\n\n```\n{panel_protocol(1, agents, folder, question).strip()}\n```\n")
    return "".join(out)


def save_panel_transcript(md: str, short: str) -> str:
    """Transcripts stay on this PC, next to ccr's state file."""
    d = state_path().parent / "panels"
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[\W_]+", "-", short).strip("-")
    if len(slug) > 40:
        slug = slug[:40].strip("-")
    f = d / f"{datetime.now():%Y%m%d-%H%M%S}-{slug or 'panel'}.md"
    f.write_bytes(md.encode("utf-8"))
    return str(f)


def run_panel(panel: dict, question: str, folder: str, ctx, dry: bool, resume: dict = None, message: str = "") -> bool:
    """Run a panel: check, start the agents round after round, stop at
    consensus (from round 2) or at the round limit, let the master (else the
    first active agent) write the final answer, save the transcript and its
    state, print the sessions. resume: a saved MAP going on (its state), with
    the user's message for the first round after the pause."""
    if not (question or "").strip():
        print("ccr: the MAP needs a question.", file=sys.stderr)
        return False
    if not dry and not os.path.isdir(folder):
        print(f"ccr: folder no longer exists: {folder}", file=sys.stderr)
        return False
    agents = [a.copy() for a in panel["agents"]]
    n = len(agents)
    if not 2 <= n <= PANEL_MAX_AGENTS:
        print(f"ccr: a MAP needs 2 to {PANEL_MAX_AGENTS} agents.", file=sys.stderr)
        return False
    if sum(1 for a in agents if a.master) > 1:
        print("ccr: a MAP has at most one master.", file=sys.stderr)
        return False
    short = panel_short(question)
    for i, a in enumerate(agents, 1):
        root = panel_root(ctx, a)
        a.index, a.root = i, root
        a.eff_model, a.eff_effort = panel_effective(a, configured_model(a.tool, [root.path]))
        a.exe = tool_path(a.tool)   # first: it may add the tool's dir to PATH
        a.multi = bool(ctx.root_var(a.tool))
        a.env = root_env(ctx, a.tool, root.path)
        child_path = codex_child_path() if a.tool == "codex" else ""
        if child_path:
            a.env = dict(a.env or os.environ, PATH=child_path)
        # an agent of a saved MAP goes on in its own session (prev)
        prev = a.prev if isinstance(a.prev, dict) else None
        prev_sid = str(prev.get("session") or "") if prev and a.tool == "claude" else ""
        prev_tid = str(prev.get("thread") or "") if prev and a.tool == "codex" else ""
        a.session_id = prev_sid or (str(uuid.uuid4()) if a.tool == "claude" else "")
        a.thread_id = prev_tid
        a.turns = max(1, _as_int(prev.get("turns"))) if (prev_sid or prev_tid) else 0
        last = prev.get("last") if prev else None
        a.last = ({"round": _as_int(last.get("round")), "text": str(last.get("text") or ""),
                   "consensus": str(last.get("consensus") or "")} if isinstance(last, dict) else None)
        a.inp, a.out = (_as_int(prev.get("in")), _as_int(prev.get("out"))) if prev else (0, 0)
        a.prev_failed = str(prev.get("failed") or "") if prev else ""
        a.name = f"MAP {'★' if a.master else ''}{i}/{n} · {short}"
        a.master_mode = panel_master_mode(root.path, a.master) if a.tool == "claude" else ""
        a.web_live = a.tool == "codex" and not codex_sets_web_search(root.path)
    for t in dict.fromkeys(a.tool for a in agents if not a.exe):
        print(f"ccr: {t} is not installed on this PC. Fix: ccr --install {t}   (or Ctrl-T in the picker)",
              file=sys.stderr)
        return False
    for a in agents:
        miss = account_missing(a.tool, a.root) if a.multi else ""
        if miss:
            print(miss, file=sys.stderr)
            return False
    master = next((a for a in agents if a.master), None)

    if dry:
        how = f"go on from round {_as_int(resume.get('round')) + 1}" if resume else "start"
        print(f"dry-run: a MAP of {n} agents in {folder} would {how} like this (the rules and the question go on stdin):")
        for a in agents:
            pre = f"{ROOT_VAR[a.tool]}={shlex.quote(a.root.path)} " if a.multi else ""
            sid = a.thread_id if a.tool == "codex" and a.turns else a.session_id
            argv = panel_argv(a, not a.turns, sid, a.name, a.master_mode)
            print(f"  agent {a.index}{' (master)' if a.master else ''}: {pre}{_cmdline([a.exe] + argv)}")
        if any(a.tool == "codex" for a in agents) and codex_child_path():
            print("  codex agents: PATH without its WindowsApps entries (codex's sandbox cannot start the Store PowerShell)")
        tab_ok = bool(os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "Apple_Terminal"))
        print("  tabs: " + ("off" if panel.get("tabs") is False else "one terminal tab per agent, live" if tab_ok
                           else "not available here (iTerm2, Terminal or tmux only)"))
        return True

    git = shutil.which("git")
    if master and git:
        st = subprocess.run([git, "-C", folder, "status", "--porcelain"], capture_output=True,
                            encoding="utf-8", errors="replace")
        dirty = [l for l in st.stdout.splitlines() if l.strip()] if st.returncode == 0 else []
        if dirty:
            print(f"{YELLOW}ccr: {folder} has {len(dirty)} uncommitted change(s), and the master will change "
                  f"files there.{RESET}")
            if not yes_no("ccr: start the MAP anyway?"):
                print("ccr: cancelled.")
                return False

    # After this many minutes of one turn ccr asks whether to keep waiting
    # (ccr.json "panel": { "askAfterMinutes": 30 }; 0 = never asks).
    pcfg = (load_config() or {}).get("panel")
    pcfg = pcfg if isinstance(pcfg, dict) else {}
    ask_after = 20
    try:
        if pcfg.get("askAfterMinutes") is not None:
            v = pcfg["askAfterMinutes"]
            ask_after = max(0, 0 if v == "" else int(round(float(v))))
        elif _as_int(pcfg.get("turnTimeoutMinutes")) > 0:
            ask_after = _as_int(pcfg.get("turnTimeoutMinutes"))
    except (TypeError, ValueError, OverflowError):
        pass   # not a number: the default
    pc = PanelRun(folder, agents, ask_after)
    # a MAP that goes on: its rounds so far open the transcript (its old final
    # answer or summary is superseded)
    if resume:
        for e in resume.get("log") or []:
            if str(e.get("title") or "").startswith("final"):
                continue
            ag = next((x for x in agents if x.index == _as_int(e.get("agent"))), None)
            if ag:
                pc.log.append({"agent": ag, "title": str(e.get("title") or ""), "note": str(e.get("note") or ""),
                               "res": {"ok": bool(e.get("ok")), "text": str(e.get("text") or ""),
                                       "error": str(e.get("error") or "")}})
    # The live records of earlier runs stay for their tabs; a day later they go.
    for old in Path(tempfile.gettempdir()).glob("ccr-panel-*"):
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    # One tab per agent: its header goes into the live record first, then the
    # tabs open and follow the record.
    tabs_note = ""
    if panel.get("tabs") is not False:
        pc.tabs = True
        for a in agents:
            panel_event(pc, a.index, {"ev": "agent", "index": a.index, "n": n, "tool": a.tool,
                                      "label": panel_agent_label(a),
                                      "role": "master, can change files" if a.master else "reviewer, no file changes",
                                      "folder": folder, "question": panel_short(question, 120), "pid": os.getpid(),
                                      "session": a.session_id})
        why = open_panel_tabs(agents, pc.tmp, folder)
        if not why:
            tabs_note = "tabs: one per agent, live - Enter there at the end opens its session; t + Enter opens them again"
        elif why.startswith("not available"):
            pc.tabs = False
            tabs_note = f"tabs: {why}"
        else:
            tabs_note = f"tabs: {why} - t + Enter tries again"
    try:
        sys.stdout.reconfigure(errors="replace")   # a reply in any script prints on any console
    except (AttributeError, ValueError):
        pass
    # Keys typed while agents work are not echoed (Unix); SIGHUP / SIGTERM
    # (the terminal closed, a kill) end the agents the way Ctrl-C does.
    saved_tty = _tty_echo(False)

    def _stop(signum, frame):
        raise KeyboardInterrupt

    old_handlers = {}
    for name in ("SIGHUP", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                old_handlers[getattr(signal, name)] = signal.signal(getattr(signal, name), _stop)
            except (ValueError, OSError):
                pass
    agreed = False
    started = datetime.now()
    max_rounds = max(2, int(panel["rounds"]))
    k = _as_int(resume.get("round")) if resume else 0
    resume_k = k + 1

    def mk(a, items):
        """One agent's prompt for a later round: the other agents' latest
        messages; the pause and the user's message on the first round after
        it; the rules too for an agent that has no session yet."""
        m = panel_round_message(k, items, bool(a.last and not a.last["consensus"]))
        if resume and k == resume_k:
            m = panel_resume_note(message, a.prev_failed) + m
        if not a.turns:
            m = panel_protocol(a.index, agents, folder, question) + "\n\n" + m
        return m
    try:
        sys.stdout.write(f"\033[?25l\n{BOLD}ccr MAP{RESET}{' with a master' if master else ''} · {folder}\n")
        for a in agents:
            role = f"{YELLOW}★ master, can change files{RESET}" if a.master else f"{DIM}reviewer, no file changes{RESET}"
            acct = f"  {MAGENTA}{acct_label(a.root.label, a.root.default)}{RESET}" if a.multi else ""
            sys.stdout.write(f"  {TOOL_COLOR[a.tool]}agent {a.index}{RESET}  {panel_agent_label(a)}{acct}  {role}\n")
        if tabs_note:
            sys.stdout.write(f"{DIM}{tabs_note}{RESET}\n")
        if resume:
            sys.stdout.write(f"{DIM}goes on from round {resume_k} (up to {max_rounds})"
                             f"{', with your message' if (message or '').strip() else ''}{RESET}\n")
        sys.stdout.write(f"{DIM}question: {panel_short(question, 120)}{RESET}\n\n")
        sys.stdout.flush()
        while True:
            k += 1
            pc.round = k
            active = [a for a in agents if a.active]
            agree_n = sum(1 for a in active if a.last and a.last["consensus"] == "AGREE")
            pc.status = f"round {k}/{max_rounds}" + (f" · {agree_n}/{len(active)} agreed in round {k - 1}" if k > 1 else "")
            if k == 1:
                prompts = {a.index: panel_protocol(a.index, agents, folder, question) for a in active}
                run_panel_batch(active, prompts, f"round {k}", pc)
            elif master and master.active:
                # the master first, on the reviewers' previous messages ...
                items = [panel_item(a) for a in active if a is not master]
                run_panel_batch([master], {master.index: mk(master, items)}, f"round {k}", pc)
                # ... then the reviewers, on its fresh message and each other's previous ones
                reviewers = [a for a in agents if a.active and not a.master]
                prompts = {a.index: mk(a, [panel_item(x) for x in agents if x.active and x is not a]) for a in reviewers}
                if reviewers:
                    run_panel_batch(reviewers, prompts, f"round {k}", pc)
            else:
                snap = {a.index: panel_item(a) for a in active}
                prompts = {a.index: mk(a, [snap[x.index] for x in active if x is not a]) for a in active}
                run_panel_batch(active, prompts, f"round {k}", pc)
            active = [a for a in agents if a.active]
            if k == 1 and 2 <= len(active) < n:
                if not yes_no(f"ccr: {n - len(active)} agent(s) failed in round 1. Go on without them?"):
                    break
            if len(active) < 2:
                sys.stdout.write(f"{YELLOW}ccr: fewer than two agents left - the MAP stops.{RESET}\n")
                break
            if k >= 2 and all(a.last and a.last["round"] == k and a.last["consensus"] == "AGREE" for a in active):
                agreed = True
                break
            if k >= max_rounds:
                if yes_no(f"ccr: no consensus after {k} rounds. Go on for 2 more rounds?"):
                    max_rounds += 2
                else:
                    break
        cons = master if master and master.active else next((a for a in agents if a.active), None)
        if cons:
            items = [panel_item(a) for a in agents if a.active and a is not cons]
            pc.status = "consensus: writing the final answer" if agreed else "no consensus: writing the summary"
            run_panel_batch([cons], {cons.index: panel_final_message(agreed, items)},
                            "final answer" if agreed else "final summary", pc)
    except KeyboardInterrupt:
        pc.stopped = True
    finally:
        # A second Ctrl-C must not leave agents running.
        prev_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for t in list(pc.running):
                stop_agent_turn(t)
        finally:
            signal.signal(signal.SIGINT, prev_int)
        # the tabs' last word: the outcome, and how to open each session there
        end_txt = ("stopped with Ctrl-C" if pc.stopped else f"consensus in round {pc.round}" if agreed
                   else f"no consensus after {pc.round} round(s)")
        for a in agents:
            sid = (a.session_id if a.turns else "") if a.tool == "claude" else a.thread_id
            panel_event(pc, a.index, {"ev": "end", "outcome": end_txt, "session": sid, "exe": a.exe,
                                      "resume": ["--resume", sid] if a.tool == "claude" else ["resume", sid],
                                      "var": ROOT_VAR[a.tool], "root": a.root.path if a.root else "",
                                      "path": (a.env or {}).get("PATH", "") if a.tool == "codex" and codex_child_path() else "",
                                      "folder": folder})
        for sig, h in old_handlers.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
        _tty_restore(saved_tty)
        try:
            sys.stdout.write("\r\033[K\033[?25h")
            sys.stdout.flush()
        except OSError:
            pass
        if not pc.tabs:
            shutil.rmtree(pc.tmp, ignore_errors=True)

    mins = round((datetime.now() - started).total_seconds() / 60)
    outcome = ("stopped with Ctrl-C" if pc.stopped else f"consensus in round {pc.round}" if agreed
               else f"no consensus after {pc.round} round(s)")
    saved = None
    if pc.log:
        try:
            saved = save_panel_transcript(panel_transcript(agents, question, folder, pc.log, outcome, started), short)
            save_panel_state(saved, agents, question, folder, pc.log, pc.round, max_rounds, outcome, agreed, started)
        except OSError as e:
            print(f"ccr: transcript not saved: {e}", file=sys.stderr)
    print()
    print(f"{GREEN if agreed else YELLOW}ccr: MAP finished - {outcome}, {mins} min.{RESET}")
    if saved:
        print(f"  transcript: {saved}")
    if pc.tabs:
        print("  tabs: Enter in an agent's tab opens its session there")
    if not agreed and saved:
        print("  to go on with the same agents: ccr --panel --continue (or r at the MAP question)")
    print('  sessions, in the picker as "MAP i/n" - resume one to go on alone:')
    for a in agents:
        sid = a.session_id if a.tool == "claude" else a.thread_id
        if not a.turns:
            note = "stopped before its first reply" if a.starts else "not started"
        else:
            note = (f"{fmt_tokens(a.inp)} in / {fmt_tokens(a.out)} out"
                    + (f", {a.denials} tool call(s) denied" if a.denials else "")
                    + (", stopped" if a.starts > a.done else ", failed" if a.starts and not a.active else ""))
        print(f"    agent {a.index}{'★' if a.master else ' '} {panel_agent_label(a):<36} "
              f"{sid if a.turns and sid else '-'}  {note}")
    if master and git:
        st = subprocess.run([git, "-C", folder, "status", "--short"], capture_output=True,
                            encoding="utf-8", errors="replace")
        if st.returncode == 0:
            print("  git status of the folder after the master's work:")
            lines = [l for l in st.stdout.splitlines() if l.strip()]
            for l in lines or ["(no changes)"]:
                print(f"    {l}")
    return True


# ----------------------------------------------------------------------------
# MAP state: saved next to each transcript, so a MAP can go on later
# (ccr --panel --continue, or r at the question)
# ----------------------------------------------------------------------------
def save_panel_state(md_file: str, agents: list, question: str, folder: str, log: list, rnd: int, rounds: int,
                     outcome: str, agreed: bool, started):
    """The state of a MAP as JSON next to its transcript (same name, .json):
    the question, the folder, the rounds, each agent with its session and
    last message, and the whole log."""
    def last_entry(a):
        mine = [e for e in log if e["agent"] is a and not str(e["title"]).startswith("final")]
        return mine[-1] if mine else None
    state = {
        "version": 1, "question": question, "folder": folder, "started": f"{started:%Y-%m-%d %H:%M}", "round": rnd,
        "rounds": rounds, "outcome": outcome, "agreed": bool(agreed),
        "agents": [{"index": a.index, "tool": a.tool, "model": a.eff_model or "", "effort": a.eff_effort or "",
                    "account": a.root.label if a.root and a.root.label else "", "master": bool(a.master),
                    "session": a.session_id if a.tool == "claude" and a.turns else "", "thread": a.thread_id or "",
                    "turns": a.turns, "in": a.inp, "out": a.out,
                    "failed": (last_entry(a)["res"]["error"] if last_entry(a) and not last_entry(a)["res"]["ok"] else ""),
                    "last": ({"round": a.last["round"], "text": a.last["text"], "consensus": a.last["consensus"]}
                             if a.last else None)} for a in agents],
        "log": [{"agent": e["agent"].index, "title": e["title"], "ok": bool(e["res"]["ok"]), "text": e["res"].get("text") or "",
                 "error": e["res"].get("error") or "", "note": e.get("note") or ""} for e in log],
    }
    Path(os.path.splitext(md_file)[0] + ".json").write_bytes(json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))


def token_text_value(text: str) -> int:
    """"22.5M" -> 22500000, "104k" -> 104000, "999" -> 999 (the transcript's token counts)."""
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([kM]?)$", text or "")
    if not m:
        return 0
    return int(round(float(m.group(1)) * {"k": 1e3, "M": 1e6}.get(m.group(2), 1)))


def parse_panel_transcript(md: str) -> dict:
    """A MAP from before v0.70 has no .json: its transcript holds the same
    state - the agents with their sessions (header), the question, every
    round's messages. The final answer / summary is left out: going on
    supersedes it."""
    lines = re.split(r"\r?\n", md or "")
    st = {"version": 0, "question": "", "folder": "", "started": "", "round": 0, "rounds": 0, "outcome": "",
          "agreed": False, "agents": [], "log": []}
    i = 0
    head_re = re.compile(r"^- agent (\d+)( \(master\))?: (.+?)(?:, account ([^,]+))?, session (\S+), (\S+) in / (\S+) out$")
    while i < len(lines):
        l = lines[i]
        if l.startswith("## "):
            break
        m = re.match(r"^- (date|folder|outcome): (.+)$", l)
        if m:
            st[{"date": "started"}.get(m.group(1), m.group(1))] = m.group(2).strip()
        m = head_re.match(l)
        if m:
            words = m.group(3).split(" ")
            rest = words[1:]
            model = effort = ""
            if " ".join(rest).startswith("its default model"):
                effort = rest[3] if len(rest) > 3 else ""
            else:
                model = rest[0] if rest else ""
                effort = rest[1] if len(rest) > 1 else ""
            sid = "" if m.group(5) == "-" else m.group(5)
            st["agents"].append({"index": int(m.group(1)), "tool": words[0], "model": model, "effort": effort,
                                 "account": (m.group(4) or "").strip(), "master": bool(m.group(2)),
                                 "session": sid if words[0] == "claude" else "", "thread": sid if words[0] == "codex" else "",
                                 "turns": 0, "in": token_text_value(m.group(6)), "out": token_text_value(m.group(7)),
                                 "failed": "", "last": None})
        i += 1
    stop = re.compile(r"^## (Final Answer|Final Summary|Round \d+|Rules sent to agent 1)$")
    entry_re = re.compile(r"^### agent (\d+) · .+ · (AGREE|CONTINUE|no marker|failed)$")
    rnd, cur, in_final, in_question, qlines = 0, None, False, False, []
    log = st["log"]

    def flush(c):
        if not c:
            return
        t = "\n".join(c["lines"]).strip()
        note = ""
        nm = re.search(r"\n*_ccr: (.*)\._\s*$", t, re.S)
        if nm:
            note, t = nm.group(1), t[:nm.start()].strip()
        ok = c["status"] != "failed"
        err = ""
        if not ok:
            em = re.match(r"^_failed: (.*)_$", t, re.S)
            err, t = (em.group(1) if em else t), ""
        log.append({"agent": c["agent"], "title": f"round {c['round']}", "ok": ok, "text": t, "error": err, "note": note})
    while i < len(lines):
        l = lines[i]
        i += 1
        if l == "## Question":
            in_question = True
            continue
        if in_question:
            if stop.match(l):
                in_question = False
            else:
                qlines.append(l)
                continue
        if re.match(r"^## (Final Answer|Final Summary)$", l):
            flush(cur)
            cur, in_final = None, True
            continue
        m = re.match(r"^## Round (\d+)$", l)
        if m:
            flush(cur)
            cur, in_final, rnd = None, False, int(m.group(1))
            continue
        if l == "## Rules sent to agent 1":
            flush(cur)
            cur = None
            break
        if in_final:
            continue
        m = entry_re.match(l)
        if rnd and m:
            flush(cur)
            cur = {"agent": int(m.group(1)), "status": m.group(2), "round": rnd, "lines": []}
            continue
        if cur:
            cur["lines"].append(l)
    flush(cur)
    st["question"] = "\n".join(qlines).strip()
    st["round"] = st["rounds"] = rnd
    for a in st["agents"]:
        mine = [e for e in log if e["agent"] == a["index"]]
        a["turns"] = len(mine)
        ok_e = [e for e in mine if e["ok"]]
        if ok_e:
            e = ok_e[-1]
            a["last"] = {"round": _as_int(re.sub(r"\D", "", e["title"])), "text": e["text"], "consensus": panel_consensus(e["text"])}
        if mine and not mine[-1]["ok"]:
            a["failed"] = mine[-1]["error"]
        if (a["tool"] == "claude" and not a["session"]) or (a["tool"] == "codex" and not a["thread"]):
            a["turns"] = 0
    return st


def panel_state(md_file: str) -> dict:
    """A saved MAP: its .json, or - before v0.70 - its transcript."""
    js = os.path.splitext(md_file)[0] + ".json"
    if os.path.isfile(js):
        return json.loads(Path(js).read_bytes().decode("utf-8"))
    return parse_panel_transcript(Path(md_file).read_bytes().decode("utf-8", errors="replace"))


def panel_runs() -> list:
    """The saved MAPs of this PC, newest first: file, date, outcome, question."""
    d = state_path().parent / "panels"
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        try:
            head = f.read_bytes().decode("utf-8", errors="replace").split("\n")[:12]
        except OSError:
            continue
        head = [h.rstrip("\r") for h in head]

        def get(k):
            return next((h[len(f"- {k}: "):] for h in head if h.startswith(f"- {k}: ")), "")
        title = next((h for h in head if h.startswith("# ccr ")), "")
        title = re.sub(r"^# ccr (MAP|panel) · ", "", title)
        agents = " + ".join(re.sub(r",.*$", "", re.sub(r"^- agent \d+( \(master\))?: ", "", h))
                            for h in head if re.match(r"^- agent \d", h))
        out.append({"file": str(f), "date": get("date"), "outcome": get("outcome"), "question": title, "agents": agents})
    return out


def choose_panel_run():
    """The saved MAPs as an fzf list; Enter picks one."""
    runs = panel_runs()
    if not runs:
        print(f"{YELLOW}ccr: no saved MAP on this PC yet{RESET}")
        pause("(Enter to go back)")
        return None
    rows = [f"{i}\t{r['date']:<16}  {r['outcome']:<28}  {r['question']}\t{r['agents']}" for i, r in enumerate(runs)]
    res = run_fzf(rows, f"{BOLD}MAP · continue a MAP{RESET}  {DIM}the same agents, on their own sessions, from the next "
                        f"round{RESET}\n" + hint(("Enter", "choose"), ("Esc", "back")),
                  multi=False, prompt="MAP> ")
    if not res or not res[1]:
        return None
    return runs[int(res[1][0])]


def panel_from_state(state: dict) -> dict:
    """The panel page's input for going on: the MAP's own agents - their
    tool, account and role fixed, their sessions kept (prev) - and a round
    limit past the rounds already run."""
    agents = []
    for x in sorted(state.get("agents") or [], key=lambda x: _as_int(x.get("index"))):
        a = PanelAgent(str(x.get("tool") or "claude"), str(x.get("model") or ""), str(x.get("effort") or ""),
                       str(x.get("account") or ""), bool(x.get("master")))
        a.prev = x
        agents.append(a)
    rnd = _as_int(state.get("round"))
    return {"agents": agents, "rounds": min(20, max(_as_int(state.get("rounds")), rnd + 2)), "tabs": True, "from": rnd + 1}


def panel_resume_note(message: str, failed: str) -> str:
    """What an agent reads first when a MAP goes on: the pause, its own turn
    that did not finish, and the user's message."""
    s = "The panel goes on after a pause.\n"
    if failed:
        s += f"Your previous turn did not finish ({failed}): pick up from where you were.\n"
    if (message or "").strip():
        s += f"Message from the user to every agent:\n{message.strip()}\n"
    return s + "\n"


def continue_panel(dry: bool, ctx) -> bool:
    """--panel --continue / r at the MAP question: pick a saved MAP, write a
    message to its agents if you like - the answers to their questions -
    set the round limit; the same agents go on in their own sessions."""
    while True:
        run = choose_panel_run()
        if not run:
            return False
        try:
            state = panel_state(run["file"])
        except (OSError, ValueError):
            state = None
        if not state or not state.get("agents"):
            print(f"{YELLOW}ccr: cannot read that MAP ({run['file']}){RESET}")
            pause("(Enter to go back)")
            continue
        folder = str(state.get("folder") or "")
        if not os.path.isdir(folder):
            print(f"{YELLOW}ccr: its folder no longer exists: {folder}{RESET}")
            pause("(Enter to go back)")
            continue
        panel = panel_from_state(state)
        while True:
            msg = read_panel_question(panel, folder, "", cont=state)
            if msg is None:
                break
            choice = panel_page(folder, ctx, panel, str(state.get("question") or ""), cont=True)
            if choice:
                return run_panel(choice, str(state.get("question") or ""), folder, ctx, dry, resume=state, message=msg)


# ----------------------------------------------------------------------------
# MAP tabs: one terminal tab per agent shows live what it does
# (tmux windows, iTerm2 tabs, Terminal.app windows)
# ----------------------------------------------------------------------------
def panel_event(pc, index: int, ev: dict):
    """One line of an agent's live record, agent-<i>.jsonl in the run's
    folder: its header, the start and end of each turn, the end of the MAP.
    The agent's tab follows it. Nothing is written when the run has no tabs."""
    if not pc.tabs:
        return
    try:
        with open(os.path.join(pc.tmp, f"agent-{index}.jsonl"), "ab") as f:
            f.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def read_new_lines(path: str, cur: dict) -> list:
    """The complete lines appended to a file since the last call; cur keeps
    the byte offset and a partial last line. Another process may be writing."""
    cur.setdefault("off", 0)
    cur.setdefault("rest", b"")
    try:
        with open(path, "rb") as f:
            f.seek(cur["off"])
            data = f.read()
    except OSError:
        return []
    cur["off"] += len(data)
    buf = cur["rest"] + data
    last = buf.rfind(b"\n")
    if last < 0:
        cur["rest"] = buf
        return []
    cur["rest"] = buf[last + 1:]
    return [l for l in re.split(r"\r?\n", buf[:last + 1].decode("utf-8", errors="replace")) if l]


def one_line(text, max_: int = 160) -> str:
    """The first non-empty line of a text, cut to max_ characters."""
    l = next((x for x in re.split(r"\r?\n", str(text or "")) if x.strip()), "").strip()
    if len(l) > max_:
        l = l[: max_ - 1] + "…"
    return l


def shell_inner(command: str) -> str:
    """The command inside a shell wrapper (codex runs `pwsh -Command '...'`,
    `bash -lc '...'`), else the command as it is."""
    m = re.search(r"\s-(?:Command|c|lc)\s+'(.*)'\s*$", command or "", re.S)
    if m:
        return m.group(1).replace("''", "'")
    m = re.search(r'\s-(?:Command|c|lc)\s+"(.*)"\s*$', command or "", re.S)
    if m:
        return m.group(1)
    return command or ""


def tool_use_text(name: str, inp) -> str:
    """A claude tool call in one line: the tool and what it works on."""
    i = inp if isinstance(inp, dict) else {}

    def s(k):
        v = i.get(k)
        return "" if v is None else str(v)
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        arg = s("file_path")
    elif name == "NotebookEdit":
        arg = s("notebook_path")
    elif name in ("Grep", "Glob"):
        arg = (f'"{s("pattern")}"' if name == "Grep" else s("pattern")) + (f" in {s('path')}" if i.get("path") else "")
    elif name in ("Bash", "PowerShell"):
        arg = s("command")
    elif name == "WebFetch":
        arg = s("url")
    elif name == "WebSearch":
        arg = s("query")
    else:
        arg = next((v for v in i.values() if isinstance(v, str) and v), "")
    return f"{name}  {one_line(arg, 120)}".rstrip()


def format_panel_event(tool: str, line: str, state: dict = None) -> list:
    """One line of an agent's output as its tab shows it: (style, text)
    pairs, style = text (what the agent writes), tool (what it reads, runs,
    changes), dim (model, reasoning) or err. state remembers the codex
    commands shown when they started. Pure: the tests feed it recorded lines."""
    state = {} if state is None else state
    out = []

    def emit(st, tx):
        t = agent_text(str(tx if tx is not None else "")).rstrip()
        if t:
            out.append((st, t))
    try:
        e = json.loads(line)
    except ValueError:
        return out
    if not isinstance(e, dict):
        return out
    if tool == "claude":
        t = str(e.get("type") or "")
        if t == "system" and e.get("subtype") == "init":
            tools = [x for x in (e.get("tools") or []) if x] if isinstance(e.get("tools"), list) else []
            tt = ", ".join(str(x) for x in tools) if len(tools) <= 6 else f"{len(tools)} tools"
            emit("dim", f"model {e.get('model') or ''} · {e.get('permissionMode') or ''} · {tt}")
        elif t in ("assistant", "user"):
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            for c in content:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if t == "assistant" and ct == "text":
                    emit("text", c.get("text"))
                elif t == "assistant" and ct == "tool_use":
                    emit("tool", f"▸ {tool_use_text(str(c.get('name') or ''), c.get('input'))}")
                elif t == "assistant" and ct == "thinking":
                    if str(c.get("thinking") or "").strip():
                        emit("dim", f"… {one_line(c.get('thinking'), 160)}")
                elif t == "user" and ct == "tool_result" and c.get("is_error"):
                    cc = c.get("content")
                    txt = cc if isinstance(cc, str) else "\n".join(str(x.get("text") or "") for x in (cc or [])
                                                                if isinstance(x, dict))
                    emit("err", f"  ✗ {one_line(txt, 160)}")
        return out
    t = str(e.get("type") or "")
    it = e.get("item") if isinstance(e.get("item"), dict) else {}
    iid = str(it.get("id") or "")
    if t == "item.started" and it.get("type") == "command_execution":
        if iid:
            state[iid] = True
        emit("tool", f"▸ $ {one_line(shell_inner(str(it.get('command') or '')), 140)}")
    elif t == "item.completed":
        k = it.get("type")
        if k == "command_execution":
            if not (iid and iid in state):
                emit("tool", f"▸ $ {one_line(shell_inner(str(it.get('command') or '')), 140)}")
            code = "" if it.get("exit_code") is None else str(it.get("exit_code"))
            if (code not in ("", "0")) or it.get("status") == "failed":
                emit("err", f"  ✗ exit {code or '?'}: {one_line(it.get('aggregated_output'), 150)}")
        elif k == "reasoning":
            if str(it.get("text") or "").strip():
                emit("dim", f"… {one_line(it.get('text'), 160)}")
        elif k == "agent_message":
            emit("text", it.get("text"))
        elif k == "file_change":
            paths = [str(c.get("path")) for c in (it.get("changes") or []) if isinstance(c, dict) and c.get("path")]
            if paths:
                emit("tool", "✎ " + ", ".join(paths[:3]) + (f" +{len(paths) - 3}" if len(paths) > 3 else ""))
        elif k == "mcp_tool_call":
            emit("tool", f"▸ {it.get('server') or ''}.{it.get('tool') or ''}")
        elif k == "web_search":
            emit("tool", f"▸ web search: {one_line(it.get('query'), 120)}")
        elif k == "error":
            emit("err", f"✗ {one_line(it.get('message'), 160)}")
    elif t == "turn.failed":
        err = e.get("error") if isinstance(e.get("error"), dict) else {}
        emit("err", f"✗ {one_line(err.get('message') or 'turn failed', 160)}")
    elif t == "error":
        emit("err", f"✗ {one_line(e.get('message') or 'error', 160)}")
    return out


def panel_tab_title(a, n: int) -> str:
    return f"MAP {'★' if a.master else ''}{a.index}/{n} · {panel_agent_label(a)}".replace(";", "")


def open_panel_tabs(agents: list, tmp: str, folder: str) -> str:
    """One terminal surface per agent (a background tmux window, an iTerm2
    tab, a Terminal.app window), each running this script's viewer of that
    agent. '' = they are open, else why not ('not available here ...' when
    there is no backend: open_tab's rule)."""
    me = self_path()
    for a in agents:
        cmd = (f"{shlex.quote(sys.executable)} {shlex.quote(me)} --panel-watch {shlex.quote(tmp)} "
               f"--panel-agent {a.index}")
        if not open_tab(folder, cmd, False, False, detached=True):
            return "not available here (iTerm2, Terminal or tmux only)"
    # the terminal returns before the tabs exist and may drop a request
    # without a word: wait for the viewers (their command line names the run)
    leaf, want, seen = os.path.basename(tmp.rstrip("/\\")), len(agents), 0
    end = time.monotonic() + 5
    while seen < want and time.monotonic() < end:
        time.sleep(0.25)
        seen = _panel_viewers(leaf)
        if seen < 0:
            return ""   # no process list here: trust the terminal
    return "" if seen >= want else f"the terminal opened {seen} of {want}"


def _panel_viewers(leaf: str) -> int:
    """How many MAP viewers of this run are running; -1 = cannot tell."""
    try:
        out = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return -1
    return sum(1 for line in out.splitlines() if "--panel-watch" in line and leaf in line)


def watch_panel_agent(d: str, index: int):
    """An agent's tab: what the agent does, live - its reads, searches,
    commands and changes, its messages, turn after turn - from the run's live
    record. At the end Enter opens the agent's own session in this tab
    (claude --resume / codex resume, under its account)."""
    colors = {"text": "", "tool": CYAN, "dim": DIM, "err": RED}
    manifest = os.path.join(d, f"agent-{index}.jsonl")
    mcur, turn, state = {}, None, {}
    hdr, end, sid, tool, tc = None, None, "", "", ""
    quiet = 0

    def show(items):
        for st, tx in items:
            c = colors.get(st, "")
            print(f"{c}{tx}{RESET if c else ''}")

    def flush(tr):
        if tr:
            for l in read_new_lines(tr["path"], tr):
                show(format_panel_event(tool, l, state))
    while end is None:
        news = 0
        for l in read_new_lines(manifest, mcur):
            news += 1
            try:
                e = json.loads(l)
            except ValueError:
                continue
            ev = e.get("ev")
            if ev == "agent":
                hdr, tool, sid = e, str(e.get("tool") or ""), str(e.get("session") or "")
                tc = TOOL_COLOR.get(tool, "")
                sys.stdout.write(f"\033]0;MAP {e.get('index')}/{e.get('n')} · {e.get('label')}\007")
                print(f"{tc}{BOLD}agent {e.get('index')}/{e.get('n')} · {e.get('label')} · {e.get('role')}{RESET}")
                print(f"{DIM}folder: {e.get('folder')}\nquestion: {e.get('question')}{RESET}")
            elif ev == "turn":
                flush(turn)
                turn = {"path": os.path.join(d, str(e.get("out") or ""))}
                print(f"\n{tc}── {e.get('title')} ──{RESET}")
            elif ev == "done":
                flush(turn)
                turn = None
                if e.get("session"):
                    sid = str(e["session"])
                if e.get("ok"):
                    cons = str(e.get("consensus") or "")
                    c = GREEN if cons == "AGREE" else YELLOW if cons == "CONTINUE" else DIM
                    print(f"{c}✓ {e.get('title')}{f' · {cons}' if cons else ''}{RESET}")
                else:
                    print(f"{RED}✗ {e.get('title')} failed: {agent_text(str(e.get('error') or ''))}{RESET}")
                if e.get("note"):
                    print(f"{YELLOW}ccr: {e['note']}.{RESET}")
            elif ev == "end":
                flush(turn)
                turn = None
                end = e
        if end is not None:
            break
        if turn:
            lines = read_new_lines(turn["path"], turn)
            news += len(lines)
            for l in lines:
                show(format_panel_event(tool, l, state))
        quiet = 0 if news else quiet + 1
        if hdr and quiet > 25 and not pid_alive(_as_int(hdr.get("pid"))):
            end = {"outcome": "ccr is no longer running"}
            break
        time.sleep(0.2)
    print(f"\n{BOLD}── MAP finished: {end.get('outcome') or 'done'} ──{RESET}")
    sid = str(end.get("session") or sid)
    if sid and end.get("exe"):
        resume = [str(x) for x in (end.get("resume") or [])]
        if ask(f"Enter: open this agent's session here ({tool} {' '.join(resume)}) · Ctrl-C: close ") is None:
            return
        env = dict(os.environ)
        if end.get("var") and end.get("root"):
            env[str(end["var"])] = str(end["root"])
        if end.get("path"):
            env["PATH"] = str(end["path"])
        if end.get("folder") and os.path.isdir(str(end["folder"])):
            os.chdir(str(end["folder"]))
        try:
            os.execvpe(str(end["exe"]), [str(end["exe"])] + resume, env)
        except OSError as ex:
            print(f"ccr: cannot start {end['exe']}: {ex}", file=sys.stderr)
    else:
        ask("no session to open · Enter: close ")


# The panel page (Ctrl-N -> panel) as fzf menus: the PowerShell page's
# fields, one menu per step (fzf has no editable fields).
def panel_page(folder: str, ctx, initial: dict = None, question: str = "", cont: bool = False):
    """The agents as an fzf list, the last step before the start: Enter on an
    agent opens its settings (tool, model, effort, account, master, remove),
    Del removes it, "+ add agent" adds a copy of the last one, "rounds" sets
    the round limit, "start" starts. The preview shows the agent's first
    command, so the permissions show. cont: a saved MAP going on - its agents
    keep their tool, account and role (their sessions go on); the model,
    effort, rounds and tabs can change. Returns {"agents", "rounds", "tabs"},
    or None on Esc."""
    state = initial or panel_default(ctx)
    agents = [a.copy() for a in state["agents"]]
    frm = max(1, _as_int(state.get("from"))) if cont else 1
    rounds = min(20, max(max(2, frm), int(state["rounds"])))
    tabs = bool(state.get("tabs", True))
    cache = {}

    def info(a):
        p = panel_root(ctx, a).path
        if (a.tool, p) not in cache:
            cache[(a.tool, p)] = (model_choices(a.tool, [p]), configured_model(a.tool, [p]))
        return cache[(a.tool, p)]

    while True:
        n = len(agents)
        multi = any(len(ctx.roots(t, all_=True)) > 1 for t in ("claude", "codex"))
        rows = [f"start\t{GREEN}{BOLD}▶ {'continue' if cont else 'start'}{RESET}  "
                f"{DIM}{f'the agents go on from round {frm}' if cont else 'the agents go to work'}{RESET}"
                f"\t{'go on: the agents get the latest messages' if cont else 'start the MAP: the rules and the question go to every agent'}"]
        for i, a in enumerate(agents):
            choices, conf = info(a)
            r = panel_root(ctx, a)
            pa = a.copy()
            pa.eff_model, pa.eff_effort = panel_effective(a, conf)
            goes_on = cont and isinstance(a.prev, dict) and bool(a.prev.get("session") or a.prev.get("thread"))
            cmd = _cmdline([a.tool] + panel_argv(pa, not goes_on, "<id>", "MAP ...",
                                                      panel_master_mode(r.path, a.master) if a.tool == "claude" else ""))
            model = a.model or "(no override)"
            effort = a.effort or "(no override)"
            acct = acct_label(r.label, r.default) if len(ctx.roots(a.tool, all_=True)) > 1 else ""
            role = f"{YELLOW}★ master: can change files{RESET}" if a.master else f"{DIM}reviewer: no file changes{RESET}"
            src = "settings" if a.tool == "claude" else "config.toml"
            runs = f"runs {pa.eff_model or 'its default model'}{' ' + pa.eff_effort if pa.eff_effort else ''}"
            rows.append(f"a{i}\t  {TOOL_COLOR[a.tool]}agent {i + 1}  {a.tool:<6}{RESET}  {model:<20}  {effort:<14}  "
                        + (f"{MAGENTA}{acct:<12}{RESET}  " if multi else "") + role
                        + f"\t{runs} ({'override' if a.model else src})\\nround {frm}: {cmd}")
        if n < PANEL_MAX_AGENTS and not cont:
            rows.append(f"add\t{GREEN}+ add agent{RESET}  {DIM}a copy of agent {n}, as a reviewer{RESET}"
                        f"\tanother agent: a copy of the last one - then Enter on it to change it")
        tab_ok = bool(os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "Apple_Terminal"))
        rows.append(f"tabs\t  {'tabs':<15}{'yes' if tabs else 'no':<22}{DIM}"
                    + ("one tab per agent shows live what it does; at the end Enter there opens its session" if tab_ok
                       else "one tab per agent - iTerm2, Terminal or tmux only, not available here")
                    + f"{RESET}\tEnter: on / off")
        rounds_note = (f"the round limit, counting the {frm - 1} round(s) already run" if cont
                       else "how many rounds before ccr asks whether to go on (2-20)")
        rows.append(f"rounds\t  {'rounds':<15}{rounds:<22}{DIM}{rounds_note}{RESET}\tEnter: choose")
        has_master = any(a.master for a in agents)
        if cont:
            title = (f"{BOLD}MAP · continue{RESET}  {DIM}{fmt_cwd(folder, 50)} · {n} agents on their sessions · goes on "
                     f"from round {frm}{RESET}")
        else:
            title = (f"{BOLD}MAP{RESET}  {DIM}{fmt_cwd(folder, 50)} · {n} agents · parallel rounds"
                     f"{', the master first from round 2' if has_master else ''}{RESET}")
        title += f"\n{DIM}question: {panel_short(question, 90)}{RESET}" if question else ""
        keys = [("Enter", "continue / change" if cont else "start / change")] + ([] if cont else [("Del", "remove agent")])
        res = run_fzf(rows, title + "\n" + hint(*keys, ("Esc", "back"), tail="MAP · last step: agents"),
                      multi=False, expect=None if cont else ["del"], prompt="MAP> ")
        if res is None:
            return None
        key, ids = res
        if not ids:
            continue
        sel = ids[0]
        if key == "del":
            if sel.startswith("a") and n > 2:
                del agents[int(sel[1:])]
            continue
        if sel == "start":
            return {"agents": agents, "rounds": rounds, "tabs": tabs}
        if sel == "tabs":
            tabs = not tabs
            continue
        if sel == "add":
            if n < PANEL_MAX_AGENTS:
                c = agents[-1].copy()
                c.master = False
                agents.append(c)
            continue
        if sel == "rounds":
            lo = max(2, frm)
            got = run_fzf([f"{v}\t{v}{DIM}{'   (now)' if v == rounds else ''}{RESET}" for v in range(lo, 21)],
                          hint(("Enter", "choose"), ("Esc", "back"), tail="the round limit"),
                          multi=False, preview=False, prompt="rounds> ")
            if got and got[1]:
                rounds = int(got[1][0])
            continue
        panel_agent_menu(agents, int(sel[1:]), ctx, info, cont)


def panel_agent_menu(agents: list, i: int, ctx, info, cont: bool = False):
    """Enter on an agent of the panel page: its settings, one per row, until
    Esc. A MAP that goes on changes only the model and the effort."""
    while True:
        a = agents[i]
        choices, conf = info(a)
        roots = ctx.roots(a.tool, all_=True)
        r = panel_root(ctx, a)
        other = "codex" if a.tool == "claude" else "claude"
        model_note = (next((c[1] for c in choices if c[0] == a.model), "") if a.model
                      else _default_note(a.tool, conf["model"]))
        effort_note = "" if a.effort else _default_note(a.tool, conf["effort"])
        rows = [] if cont else [f"tool\t{'tool':<9}{TOOL_COLOR[a.tool]}{a.tool:<24}{RESET}{DIM}Enter: switch to {other}{RESET}"]
        rows += [f"model\t{'model':<9}{a.model or '(no override)':<24}{DIM}{model_note}{RESET}",
                 f"effort\t{'effort':<9}{a.effort or '(no override)':<24}{DIM}{effort_note}{RESET}"]
        if len(roots) > 1 and not cont:
            rows.append(f"account\t{'account':<9}{MAGENTA}{acct_label(r.label, r.default):<24}{RESET}"
                        f"{DIM}{who_at(a.tool, r.path)}{RESET}")
        if not cont:
            rows.append(f"master\t{'master':<9}{'★ yes' if a.master else 'no':<24}{DIM}"
                        + ("Enter: make it a reviewer again" if a.master
                           else "Enter: make it the master, the only agent that can change files") + RESET)
        if len(agents) > 2 and not cont:
            rows.append(f"remove\t{RED}remove agent {i + 1}{RESET}")
        title = f"{BOLD}MAP · agent {i + 1}{RESET}  {TOOL_COLOR[a.tool]}{a.tool}{RESET}"
        res = run_fzf(rows, title + "\n" + hint(("Enter", "change"), ("Esc", "back to the agents")),
                      multi=False, preview=False, prompt=f"agent {i + 1}> ")
        if not res or not res[1]:
            return
        act = res[1][0]
        if act == "tool":
            a.tool, a.model, a.effort, a.account = other, "", "", ""
        elif act == "model":
            m = choose_model(a.tool, choices, conf, title)
            if m is not None:
                a.model = m
                if a.effort and a.effort not in effort_choices(a.tool, choices, a.model, conf["model"]):
                    a.effort = ""
        elif act == "effort":
            e = choose_effort(a.tool, choices, conf, a.model, title)
            if e is not None:
                a.effort = e
        elif act == "account":
            dir_w = max(len(fmt_cwd(x.path, 40)) for x in roots)
            lbl = choose_account([(x.label, f"{fmt_cwd(x.path, 40):<{dir_w}}  {who_at(a.tool, x.path)}", x.default)
                                  for x in roots], f"account of agent {i + 1}")
            if lbl is not None:
                a.account = "" if next(x for x in roots if x.label == lbl).default else lbl
        elif act == "master":
            was = a.master
            for x in agents:
                x.master = False
            a.master = not was
        elif act == "remove":
            if len(agents) > 2:
                del agents[i]
            return


def new_panel(sessions: list, question: str, dry: bool, ctx) -> bool:
    """Ctrl-P / --panel: the question first (the rules ccr puts in front of it
    shown above), then the folder, then the agents; the panel runs here. Esc
    (Ctrl-C at the question) steps back one page, the question kept; r at
    the question goes on with a saved MAP instead."""
    q, folder, step = question, None, "question"
    while True:
        if step == "question":
            q = read_panel_question(panel_default(ctx), "<the folder you pick next>", q or "")
            if q is None:
                return False
            if isinstance(q, dict):
                if continue_panel(dry, ctx):
                    return True
                q = q.get("text") or ""
                continue
            step = "folder"
        elif step == "folder":
            folder = choose_folder(sessions, "", dry, "MAP · step 2: the folder the agents work in", "MAP in> ")
            step = "agents" if folder else "question"
        else:
            if not dry and not Path(folder).is_dir():
                print(f"ccr: folder no longer exists: {folder}", file=sys.stderr)
                return False
            choice = panel_page(folder, ctx, None, q)
            if not choice:
                step = "folder"
                continue
            if not dry:
                save_panel_default(choice)
            return run_panel(choice, q, folder, ctx, dry)


def editor_argv() -> list:
    """$VISUAL, else $EDITOR, else Notepad (Windows) / nano / vi."""
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
    if ed:
        return [x.strip('"') for x in shlex.split(ed, posix=os.name != "nt")]
    if os.name == "nt":
        return ["notepad"]
    return ["nano"] if shutil.which("nano") else ["vi"]


def edit_text(text: str) -> str:
    """Edit text in the editor, on a temporary file; returns the new text."""
    fd, tmp = tempfile.mkstemp(prefix="ccr-panel-question-", suffix=".md")
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))
    before = os.stat(tmp).st_mtime_ns
    argv = editor_argv() + [tmp]
    t0 = time.monotonic()
    try:
        subprocess.run(argv)
    except OSError as e:
        print(f"{YELLOW}ccr: cannot start the editor '{argv[0]}': {e}{RESET}")
    if time.monotonic() - t0 < 2 and os.stat(tmp).st_mtime_ns == before:
        # Windows 11 Notepad and VS Code without --wait hand the file to a
        # window and return at once.
        ask(f"{YELLOW}ccr: edit {tmp} in the editor, save it, then press Enter here.{RESET} ")
    new = _read_text(tmp).lstrip("﻿").replace("\r\n", "\n").rstrip()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return new


def _pending_input() -> str:
    """What is already waiting once input() has returned a line: the rest of a
    paste (input() stops at its first newline). Unix: the terminal leaves
    canonical mode for a moment, so a last line without a newline comes too."""
    if not sys.stdin.isatty():
        return ""
    try:
        if os.name == "nt":
            import msvcrt
            out, deadline = [], time.monotonic() + 0.04
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    out.append(sys.stdin.readline())   # a last line without a newline waits for Enter
                    deadline = time.monotonic() + 0.04
                else:
                    time.sleep(0.005)
            return "".join(out)
        import select
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN], raw[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        data = b""
        try:
            while select.select([fd], [], [], 0.04)[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_panel_question(panel: dict, folder: str, seed: str = "", cont: dict = None):
    """The rules ccr puts in front of the question (dim, agent 1's copy),
    then the question: one line typed here, a paste of several lines, or,
    with an empty line or "e", the editor ($VISUAL / $EDITOR / nano / vi);
    "r" goes on with a saved MAP instead ({"continue": True}). cont: a saved
    MAP going on - where it stopped, then an optional message to every agent
    ('' = none). Returns the text, or None (Ctrl-C / Ctrl-D: back)."""
    ed = os.path.basename(editor_argv()[0])
    if cont is not None:
        done = _as_int(cont.get("round"))
        ql = str(cont.get("question") or "").split("\n")
        print(f"\n{BOLD}MAP · continue:{RESET} {DIM}{panel_short(str(cont.get('question') or ''), 90)}{RESET}\n")
        print(f"{DIM}  the question:{RESET}")
        for l in ql[:8]:
            print(f"{DIM}    {l}{RESET}")
        if len(ql) > 8:
            print(f"{DIM}    …{RESET}")
        print(f"\n{DIM}  {done} round(s) run · {cont.get('outcome') or ''}{RESET}")
        for x in sorted(cont.get("agents") or [], key=lambda x: _as_int(x.get("index"))):
            lbl = " ".join(str(v) for v in (x.get("tool"), x.get("model") or "its default model", x.get("effort")) if v)
            failed = f" · its last turn did not finish: {x['failed']}" if x.get("failed") else ""
            print(f"{DIM}    agent {x.get('index')}: {lbl}{' (master)' if x.get('master') else ''}{failed}{RESET}")
        print()
        line = ask(f"message to every agent for round {done + 1}  {DIM}optional: the answers to their questions, a new "
                   f"hint · Enter for none · e: write it in {ed}{RESET}\n> ")
        if line is None:
            return None
        text = (line + "\n" + _pending_input()).replace("\r\n", "\n").replace("\r", "\n").strip()
        return edit_text("").strip() if text.lower() == "e" else text
    preview = [a.copy() for a in panel["agents"]]
    for i, a in enumerate(preview, 1):
        a.index = i
    print(f"\n{BOLD}MAP · your question{RESET}  {DIM}ccr puts these rules in front of it (agent 1's copy){RESET}\n")
    for l in panel_protocol(1, preview, folder, "<your question>").rstrip().split("\n"):
        print(f"{DIM}  {l}{RESET}")
    print()
    cur = (seed or "").strip()
    while True:
        if cur:
            more = cur.count("\n")
            shown = panel_short(cur, 60) + (f" (+{more} line{'' if more == 1 else 's'})" if more else "")
            prompt = f"question [{shown}]  {DIM}Enter keeps it · e edits it in {ed} · r: continue a saved MAP{RESET}\n> "
        else:
            prompt = f"question  {DIM}one line, or Enter to write it in {ed} · r: continue a saved MAP{RESET}\n> "
        line = ask(prompt)
        if line is None:
            return None
        text = (line + "\n" + _pending_input()).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            if cur:
                return cur
            cur = edit_text("").strip()
            continue
        if text.lower() == "e":
            cur = edit_text(cur).strip()
            continue
        if text.lower() == "r":
            return {"continue": True, "text": cur}
        if "\n" in text:
            cur = text   # a paste: shown back first
            continue
        return text


# ----------------------------------------------------------------------------
# release channels: ccr (stable) and ccrtest (test), side by side
# ----------------------------------------------------------------------------
REPO = "Cepstral/claude-codex-resume"
# Channels are branches of the public repo, served raw by GitHub. Two copies
# live next to each other: `ccr` (stable = main) and `ccrtest` (test branch),
# and the channel of a copy is its file name. `ccr --update` refreshes the
# stable copy from main, `ccrtest --update` the test copy from the test
# branch, and `ccr --channel test` installs/refreshes the test copy.
CHANNELS = {"stable": "main", "test": "test"}


def self_path() -> str:
    return os.path.realpath(sys.argv[0] or __file__)


def channel_of(path: str) -> str:
    """'test' for a copy named <name>test (ccrtest, ccrtest.py), else 'stable'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return "test" if stem.endswith("test") else "stable"


def channel_path(channel: str, any_copy: str) -> str:
    """The sibling copy of a channel: ccr <-> ccrtest (an extension, as in a
    source checkout's ccr.py <-> ccrtest.py, is kept)."""
    d = os.path.dirname(any_copy)
    stem, ext = os.path.splitext(os.path.basename(any_copy))
    if stem.endswith("test"):
        stem = stem[:-4]
    return os.path.join(d, stem + ("test" if channel == "test" else "") + ext)


def file_version(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(40))
        m = re.search(r'VERSION = "([^"]+)"', head)
        return m.group(1) if m else "?"
    except OSError:
        return "none"


def branch_head(branch: str, timeout: int = 15):
    """The branch head through the API - never cached, unlike the raw CDN,
    which serves a branch URL from cache for minutes after a push. None
    when the API is unreachable."""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{branch}",
                                     headers={"User-Agent": "ccr"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("sha")
    except Exception:
        return None


def fetch_script(ref: str, timeout: int = 30):
    """Download one revision of this script and compile-check it. Returns
    (source, version); raises RuntimeError when it cannot be used."""
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/ccr.py"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ccr"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            src = r.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"download failed: {e}")
    try:
        compile(src, "ccr.py", "exec")
    except SyntaxError as e:
        raise RuntimeError(f"the downloaded file does not parse ({e}) - nothing replaced")
    m = re.search(r'VERSION = "([^"]+)"', src)
    return src, (m.group(1) if m else "?")


def install_script(src: str, target: str):
    tmp = target + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)


def update_self(channel: str, target: str, dry: bool):
    branch = CHANNELS.get(channel)
    if not branch:
        sys.exit(f"ccr: unknown channel '{channel}' (known: {', '.join(CHANNELS)})")
    # Fetch by commit, which is immutable; fall back to the branch URL when
    # the API is unreachable.
    sha = branch_head(branch, 15)
    ref = sha or branch
    at = f"branch {branch} @ {sha[:7]}" if sha else f"branch {branch} (head unknown, raw URL may lag)"
    if dry:
        print(f"would download {at} -> {target}")
        return
    try:
        src, new_ver = fetch_script(ref)
    except RuntimeError as e:
        sys.exit(f"ccr: {e}")
    had = file_version(target)
    install_script(src, target)
    if sha:
        set_channel_state(channel, sha=sha)
    cmd = "ccrtest" if channel == "test" else "ccr"
    print(f"{GREEN}ccr: channel '{channel}' ({at}) v{had} -> v{new_ver} at {target}. Run: {cmd}{RESET}")
    if new_ver == had:
        print("ccr: same version as before - nothing newer on that branch.")


# --- auto-update -------------------------------------------------------------
# On the first run in a shell (a new parent process), and then once an hour
# per channel, ccr asks GitHub for the branch head at start; when the
# installed commit differs, the new file is downloaded, compile-checked and
# swapped in before the run - with a message - and the picker's first line
# says "updated vX -> vY" for that run only. ccr.state.json next to ccr.json
# remembers the installed commit, the last check and the shell it was made
# from. CCR_AUTO_UPDATE=0 turns it off.
AUTO_UPDATE_HOURS = 1


def state_path() -> Path:
    """Per machine, never in a synced dir: a shared state would tell a second
    PC that the latest commit is installed while it still runs an old file.
    CCR_STATE overrides the location (tests)."""
    if os.environ.get("CCR_STATE"):
        return Path(os.environ["CCR_STATE"])
    return Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "ccr" / "ccr.state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def set_channel_state(channel: str, **values):
    state = load_state()
    st = state.get(channel) if isinstance(state.get(channel), dict) else {}
    st.update(values)
    state[channel] = st
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v or "0"))


def in_checkout(path: str) -> bool:
    """Is this copy a file of a git checkout (a .git next to it)? Then it is
    a developer's working copy: never replaced by itself."""
    return os.path.exists(os.path.join(os.path.dirname(path), ".git"))


def auto_update(channel: str, me: str):
    """(had, new) when a newer version was installed, else None. Never a
    downgrade, and never inside a git checkout - an uncommitted edit would
    be silently replaced by the pushed version."""
    if os.environ.get("CCR_AUTO_UPDATE") == "0" or in_checkout(me):
        return None
    branch = CHANNELS.get(channel)
    if not branch:
        return None
    st = load_state().get(channel) or {}
    now = int(time.time())
    shell = os.getppid()
    if st.get("shell") == shell and now - int(st.get("checked") or 0) < AUTO_UPDATE_HOURS * 3600:
        return None
    # Recorded before the network call, also when it fails: a slow or
    # offline network costs one short timeout per hour, not one per run.
    set_channel_state(channel, checked=now, shell=shell)
    sha = branch_head(branch, 3)
    if not sha or sha == st.get("sha"):
        return None
    target = channel_path(channel, me)
    src, new_ver = fetch_script(sha, 10)
    had = file_version(target)
    if version_tuple(new_ver) <= version_tuple(had):   # same (e.g. a docs-only commit) or older
        set_channel_state(channel, sha=sha)
        return None
    print(f"{YELLOW}ccr: auto-updating channel '{channel}' v{had} -> v{new_ver} (branch {branch} @ {sha[:7]})...{RESET}")
    install_script(src, target)
    set_channel_state(channel, sha=sha)
    return had, new_ver


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
# The PowerShell spellings (ccr -Update, -Root work, -WhatIf, -AddAccount x,
# any case, one or two dashes) translated to the GNU ones argparse knows, so
# a command line copied from either doc works on both platforms. Only known
# names are touched; anything else stays a filter word or an argparse error.
PS_FLAGS = {"update": "--update", "channel": "--channel", "root": "--root", "accounts": "--accounts",
            "addaccount": "--add-account", "removeaccount": "--remove-account", "disableaccounts": "--disable-accounts",
            "copysettings": "--copy-settings", "copystatusline": "--copy-settings", "tool": "--tool", "top": "--top",
            "new": "--new", "newwindow": "--new-window", "whatif": "--dry-run", "dryrun": "--dry-run",
            "usagehours": "--usage-hours", "usage": "--usage", "tabs": "--tabs", "terminal": "--terminal", "version": "--version",
            "install": "--install", "panel": "--panel", "map": "--panel", "continue": "--continue",
            "filter": None}


def normalize_argv(argv: list) -> list:
    out = []
    for tok in argv:
        m = re.fullmatch(r"--?([A-Za-z][A-Za-z-]+)(?:[=:](.*))?", tok, re.S)
        key = m.group(1).replace("-", "").lower() if m else None
        if not m or key not in PS_FLAGS:
            out.append(tok)
            continue
        flag = PS_FLAGS[key]
        if flag is None:          # -Filter kit: the value is the query itself
            if m.group(2) is not None:
                out.append(m.group(2))
            continue
        out.append(flag if m.group(2) is None else f"{flag}={m.group(2)}")
    return out


def main():
    ap = argparse.ArgumentParser(prog="ccr",
                                 description="Resume Claude Code / Codex conversations as terminal tabs.")
    ap.add_argument("query", nargs="*", help="pre-seed the picker filter (ccr kit)")
    ap.add_argument("--tool", choices=["claude", "codex", "all"], default="all")
    ap.add_argument("--top", type=int, default=200, help="most recent sessions to list (0 = all)")
    ap.add_argument("-n", "--new", action="store_true",
                    help="start a new conversation (folder menu); trailing text prefills the name box")
    ap.add_argument("--panel", "--map", action="store_true",
                    help="a panel: 2-4 fresh agents (claude and/or codex, each with its model, effort and account) "
                         "discuss one question in a folder until they agree (Ctrl-P in the picker): the question, "
                         "then the folder, then the agents; the trailing words prefill the question - quote it. "
                         "Every agent works like its CLI in auto mode, the web included; no turn is ever cut (after "
                         "20 minutes ccr asks). --dry-run prints the round-1 commands")
    ap.add_argument("--panel-watch", default="", help=argparse.SUPPRESS)   # a MAP agent's tab (the viewer)
    ap.add_argument("--panel-agent", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="with --panel (or alone): go on with a saved MAP - pick it, write a message to its agents if "
                         "you like, and the same agents continue on their own sessions from the next round")
    ap.add_argument("--new-window", action="store_true", help="open selections in new windows, keep this tab")
    ap.add_argument("--tabs", action="store_true",
                    help="Terminal.app: open the extra sessions as tabs instead of windows. Terminal has no "
                         "AppleScript for a new tab, so this presses Cmd-T through System Events and macOS asks "
                         "for Automation rights once; iTerm2 and tmux use tabs either way")
    ap.add_argument("--terminal", action="store_true",
                    help="resume Codex desktop-app conversations with 'codex resume' in a terminal "
                         "instead of handing them back to the app")
    ap.add_argument("--dry-run", action="store_true", help="print what would be launched, launch nothing")
    ap.add_argument("--usage", action="store_true",
                    help="start with the token-usage column on (Ctrl-K toggles it); ccr.json \"usageColumn\": true "
                         "makes that the default")
    ap.add_argument("--usage-hours", type=int, default=None, metavar="H",
                    help="window of the token-usage column (Ctrl-K) and details (Ctrl-J); default: ccr.json "
                         "\"usageHours\", else 5 = Claude's usage window")
    acc = ap.add_argument_group("accounts (one data dir per account and tool, ccr.json)")
    acc.add_argument("--root", metavar="LABEL", default="",
                     help="list only this account's sessions (label from ccr.json); also the account -n defaults to")
    acc.add_argument("--accounts", action="store_true", help="list the configured accounts and who is logged in where")
    acc.add_argument("--add-account", metavar="LABEL", default="",
                     help="create a config dir per tool (see --tool) next to the tool's default dir, run the "
                          "tool's own login inside it, and record it in ccr.json")
    acc.add_argument("--copy-settings", "--copy-statusline", action="store_true", dest="copy_settings",
                     help="with --add-account: give the new dir the default account's settings "
                          "(claude: status line, codex: config.toml)")
    acc.add_argument("--remove-account", metavar="LABEL", default="",
                     help="move its sessions to the default account and forget the entry (nothing deleted)")
    acc.add_argument("--disable-accounts", action="store_true",
                     help="do that for every account and turn multi-account mode off")
    ins = ap.add_argument_group("installing the tools")
    ins.add_argument("--install", choices=["claude", "codex", "all"], default="",
                     help="install or update claude / codex with the vendor's published installer, fetched from its "
                          "official URL and run as published (Ctrl-T in the picker); all = both")
    upd = ap.add_argument_group("release channels")
    upd.add_argument("--update", action="store_true",
                     help="refresh this copy from its own channel (ccr: main, ccrtest: the test branch)")
    upd.add_argument("--channel", choices=["stable", "test"], default="",
                     help="install/refresh the side-by-side copy of that channel (test = ccrtest next to ccr)")
    ap.add_argument("--version", action="version", version="ccr " + VERSION + " (python)")
    a = ap.parse_args(normalize_argv(sys.argv[1:]))
    if a.panel_watch:
        watch_panel_agent(a.panel_watch, a.panel_agent)
        return
    query = " ".join(a.query).strip()
    # Token-usage defaults from ccr.json: "usageColumn": true starts the
    # picker with the column on, "usageHours": N sets the window. The
    # flags win over the file.
    cfg = load_config() or {}
    usage_default = a.usage or bool(cfg.get("usageColumn"))
    if a.usage_hours is None:
        try:
            a.usage_hours = int(cfg.get("usageHours") or 5)
        except (TypeError, ValueError):
            a.usage_hours = 5
    if a.usage_hours < 1:
        a.usage_hours = 5

    if a.update or a.channel:
        me = self_path()
        ch = a.channel or channel_of(me)
        update_self(ch, channel_path(ch, me), a.dry_run)
        return
    # Auto-update (throttled; a message only when something is installed),
    # then run the freshly installed file with the same arguments. The
    # banner the picker shows for this one run travels in an env var.
    upd_note = os.environ.pop("CCR_UPDATED_FROM", "")
    if not a.dry_run and not upd_note:
        me = self_path()
        try:
            upd = auto_update(channel_of(me), me)
        except Exception as e:
            print(f"ccr: auto-update failed: {e}", file=sys.stderr)
            upd = None
        if upd:
            env = dict(os.environ)
            env["CCR_UPDATED_FROM"] = f"{upd[0]}>{upd[1]}"
            os.execve(sys.executable, [sys.executable, channel_path(channel_of(me), me)] + sys.argv[1:], env)
    if a.install:
        ok = True
        for t in (["claude", "codex"] if a.install == "all" else [a.install]):
            ok = run_install(t, a.dry_run) and ok
        sys.exit(0 if ok else 1)
    if a.accounts:
        show_accounts()
        return
    if a.add_account or a.remove_account or a.disable_accounts:
        if a.dry_run:
            # The login flows are external processes, so --dry-run must stop
            # here (the same notice the picker's account page prints).
            act = ({"action": "add", "tool": a.tool, "label": a.add_account, "copy": a.copy_settings} if a.add_account
                   else {"action": "remove", "tool": a.tool, "label": a.remove_account} if a.remove_account
                   else {"action": "disable"})
            run_account_action(act, True)
            return
        try:
            if a.add_account:
                add_account(a.add_account, a.tool, a.copy_settings)
            elif a.remove_account:
                remove_account(a.remove_account, a.tool)
            else:
                disable_multi_account()
        except Exception as e:
            sys.exit(str(e))
        return

    while True:   # restarted after an account change, so the listing reflects the new ccr.json
        ctx = Ctx(a.root)
        sessions = []
        if a.tool in ("claude", "all"):
            for r in ctx.claude:
                sessions += claude_sessions(r)
        if a.tool in ("codex", "all"):
            for r in ctx.codex:
                sessions += codex_sessions(r)
        if not sessions and not (a.new or a.panel or a.cont):
            absent = [t for t in ("claude", "codex") if not tool_path(t)]
            sys.exit("ccr: no sessions found." + (f" Not installed on this computer: {', '.join(absent)} - "
                                                  f"ccr --install {absent[0]} installs it." if absent else ""))
        sessions.sort(key=lambda s: s.last, reverse=True)
        if a.top > 0:
            sessions = sessions[: a.top]

        if a.new or a.panel or a.cont:
            # -n: the folder menu (works with no session at all: the "here"
            # row is always there); the trailing text prefills the name box.
            # --panel is Ctrl-P: the question (prefilled with the trailing
            # text), then the folder, then the agents; --continue goes on
            # with a saved MAP.
            ok = (continue_panel(a.dry_run, ctx) if a.cont else new_panel(sessions, query, a.dry_run, ctx) if a.panel
                  else new_conversation(sessions, query, a.dry_run, ctx, a.terminal))
            if not ok:
                print("ccr: cancelled.")
            return

        # Token usage (Ctrl-K toggles the column, Ctrl-J opens the details):
        # computed on demand and cached for the picker's lifetime. The
        # column starts on with --usage or ccr.json "usageColumn": true.
        usage_on, usage_of = usage_default, {}
        usage_since = datetime.now(timezone.utc) - timedelta(hours=a.usage_hours)

        def usage_cached(s):
            if s.key not in usage_of:
                usage_of[s.key] = session_usage(s, usage_since)
            return usage_of[s.key]

        def usage_fill():
            """Read the transcripts written inside the window, once per picker."""
            todo = [s for s in sessions if s.key not in usage_of]
            if todo:
                print(f"{YELLOW}ccr: reading token usage of the last {a.usage_hours} h...{RESET}")
                for s in todo:
                    usage_cached(s)

        if usage_on:
            usage_fill()
        # Account limits (the table above the list): the stale accounts are
        # asked in the background; with fzf 0.45+ an answer rewrites the
        # header of the fzf on screen, else it shows at the next redraw.
        lim = Limits(ctx)
        lim.refresh()
        restart = False
        while not restart:
            index = {}
            rows = session_rows(sessions, index, ctx, usage_of if usage_on else None)
            note = upd_note
            lim.header = lambda: picker_hint(ctx, note, lim)
            lim.port = free_port() if lim.pending and fzf_version() >= (0, 45) else 0
            res = run_fzf(rows, picker_hint(ctx, upd_note, lim), query=query,
                          expect=["del", "ctrl-n", "ctrl-p", "ctrl-a", "ctrl-o", "ctrl-k", "ctrl-j", "ctrl-x", "ctrl-e",
                                  "ctrl-t", "ctrl-l"], listen=lim.port)
            lim.port = 0
            if res is None:
                print("ccr: cancelled.")
                return
            key, ids = res
            picked = [index[i] for i in ids if i in index]
            if key == "ctrl-l":
                # Ask every account for its usage limits again.
                lim.refresh(force=True)
                continue
            if key == "ctrl-k":
                # The usage column on/off. Turning it on reads the transcripts
                # written inside the window (once per picker).
                usage_on = not usage_on
                if usage_on:
                    usage_fill()
                continue
            if key == "ctrl-j":
                # The usage details of the highlighted row, plus the window
                # split session by session (every listed session).
                if picked:
                    print(f"{YELLOW}ccr: reading token usage of the last {a.usage_hours} h...{RESET}")
                    all_ = [(x, usage_cached(x)) for x in sessions]
                    usage_page(picked[0], usage_cached(picked[0]), a.usage_hours, usage_since, all_)
                    pause()
                continue
            if key == "ctrl-n":
                if not new_conversation(sessions, "", a.dry_run, ctx, a.terminal):
                    print("ccr: cancelled.")
                return
            if key == "ctrl-p":
                # A panel: the question first, then the folder, then the agents.
                if not new_panel(sessions, "", a.dry_run, ctx):
                    print("ccr: cancelled.")
                return
            if key == "ctrl-t":
                # Install or update claude / codex (Ctrl+I or Ctrl+T in the
                # PowerShell picker; fzf sees Ctrl-I as Tab), then list again.
                tool = install_page()
                if tool:
                    run_install(tool, a.dry_run)
                    pause()
                    restart = True
                    upd_note = ""
                continue
            if key == "ctrl-a":
                # The account page: add / remove / copy settings / turn off,
                # then run again from the top so the listing and the account
                # column reflect the new ccr.json.
                act = account_page(ctx, a.dry_run)
                if act:
                    run_account_action(act, a.dry_run)
                    pause()
                    restart = True
                    upd_note = ""   # the banner is for the first listing only
                continue
            if key == "ctrl-x":
                # Close the running conversation of the highlighted row (a stray
                # background session, a forgotten tab). Kept on disk.
                if picked:
                    s = picked[0]
                    if s.running_on:
                        print(f"{YELLOW}ccr: '{s.title}' is open on {s.running_on} - close it there.{RESET}")
                        pause("(Enter to continue)")
                    elif not s.running:
                        print(f"{YELLOW}ccr: '{s.title}' is not running - nothing to close.{RESET}")
                        pause("(Enter to continue)")
                    elif confirm_close(s):
                        if a.dry_run:
                            print(f"dry-run: would close '{s.title}' (pid {s.pid})")
                            pause("(Enter to continue)")
                        elif stop_session(s):
                            s.running, s.pid, s.run_kind = False, None, ""
                        else:
                            print(f"ccr: could not close '{s.title}' (pid {s.pid}).", file=sys.stderr)
                            pause("(Enter to continue)")
                continue
            if key == "del":
                if not picked:
                    continue
                victim = picked[0]
                if victim.running:
                    print(f"ccr: '{victim.title}' is running right now - close it first (Ctrl-X).")
                    pause("(Enter to continue)")
                    continue
                if victim.running_on:
                    print(f"ccr: '{victim.title}' is open on {victim.running_on} - close it there first.")
                    pause("(Enter to continue)")
                    continue
                if confirm_delete(victim):
                    if a.dry_run:
                        print(f"dry-run: would delete '{victim.title}'")
                    else:
                        try:
                            ok = remove_session_data(victim)
                        except Exception as e:
                            ok = False
                            print(f"ccr: delete failed: {e}", file=sys.stderr)
                        if ok:
                            sessions = [s for s in sessions if s.key != victim.key]
                query = ""
                continue
            if key == "ctrl-e":
                # The rows Enter would open, resumed with a model and an effort
                # picked per tool (Shift+Enter in the PowerShell picker; fzf
                # cannot tell Shift+Enter from Enter).
                if not picked:
                    continue
                elsewhere = [x for x in picked if x.running_on]
                if elsewhere and not confirm_elsewhere(elsewhere):
                    continue
                opts = launch_options(picked, ctx)
                if opts is None:
                    continue
                for x in picked:
                    x.model_override, x.effort_override = opts.get(x.tool, ("", ""))
                launch(picked, a.new_window, a.dry_run, ctx, a.terminal, a.tabs)
                return
            if not picked:
                print("ccr: cancelled.")
                return
            if key == "ctrl-o":
                # Open the marked rows under another account: the conversation
                # moves into that account's dir first (running ones refused).
                if not ctx.multi_root:
                    print("ccr: no accounts configured - Ctrl-A adds one.")
                    pause()
                    continue
                # The menu is the legend's numbering: one entry per tool and
                # account, only for the tools of the marked rows. Picking an
                # entry sets its label on every marked row (a row of the
                # other tool without a dir for that label is reported at
                # launch and stays where it is).
                tools = {s.tool for s in picked}
                rows = [f"{n}\t{TOOL_COLOR[t]}{BOLD}{n}{RESET} {MAGENTA}{acct_label(r.label, r.default):<14}{RESET} "
                        f"{TOOL_COLOR[t]}{t:<6}{RESET} {fmt_cwd(r.path, 40):<40}  {DIM}{who_at(t, r.path)}{RESET}\t{r.path}"
                        for n, t, r in ctx.entries if t in tools]
                cnt = len(picked)
                res = run_fzf(rows, hint(("Enter", "choose"), ("Esc", "back"),
                                         tail=f"open {cnt} conversation{'' if cnt == 1 else 's'} under which account?"),
                              multi=False, preview=False, prompt="account> ")
                if not res or not res[1]:
                    continue
                lbl = next(r.label for n, t, r in ctx.entries if str(n) == res[1][0])
                for s in picked:
                    s.target_root = lbl
            # A conversation open on another PC: ask first - two processes
            # would append to the same transcript.
            elsewhere = [s for s in picked if s.running_on]
            if elsewhere and not confirm_elsewhere(elsewhere):
                continue
            launch(picked, a.new_window, a.dry_run, ctx, a.terminal, a.tabs)
            return


if __name__ == "__main__":
    main()
