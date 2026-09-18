#!/usr/bin/env python3
"""ccr - multi-select resume picker for Claude Code + Codex CLI sessions.

Native macOS/Linux port of Resume-CcSessions.ps1 (same data sources, same
rules, same features - the two are kept aligned change for change), with fzf
as the picker and real terminal tabs (iTerm2, Terminal.app) or tmux windows
as the launch backend. Python 3.9+, stdlib only; fzf for UI.

Keys in the picker (fzf conventions): type to fuzzy-filter, Tab marks,
Enter opens, Ctrl-E opens with a model / effort, Ctrl-N new conversation, Ctrl-A accounts, Ctrl-O open the
marked rows under another account, Del deletes, Esc cancels.
"""
import argparse
import base64
import ctypes
import json
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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Shown in the picker hint line; bumped together with $script:CcrVersion in
# Resume-CcSessions.ps1 - the two scripts move in lockstep.
VERSION = "0.61"
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
    try:
        idx = {}
        with open(root / "session_index.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("id") and o.get("thread_name"):
                    idx[o["id"]] = o["thread_name"]  # last wins
        for k, v in idx.items():
            m.setdefault(k, v)
    except OSError:
        pass
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
                                        if not c["text"].startswith("<"):
                                            title = clean_title(c["text"])
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


def run_fzf(rows, header, query="", multi=True, expect=None, preview=True, prompt="filter> "):
    """Rows are '<id>\\t<display>\\t<preview>'. Returns (key, [ids]) or None on
    Esc/Ctrl-C; key is '' for Enter or one of `expect`."""
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

    def legend(self) -> list:
        """One line per tool and account: the tool (once per group), the
        number in the tool's colour, the label, the dir and the email logged
        in there. Dirs are dropped when the lines would not fit."""
        width = shutil.get_terminal_size((100, 24)).columns - 4
        lbl_w = max(len(acct_label(r.label, r.default)) for _, _, r in self.entries)
        n_w = len(str(len(self.entries)))
        cells = {}
        for n, t, r in self.entries:
            qk = f"{t}|{r.label}"
            if qk not in self.quick:
                self.quick[qk] = quick_identity(t, r.path)
            cells[n] = (fmt_cwd(r.path, 28), self.quick[qk] or who_at(t, r.path))
        dir_w = max(len(c[0]) for c in cells.values())
        who_w = max(len(c[1]) for c in cells.values())
        with_dirs = 2 + 6 + 2 + n_w + 1 + lbl_w + 2 + dir_w + 2 + who_w <= width
        lines = [f"{BOLD}{MAGENTA}Multi-account mode active.{RESET}"]
        prev = ""
        for n, t, r in self.entries:
            d, who = cells[n]
            tool_txt = f"{t:<6}" if t != prev else " " * 6
            prev = t
            line = (f"  {TOOL_COLOR[t]}{tool_txt}{RESET}  {TOOL_COLOR[t]}{BOLD}{n:>{n_w}}{RESET} "
                    f"{MAGENTA}{acct_label(r.label, r.default):<{lbl_w}}{RESET}")
            if with_dirs:
                line += f"  {d:<{dir_w}}"
            line += f"  {DIM}{who:<{who_w}}{RESET}"
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


def picker_hint(ctx: Ctx, upd_note: str = "") -> str:
    lines = []
    if ">" in upd_note:
        f, t = upd_note.split(">", 1)
        lines.append(f"{BOLD}{GREEN}ccr updated v{f} -> v{t}{RESET}")
    lines += ctx.legend() if ctx.multi_root else []
    keys = [("↑↓", "move"), ("Tab", "mark"), ("Enter", "open"), ("Ctrl-E", "model/effort")]
    if ctx.multi_root:
        keys.append(("Ctrl-O", "open under another account"))
    keys += [("Ctrl-N", "new"), ("Ctrl-A", "accounts"), ("Ctrl-K", "usage"), ("Ctrl-J", "details"),
             ("Ctrl-X", "close"), ("Del", "delete"), ("Esc", "cancel")]
    lines.append(hint(*keys))
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
                 "models", "buckets", "limit_5h", "limit_7d", "reset_5h")

    def __init__(self):
        self.turns = self.input = self.cache_write = self.cache_read = self.output = self.thinking = self.total = 0
        self.models, self.buckets = {}, {}
        self.limit_5h = self.limit_7d = self.reset_5h = None

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
            m = re.search(r'"primary":\{"used_percent":([0-9.]+),"window_minutes":\d+,"resets_at":(\d+)', line)
            if m:
                u.limit_5h, u.reset_5h = float(m.group(1)), int(m.group(2))
            m = re.search(r'"secondary":\{"used_percent":([0-9.]+)', line)
            if m:
                u.limit_7d = float(m.group(1))


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
        if u.limit_5h is not None:
            reset = f" (resets {datetime.fromtimestamp(u.reset_5h, tz=timezone.utc).astimezone():%H:%M})" if u.reset_5h else ""
            seven = f"{u.limit_7d:.0f}%" if u.limit_7d is not None else "?"
            print(f"  rate limit seen at the last turn: 5 h {u.limit_5h:.0f}%{reset} · 7 d {seven}")
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
        try:
            env = tool_env("codex", s.root_path) if s.root_path else None
            if subprocess.run(["codex", "delete", s.id], capture_output=True, env=env).returncode == 0:
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


def open_tab(cwd: str, cmd: str, new_window: bool, dry: bool, tabs: bool = False) -> bool:
    """Run cmd in a new terminal surface: a tmux window when inside tmux,
    else an iTerm2 / Terminal.app tab (or window). False = no backend.

    Terminal.app tabs cost an Automation prompt (see below), so there a window
    is the default and `tabs` is the opt-in; iTerm2 and tmux pay nothing for a
    tab and keep it."""
    shell_cmd = f"cd {shlex.quote(cwd)} && {cmd}"
    tp = os.environ.get("TERM_PROGRAM", "")
    if os.environ.get("TMUX"):
        argv = ["tmux", "new-window", "-c", cwd, cmd]
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
                if isinstance(o, dict) and o.get("value"):
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
        src = "settings" if tool == "claude" else "config.toml"
        n = len(rows_t)
        title = (f"{BOLD}Resume with a model / effort{RESET}  {TOOL_COLOR[tool]}{tool}{RESET} "
                 f"{DIM}· {n} conversation{'' if n == 1 else 's'}{RESET}")

        def default_note(val):
            if val == "*":
                return f"{src}: differs per account"
            return f"{src}: {val}" if val else f"{src}: not set"

        rows = [f"0\t{'(no override)':<26}{DIM}{default_note(cfg['model'])}{RESET}"]
        rows += [f"{i}\t{v:<26}{DIM}{note}{RESET}" for i, (v, note, _) in enumerate(choices, 1)]
        res = run_fzf(rows, title + "\n" + hint(("Enter", "choose"), ("Esc", "back to the list"), tail="model"),
                      multi=False, preview=False, prompt=f"{tool} model> ")
        if not res or not res[1]:
            return None
        i = int(res[1][0])
        model = choices[i - 1][0] if i else ""
        levels = effort_choices(tool, choices, model, cfg["model"])
        eff_note = f"{src}: per model" if tool == "claude" and cfg["per_model"] else default_note(cfg["effort"])
        rows = [f"0\t{'(no override)':<26}{DIM}{eff_note}{RESET}"]
        rows += [f"{i}\t{lv}" for i, lv in enumerate(levels, 1)]
        res = run_fzf(rows, title + "\n" + hint(("Enter", "choose"), ("Esc", "back to the list"),
                                               tail=f"effort{f' for {model}' if model else ''}"),
                      multi=False, preview=False, prompt=f"{tool} effort> ")
        if not res or not res[1]:
            return None
        i = int(res[1][0])
        out[tool] = (model, levels[i - 1] if i else "")
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


def new_conversation(sessions: list, initial_name: str, dry: bool, ctx: Ctx, terminal: bool = False) -> bool:
    """Folder -> tool -> (claude) name -> (several accounts for that tool)
    account, then the tool takes over this terminal."""
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
    res = run_fzf(rows, hint(("Enter", "next"), ("Ctrl-O", "new folder"), ("Esc", "back"),
                             tail="new conversation · step 1: folder"),
                  multi=False, prompt="new session in> ", expect=["ctrl-o"])
    if not res or not (res[0] or res[1]):
        return False
    if res[0] == "ctrl-o" or (res[1] and res[1][0] == "new"):
        folder = ask_new_folder(initial_name if os.sep in initial_name else "", dry)
        if not folder:
            return False
    else:
        folder = index[res[1][0]]["path"]
    opener = [] if terminal else url_opener()
    app_ok = bool(opener) and codex_app_installed(sessions)
    extra = []
    if app_ok:
        extra.append(f"codex app\t{CYAN}codex{RESET}{DIM} app{RESET}{' ' * 2}{DIM}a new thread in the Codex "
                     f"desktop app, rooted at this folder{RESET}\tcodex app desktop")
    tool = choose_tool(f"step 2: tool · {fmt_cwd(folder, 46)}", "asks for a session name",
                       "a terminal tab · no start name - /rename inside", extra)
    if not tool:
        return False
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
    argv = ["claude", "--name", name] if (tool == "claude" and name) else [tool]
    rp = root.path if root else ""
    exec_inline(folder, argv, f"{tool} · {name or 'new'}", dry, root_prefix(ctx, tool, rp), root_env(ctx, tool, rp))
    return True


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
    upd = ap.add_argument_group("release channels")
    upd.add_argument("--update", action="store_true",
                     help="refresh this copy from its own channel (ccr: main, ccrtest: the test branch)")
    upd.add_argument("--channel", choices=["stable", "test"], default="",
                     help="install/refresh the side-by-side copy of that channel (test = ccrtest next to ccr)")
    ap.add_argument("--version", action="version", version="ccr " + VERSION + " (python)")
    a = ap.parse_args(normalize_argv(sys.argv[1:]))
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
        if not sessions:
            sys.exit("ccr: no sessions found.")
        sessions.sort(key=lambda s: s.last, reverse=True)
        if a.top > 0:
            sessions = sessions[: a.top]

        if a.new:
            if not new_conversation(sessions, query, a.dry_run, ctx, a.terminal):
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
        restart = False
        while not restart:
            index = {}
            rows = session_rows(sessions, index, ctx, usage_of if usage_on else None)
            res = run_fzf(rows, picker_hint(ctx, upd_note), query=query,
                          expect=["del", "ctrl-n", "ctrl-a", "ctrl-o", "ctrl-k", "ctrl-j", "ctrl-x", "ctrl-e"])
            if res is None:
                print("ccr: cancelled.")
                return
            key, ids = res
            picked = [index[i] for i in ids if i in index]
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
