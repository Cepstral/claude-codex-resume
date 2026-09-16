#!/usr/bin/env python3
"""ccr - multi-select resume picker for Claude Code + Codex CLI sessions.

Native macOS/Linux port of Resume-CcSessions.ps1 (same data sources, same
rules), with fzf as the picker and real terminal tabs (iTerm2, Terminal.app)
or tmux windows as the launch backend. Python 3.9+, stdlib only; fzf for UI.

Keys in the picker (fzf conventions): type to fuzzy-filter, Tab marks,
Enter opens, Ctrl-N new conversation, Del deletes, Esc cancels.
"""
import argparse
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "0.22"
UUID_IN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile("^" + UUID_IN + "$")
HOME = Path.home()
# CCR_FZF may be a full command (e.g. "fzf-tmux -p"); split like a shell would.
FZF = shlex.split(os.environ.get("CCR_FZF") or "fzf")

ORANGE, CYAN, RED, YELLOW, DIM, RESET = "\033[38;5;208m", "\033[36m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# ----------------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------------
def claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


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
                 "origin")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.cleared = bool(self.cleared)
        self.origin = self.origin or "cli"

    @property
    def key(self):
        return f"{self.tool}|{self.id}"


# ----------------------------------------------------------------------------
# claude enumerator
# ----------------------------------------------------------------------------
def claude_running() -> dict:
    """sessionId -> live claude pid (stale pid files filtered out)."""
    m = {}
    d = claude_root() / "sessions"
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


def claude_sessions() -> list:
    root = claude_root() / "projects"
    if not root.is_dir():
        return []
    running = claude_running()
    cache = {}

    def history():
        if "t" not in cache:
            cache["t"] = history_table(claude_root() / "history.jsonl", "sessionId")
        return cache["t"]

    out = []
    for slug in sorted(root.iterdir()):
        if not slug.is_dir():
            continue
        for f in slug.glob("*.jsonl"):  # depth 1 only: subfolders hold subagent transcripts
            if not UUID_RE.match(f.stem):
                continue
            try:
                s = claude_one(f, running, history)
                if s:
                    out.append(s)
            except Exception as e:
                if os.environ.get("CCR_DEBUG"):
                    print(f"ccr: skipping {f}: {e}", file=sys.stderr)
    mark_cleared(out)
    return out


def claude_one(f: Path, running: dict, history):
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
                   tail_bridge=bt[-1] if bt else (bh[-1] if bh else None))


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


def codex_title_map() -> dict:
    """Best-effort curated titles: codex catalog (state_N.sqlite: /rename
    name, or a title distinct from the first message) plus the legacy
    session_index.jsonl. Reads a private temp copy of the DB."""
    m = {}
    root = codex_root()
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


def codex_sessions() -> list:
    root = codex_root() / "sessions"
    if not root.is_dir():
        return []
    running = codex_running()
    curated = codex_title_map()
    cache = {}

    def history():
        if "t" not in cache:
            cache["t"] = history_table(codex_root() / "history.jsonl", "session_id")
        return cache["t"]

    out = []
    for f in root.rglob("rollout-*.jsonl"):
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
                           origin=origin))
    return out


# ----------------------------------------------------------------------------
# fzf picker
# ----------------------------------------------------------------------------
def run_fzf(rows, header, query="", multi=True, expect=None, preview=True, prompt="filter> "):
    """Rows are '<id>\\t<display>\\t<preview>'. Returns (key, [ids]) or None on
    Esc/Ctrl-C; key is '' for Enter or one of `expect`."""
    if not shutil.which(FZF[0]):
        sys.exit("ccr: fzf not found - install it first (brew install fzf)")
    args = FZF + ["--ansi", "--no-sort", "--layout=reverse", "--delimiter=\t", "--with-nth=2",
            "--header=" + header, "--prompt=" + prompt, "--info=inline"]
    if multi:
        args.append("--multi")
    if query:
        args += ["--query", query]
    if expect:
        args += ["--expect", ",".join(expect)]
    if preview:
        args += ["--preview", "printf '%b\\n' {3}", "--preview-window=down,4,wrap"]
    p = subprocess.run(args, input="\n".join(rows) + "\n", text=True, stdout=subprocess.PIPE)
    if p.returncode not in (0, 1):
        return None
    lines = p.stdout.splitlines()
    key = ""
    if expect:
        key = lines[0] if lines else ""
        lines = lines[1:]
    return key, [l.split("\t", 1)[0] for l in lines if l.strip()]


def session_rows(sessions, index):
    rows = []
    for i, s in enumerate(sessions):
        index[str(i)] = s
        color = ORANGE if s.tool == "claude" else CYAN
        age = f"{RED}{'run':>6}{RESET}" if s.running else f"{fmt_age(s.last):>6}"
        title = s.title if len(s.title) <= 50 else s.title[:49] + "…"
        tag = f" {YELLOW}(cleared){RESET}" if s.cleared else ""
        # Tool column, padded on the plain text (the colors are zero-width).
        app = s.origin == "app"
        tool = f"{color}{s.tool}{RESET}" + (f"{DIM} app{RESET}" if app else "")
        tool += " " * max(1, 10 - len(s.tool) - (4 if app else 0))
        disp = f"{tool}{age}  {title}{tag}  {DIM}{fmt_cwd(s.cwd, 40)}{RESET}"
        extra = ((" cleared" if s.cleared else "") + (" run" if s.running else "")
                 + (" app" if app else ""))  # filter words
        how = (f"opens in: Codex app (codex://threads/{s.id})" if app
               else f"opens in: terminal ({resume_argv(s)[0]} …)")
        prev = (f"{s.tool} · {s.title}\\nfolder: {s.cwd}\\n{how}\\nlast: "
                f"{s.last.astimezone():%Y-%m-%d %H:%M}   id: {s.id}").replace("\t", " ")
        rows.append(f"{i}\t{disp}{DIM}{extra}{RESET}\t{prev}")
    return rows


HINT = ("↑↓ move · Tab mark · Enter open · Ctrl-N new · Del delete · "
        "Esc cancel · type to filter · ccr v" + VERSION)


# ----------------------------------------------------------------------------
# delete
# ----------------------------------------------------------------------------
def remove_session_data(s: Session) -> bool:
    if s.tool == "codex":
        try:
            if subprocess.run(["codex", "delete", s.id], capture_output=True).returncode == 0:
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
    print(f"    last used: {s.last.astimezone():%Y-%m-%d %H:%M}  ({fmt_age(s.last)})")
    if size:
        print(f"    size:      {size}")
    if preview:
        print(f"    {'last prompt' if s.tool == 'claude' else 'last reply '}: {DIM}{preview}{RESET}")
    print(f"\n  {DIM}removed from disk, no undo (codex: via 'codex delete'){RESET}")
    if s.tool == "codex" and s.origin == "app" and not shutil.which("codex"):
        print(f"  {YELLOW}the Codex app keeps its own copy: without the 'codex' CLI this only drops "
              f"the transcript, the thread stays in the app{RESET}")
    try:
        ans = input(f"  {RED}[y]{RESET} delete    {DIM}anything else: cancel{RESET} > ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() == "y"


# ----------------------------------------------------------------------------
# launch backends
# ----------------------------------------------------------------------------
def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_tab(cwd: str, cmd: str, new_window: bool, dry: bool) -> bool:
    """Run cmd in a new terminal surface: a tmux window when inside tmux,
    else an iTerm2 / Terminal.app tab (or window). False = no backend."""
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
        if new_window:
            script = ('tell application "iTerm2"\n  tell current session of (create window with default profile) '
                      f'to write text {applescript_str(shell_cmd)}\nend tell')
        else:
            script = ('tell application "iTerm2"\n  tell current window\n    tell current session of '
                      f'(create tab with default profile) to write text {applescript_str(shell_cmd)}\n'
                      '  end tell\nend tell')
    elif tp == "Apple_Terminal":
        if new_window:
            script = f'tell application "Terminal" to do script {applescript_str(shell_cmd)}'
        else:
            script = ('tell application "Terminal"\n  activate\n  tell application "System Events" to keystroke "t" '
                      f'using command down\n  delay 0.3\n  do script {applescript_str(shell_cmd)} in selected tab of '
                      'front window\nend tell')
    else:
        return False
    if dry:
        print("  osascript:\n    " + script.replace("\n", "\n    "))
    else:
        subprocess.run(["osascript", "-e", script], capture_output=True)
    return True


def exec_inline(cwd: str, argv: list, title: str, dry: bool):
    """Hand this terminal over to the agent (the shell gets it back on exit)."""
    if dry:
        print(f"this tab: {' '.join(shlex.quote(a) for a in argv)}   (cd {cwd})")
        return
    os.chdir(cwd)
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        sys.exit(f"ccr: cannot start {argv[0]}: {e}")


def resume_argv(s: Session) -> list:
    return ["claude", "--resume", s.id] if s.tool == "claude" else ["codex", "resume", s.id]


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


def launch(picked: list, new_window: bool, dry: bool, terminal: bool = False):
    opener = [] if terminal else url_opener()
    app_list, launch_list = [], []
    for s in picked:
        if not UUID_RE.match(s.id):
            print(f"ccr: skipping '{s.title}' - unexpected session id", file=sys.stderr)
            continue
        # Desktop-app threads go back to the app, not to a terminal tab.
        if s.tool == "codex" and s.origin == "app" and not terminal:
            if opener:
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
        launch_list.append((s, cwd, resume_argv(s)))
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
    # First terminal selection takes over this terminal; the rest open as tabs.
    inline, tabs = (None, launch_list) if new_window else (launch_list[0], launch_list[1:])
    for s, cwd, argv in tabs:
        if not open_tab(cwd, " ".join(shlex.quote(a) for a in argv), new_window, dry):
            print("ccr: opening several sessions needs iTerm2, Terminal.app or tmux - only the first starts.",
                  file=sys.stderr)
            break
    if inline:
        s, cwd, argv = inline
        exec_inline(cwd, argv, f"{s.tool} · {s.title}", dry)


# ----------------------------------------------------------------------------
# new conversation (Ctrl-N / -n)
# ----------------------------------------------------------------------------
def new_conversation(sessions: list, initial_name: str, dry: bool) -> bool:
    groups = {}
    for s in sessions:
        g = groups.setdefault(s.cwd.lower(), {"path": s.cwd, "last": s.last, "count": 0})
        g["count"] += 1
        if s.last > g["last"]:
            g["last"] = s.last
    folders = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
    index, rows = {}, []
    for i, g in enumerate(folders):
        index[str(i)] = g
        rows.append(f"{i}\t{fmt_age(g['last']):>6}  {DIM}{g['count']:>3}×{RESET}  "
                    f"{fmt_cwd(g['path'], 70)}\t{g['path']}")
    res = run_fzf(rows, "new conversation: pick a folder · Enter next · Esc back",
                  multi=False, prompt="new session in> ")
    if not res or not res[1]:
        return False
    folder = index[res[1][0]]["path"]
    tools = [f"0\t{ORANGE}claude{RESET}   {DIM}asks for a session name{RESET}\tclaude",
             f"1\t{CYAN}codex{RESET}    {DIM}no start name - /rename inside{RESET}\tcodex"]
    res = run_fzf(tools, "tool for the new conversation · Enter choose · Esc back",
                  multi=False, preview=False, prompt="tool> ")
    if not res or not res[1]:
        return False
    tool = "codex" if res[1][0] == "1" else "claude"
    name = ""
    if tool == "claude":
        try:
            hint = f" [{initial_name}]" if initial_name else ""
            name = input(f"session name for claude (empty = auto title){hint}> ").strip() or initial_name
        except (EOFError, KeyboardInterrupt):
            return False
    if not Path(folder).is_dir():
        print(f"ccr: folder no longer exists: {folder}", file=sys.stderr)
        return False
    argv = ["claude", "--name", name] if (tool == "claude" and name) else [tool]
    exec_inline(folder, argv, f"{tool} · {name or 'new'}", dry)
    return True


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
    ap.add_argument("--terminal", action="store_true",
                    help="resume Codex desktop-app conversations with 'codex resume' in a terminal "
                         "instead of handing them back to the app")
    ap.add_argument("--dry-run", action="store_true", help="print what would be launched, launch nothing")
    ap.add_argument("--version", action="version", version="ccr " + VERSION + " (python)")
    a = ap.parse_args()
    query = " ".join(a.query).strip()

    sessions = []
    if a.tool in ("claude", "all"):
        sessions += claude_sessions()
    if a.tool in ("codex", "all"):
        sessions += codex_sessions()
    if not sessions:
        sys.exit("ccr: no sessions found.")
    sessions.sort(key=lambda s: s.last, reverse=True)
    if a.top > 0:
        sessions = sessions[: a.top]

    if a.new:
        if not new_conversation(sessions, query, a.dry_run):
            print("ccr: cancelled.")
        return

    while True:
        index = {}
        rows = session_rows(sessions, index)
        res = run_fzf(rows, HINT, query=query, expect=["del", "ctrl-n"])
        if res is None:
            print("ccr: cancelled.")
            return
        key, ids = res
        picked = [index[i] for i in ids if i in index]
        if key == "ctrl-n":
            if not new_conversation(sessions, "", a.dry_run):
                print("ccr: cancelled.")
            return
        if key == "del":
            if not picked:
                continue
            victim = picked[0]
            if victim.running:
                print(f"ccr: '{victim.title}' is running right now - close that tab first.")
                try:
                    input("  (Enter to continue)")
                except (EOFError, KeyboardInterrupt):
                    pass
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
        launch(picked, a.new_window, a.dry_run, a.terminal)
        return


if __name__ == "__main__":
    main()
