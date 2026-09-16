#!/usr/bin/env python3
"""ccr - multi-select resume picker for Claude Code + Codex CLI sessions.

Native macOS/Linux port of Resume-CcSessions.ps1 (same data sources, same
rules, same features - the two are kept aligned change for change), with fzf
as the picker and real terminal tabs (iTerm2, Terminal.app) or tmux windows
as the launch backend. Python 3.9+, stdlib only; fzf for UI.

Keys in the picker (fzf conventions): type to fuzzy-filter, Tab marks,
Enter opens, Ctrl-N new conversation, Ctrl-A accounts, Ctrl-O open the
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
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Shown in the picker hint line; bumped together with $script:CcrVersion in
# Resume-CcSessions.ps1 - the two scripts move in lockstep.
VERSION = "0.47"
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
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            k32.CloseHandle(h)
            return True
        return False
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
                 "origin", "root", "root_path", "target_root")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.cleared = bool(self.cleared)
        self.origin = self.origin or "cli"
        self.root = self.root or ""

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
        return "(dir missing)"
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
        if label in m:
            print(f"ccr: {t} account '{label}' already configured - skipping", file=sys.stderr)
            continue
        def_path = default_root(t).path
        d = os.path.join(os.path.dirname(def_path), f"{os.path.basename(def_path)}-{label}")
        reused = os.path.isdir(d)
        Path(d).mkdir(parents=True, exist_ok=True)
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
            print(f"{YELLOW}ccr: {t} account '{label}' -> {d}  - existing dir, already logged in"
                  f"{' as ' + already if already else ''}; no login needed{RESET}")
            continue
        print(f"{YELLOW}ccr: {t} account '{label}' -> {d}  - starting the {t} login flow in that dir{RESET}")
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
        running = [s for s in sessions if s.running]
        if running:
            raise RuntimeError(f"ccr: {len(running)} {t} session(s) of '{label}' are running - close them first")
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
def claude_running(root_path: str) -> dict:
    """sessionId -> live claude pid (stale pid files filtered out)."""
    m = {}
    d = Path(root_path) / "sessions"
    if not d.is_dir():
        return m
    for f in d.glob("*.json"):
        if not f.stem.isdigit():
            continue
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
            pid, sid = int(o.get("pid") or 0), o.get("sessionId")
            if sid and pid and pid_alive(pid):
                m[sid] = pid
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

    return Session(tool="claude", id=sid, title=title, cwd=cwd, last=last,
                   running=sid in running, pid=running.get(sid), source=str(f),
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
        # Accounts = the union of labels across both tools, in config order.
        self.accounts = []
        for r in self.claude + self.codex:
            if r.label and r.label not in self.accounts:
                self.accounts.append(r.label)
        self.quick = {}   # "tool|label" -> email from the dir's own files

    def roots(self, tool: str, all_: bool = False) -> list:
        if tool == "codex":
            return self.all_codex if all_ else self.codex
        return self.all_claude if all_ else self.claude

    def root_var(self, tool: str):
        """The env var to set for a launch, or None when the tool has one dir."""
        return ROOT_VAR[tool] if self.multi[tool] else None

    def is_default(self, label: str) -> bool:
        return any(r.label == label and r.default for r in self.claude + self.codex)

    def legend(self) -> list:
        """One line per account: number, label, and per tool the dir and the
        email logged in there. Columns aligned across accounts; dirs are
        dropped when the lines would not fit."""
        width = shutil.get_terminal_size((100, 24)).columns - 4
        lbl_w = max(len(acct_label(a, self.is_default(a))) for a in self.accounts)
        cells, col_w = {}, {}
        for t in ("claude", "codex"):
            col_w[t] = [0, 0]
            for a in self.accounts:
                r = next((x for x in self.roots(t) if x.label == a), None)
                if not r:
                    continue
                qk = f"{t}|{a}"
                if qk not in self.quick:
                    self.quick[qk] = quick_identity(t, r.path)
                cell = (fmt_cwd(r.path, 28), self.quick[qk] or "not logged in")
                cells[qk] = cell
                col_w[t] = [max(col_w[t][0], len(cell[0])), max(col_w[t][1], len(cell[1]))]
        full = 4 + lbl_w + 2 + sum(len(t) + 1 + col_w[t][0] + 1 + col_w[t][1] + 2
                                   for t in ("claude", "codex") if col_w[t][1])
        with_dirs = full <= width
        lines = [f"{BOLD}{MAGENTA}Multi-account mode active.{RESET}"]
        for i, a in enumerate(self.accounts):
            parts = []
            for t in ("claude", "codex"):
                if not col_w[t][1]:
                    continue
                d, who = cells.get(f"{t}|{a}", ("", ""))
                if with_dirs:
                    parts.append(f"{TOOL_COLOR[t]}{t}{RESET} {d:<{col_w[t][0]}} {DIM}{who:<{col_w[t][1]}}{RESET}")
                else:
                    parts.append(f"{TOOL_COLOR[t]}{t}{RESET} {DIM}{who:<{col_w[t][1]}}{RESET}")
            lines.append(f"  {BOLD}{MAGENTA}{i + 1}{RESET} {MAGENTA}{acct_label(a, self.is_default(a)):<{lbl_w}}{RESET}"
                         f"  {'  '.join(parts)}")
        return lines


def session_rows(sessions, index, ctx: Ctx):
    rows = []
    root_w = min(14, max((len(acct_label(r.label, r.default)) for r in ctx.claude + ctx.codex), default=0)) \
        if ctx.multi_root else 0
    for i, s in enumerate(sessions):
        index[str(i)] = s
        color = TOOL_COLOR[s.tool]
        age = f"{RED}{'run':>6}{RESET}" if s.running else f"{fmt_age(s.last):>6}"
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
        disp = f"{tool}{acct}{age}  {title}{tag}  {DIM}{fmt_cwd(s.cwd, 40)}{RESET}"
        extra = ((" cleared" if s.cleared else "") + (" run" if s.running else "")
                 + (" app" if app else ""))  # filter words
        how = (f"opens in: Codex app (codex://threads/{s.id})" if app
               else f"opens in: terminal ({resume_argv(s)[0]} …)")
        where = f"\\naccount: {s.root} ({fmt_cwd(s.root_path, 40)})" if ctx.multi_root else ""
        prev = (f"{s.tool} · {s.title}\\nfolder: {s.cwd}{where}\\n{how}\\nlast: "
                f"{s.last.astimezone():%Y-%m-%d %H:%M}   id: {s.id}").replace("\t", " ")
        rows.append(f"{i}\t{disp}{DIM}{extra}{RESET}\t{prev}")
    return rows


def picker_hint(ctx: Ctx) -> str:
    lines = ctx.legend() if ctx.multi_root else []
    keys = [("↑↓", "move"), ("Tab", "mark"), ("Enter", "open")]
    if ctx.multi_root:
        keys.append(("Ctrl-O", "open under another account"))
    keys += [("Ctrl-N", "new"), ("Ctrl-A", "accounts"), ("Del", "delete"), ("Esc", "cancel")]
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
        q = quick_identity(t, r.path)
        who[f"{t}|{r.label}"] = q or "not logged in"
    lbl_w = max(7, max(len(acct_label(r.label, r.default)) for _, r in entries))
    dir_w = min(40, max(len(fmt_cwd(r.path, 40)) for _, r in entries))
    for i, (t, r) in enumerate(entries):
        disp = (f"{MAGENTA}{acct_label(r.label, r.default):<{lbl_w}}{RESET}  {TOOL_COLOR[t]}{t:<6}{RESET}  "
                f"{fmt_cwd(r.path, 40):<{dir_w}}  {DIM}{who[f'{t}|{r.label}']}{RESET}")
        rows.append(f"{i}\t{disp}\t{r.path}")
    rows.append(f"add\t{GREEN}+ add an account{RESET}  {DIM}tool, label, then that tool's login{RESET}\tadd")
    rows.append(f"off\t{RED}X turn multi-account mode off{RESET}  {DIM}every session goes to the '{DEFAULT_LABEL}' account{RESET}\toff")
    header = (f"{BOLD}Accounts{RESET}  {DIM}{CONFIG_PATH}{RESET}\n"
              + hint(("Enter", "choose"), ("Del", "remove"), ("Ctrl-S", "copy settings from default"), ("Esc", "back"))
              + f"\n{DIM}A session always resumes under the account whose dir it lives in. "
              f"In the picker, Ctrl-O opens the marked rows under another account.{RESET}")
    res = run_fzf(rows, header, multi=False, preview=False, prompt="accounts> ", expect=["del", "ctrl-s"])
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
        elif act["action"] == "remove":
            remove_account(act["label"], act["tool"])
        elif act["action"] == "disable":
            disable_multi_account()
    except Exception as e:
        print(f"ccr: {act['action']} failed: {e}", file=sys.stderr)


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


def resume_argv(s: Session) -> list:
    return ["claude", "--resume", s.id] if s.tool == "claude" else ["codex", "resume", s.id]


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
            lbl = choose_account([(r.label, fmt_cwd(r.path, 60), r.default) for r in roots],
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


def update_self(channel: str, target: str, dry: bool):
    branch = CHANNELS.get(channel)
    if not branch:
        sys.exit(f"ccr: unknown channel '{channel}' (known: {', '.join(CHANNELS)})")
    # GitHub's raw CDN serves a branch URL from cache for minutes after a
    # push (and ignores query strings), so resolve the branch head through
    # the API - never cached - and fetch the file by commit, which is
    # immutable. Falls back to the branch URL when the API is unreachable.
    sha = None
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{branch}",
                                     headers={"User-Agent": "ccr"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sha = json.loads(r.read().decode("utf-8")).get("sha")
    except Exception:
        pass
    ref = sha or branch
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/ccr.py"
    at = f"branch {branch} @ {sha[:7]}" if sha else f"branch {branch} (head unknown, raw URL may lag)"
    if dry:
        print(f"would download {at} -> {target}")
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ccr"})
        with urllib.request.urlopen(req, timeout=30) as r:
            src = r.read().decode("utf-8")
    except Exception as e:
        sys.exit(f"ccr: download failed: {e}")
    try:
        compile(src, "ccr.py", "exec")
    except SyntaxError as e:
        sys.exit(f"ccr: the downloaded file does not parse ({e}) - nothing replaced")
    m = re.search(r'VERSION = "([^"]+)"', src)
    new_ver = m.group(1) if m else "?"
    had = file_version(target)
    tmp = target + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    cmd = "ccrtest" if channel == "test" else "ccr"
    print(f"{GREEN}ccr: channel '{channel}' ({at}) v{had} -> v{new_ver} at {target}. Run: {cmd}{RESET}")
    if new_ver == had:
        print("ccr: same version as before - nothing newer on that branch.")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
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
    a = ap.parse_args()
    query = " ".join(a.query).strip()

    if a.update or a.channel:
        me = self_path()
        ch = a.channel or channel_of(me)
        update_self(ch, channel_path(ch, me), a.dry_run)
        return
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

        restart = False
        while not restart:
            index = {}
            rows = session_rows(sessions, index, ctx)
            res = run_fzf(rows, picker_hint(ctx), query=query, expect=["del", "ctrl-n", "ctrl-a", "ctrl-o"])
            if res is None:
                print("ccr: cancelled.")
                return
            key, ids = res
            picked = [index[i] for i in ids if i in index]
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
                continue
            if key == "del":
                if not picked:
                    continue
                victim = picked[0]
                if victim.running:
                    print(f"ccr: '{victim.title}' is running right now - close that tab first.")
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
                labels = []
                for s in picked:
                    for r in ctx.roots(s.tool, all_=True):
                        if r.label and r.label not in labels:
                            labels.append(r.label)
                menu = [(l, "  ".join(f"{t} {fmt_cwd(r.path, 30)}" for t in ("claude", "codex")
                                      for r in ctx.roots(t, all_=True) if r.label == l),
                         ctx.is_default(l)) for l in labels]
                n = len(picked)
                lbl = choose_account(menu, f"open {n} conversation{'' if n == 1 else 's'} under which account?")
                if lbl is None:
                    continue
                for s in picked:
                    s.target_root = lbl
            launch(picked, a.new_window, a.dry_run, ctx, a.terminal, a.tabs)
            return


if __name__ == "__main__":
    main()
