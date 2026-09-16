# claude-codex-resume

A multi-select resume picker for **Claude Code** and **OpenAI Codex CLI** conversations — Windows first, macOS/Linux via tmux.

`ccr` shows your past sessions from both tools in one list, sorted by last message. Mark the ones you want with `Space`, press `Enter`, and each reopens as a tab of your current Windows Terminal window — in its original working directory, with the right resume command.

```
filter>                                            49/128 · 2 marked
↑↓ move · Space mark · Enter open · Ctrl+N new · Del delete · Esc cancel · type to filter · v0.15
  claude   run  API rate limiting bug          D:\repos\api-server
● codex     1h  migrate build to vite          D:\repos\web-app
  claude    3h  Docs restructure               D:\repos\docs
● claude   23h  dotfiles cleanup               ~\dotfiles
```

A green `●` = marked for opening. Sessions running right now in some terminal show a red `run` in the age column, so you don't open a second copy by accident.

## Why

If you work with several CLI agent sessions at once — one Windows Terminal window, one tab per conversation — reopening them after a reboot means `cd`-ing around and running `claude --resume` / `codex resume` once per tab. `ccr` does the whole thing in one go.

## Features

- **One merged list** for Claude Code + Codex, sorted by last activity. Claude ordering replicates the real `--resume` picker exactly (`min(last message timestamp, file mtime)`), titles follow the same precedence (custom title → AI title → first prompt).
- **Zero dependencies.** Pure PowerShell 7. No modules, no fzf, nothing to install (the optional Codex title overlay uses `winsqlite3.dll`, which ships with Windows).
- **Robust across tool updates.** It reads only the session files the tools themselves use to resume (`<claude config>\projects\*\*.jsonl`, `~\.codex\sessions\**\rollout-*.jsonl`), with bounded reads (transcripts can reach hundreds of MB) and defensive parsing — an unknown format degrades a row's title, never crashes the listing.
- **The first selection takes over the tab you ran `ccr` in** — the launcher tab never sits idle — and the rest open as tabs of the current window (`wt -w 0`), each in the session's recorded folder. Sessions already running elsewhere are flagged `run` (codex ones when started via `codex resume <id>`).
- **Cleared conversations are labeled.** `/clear` starts a new session that keeps the tab's name, so the conversation it replaced would look like a duplicate; ccr tags it with a yellow `(cleared)`. The link is exact where Claude Code recorded a Remote Control bridge id (the new session's first id equals the old one's last), with a same-folder-and-name fallback only for older sessions that predate those ids. Type `cleared` to list them all.
- Respects `CLAUDE_CONFIG_DIR` for relocated Claude data directories.
- **Several accounts, decided per conversation** — for Claude Code and Codex alike: `Ctrl+A` in the picker (or `ccr -AddAccount work`) turns it on: the dirs in use today become the `default` account, and each extra account gets a fresh data dir per tool with that tool's own login in it. The picker gains an account column, every session resumes under its own account, `Ctrl+N` asks which account a new conversation goes to, the account page adds/removes accounts (a removed account's sessions move to `default`) or turns the mode off again, and `Space` in the picker cycles the account a conversation opens under (it moves there). `-Root work` narrows the listing to one account, `-Accounts` shows who is logged in where. Details and examples: [docs/multi-account.md](docs/multi-account.md).
- Type to filter, `-WhatIf` to preview the exact `wt.exe` command line instead of launching.

## Requirements

- [PowerShell 7+](https://github.com/PowerShell/PowerShell) and [Claude Code](https://code.claude.com) and/or [Codex CLI](https://github.com/openai/codex)
- **Windows 10/11**: [Windows Terminal](https://github.com/microsoft/terminal) (multiple selections open as tabs)
- **macOS / Linux**: use the **native Python version** below (`ccr.py` — real iTerm2/Terminal.app tabs, fzf picker). The PowerShell script also runs there under `pwsh` (tmux windows for multiple selections).

## Install

One line, no admin rights (it copies the script next to your PowerShell profile and adds a load line to the profile — both idempotent):

```powershell
irm https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.ps1 | iex
```

From a clone: `.\install.ps1`. To update later, run **`ccr -Update`** — it downloads `main`'s script from GitHub over the installed copy, and open tabs pick the new version up by themselves on the next `ccr`. ccr also **checks by itself**: once an hour, when it starts, it asks GitHub for its channel's head; a newer version is installed before the run (with a message) and the picker's first line says `ccr updated v0.48 -> v0.49` for that run. `$env:CCR_AUTO_UPDATE = '0'` turns the check off; `ccr.state.json` next to `ccr.json` remembers the installed commit and the last check. To try what's coming next without touching your daily `ccr`: **`ccr -Channel test`** installs the `test` branch as a side-by-side copy and **`ccrtest`** runs it (same parameters and keys); `ccrtest -Update` refreshes it.

If the raw URL isn't reachable (e.g. a private fork), install through the authenticated [GitHub CLI](https://cli.github.com) instead — the installer then also downloads the script via `gh`:

```powershell
gh api repos/Cepstral/claude-codex-resume/contents/install.ps1 -H "Accept: application/vnd.github.raw" | Out-String | iex
```

<details>
<summary>Manual install</summary>

1. Get `Resume-CcSessions.ps1` (clone the repo, or download the raw file) and copy it next to your PowerShell profile:

   ```powershell
   Copy-Item .\Resume-CcSessions.ps1 (Split-Path -Parent $PROFILE)
   ```

2. If you downloaded it from the browser (rather than `git clone`), remove Windows' mark-of-the-web or the default execution policy will refuse to load it:

   ```powershell
   Unblock-File (Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1')
   ```

3. Add one line to your profile (`notepad $PROFILE`):

   ```powershell
   . (Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1')
   ```

4. Open a new tab and run `ccr`.

</details>

## Usage

```powershell
ccr                    # pick from the 200 most recent sessions (-Top 0 = all)
ccr kit                # open the picker already filtered to "kit"
ccr -n                 # new conversation: pick a folder, name it, go
ccr -n Kitchen         # same, name box prefilled with "Kitchen"
ccr -Tool codex        # only codex sessions
ccr -Top 100           # deeper history
ccr -NewWindow         # open the tabs in a fresh WT window instead
ccr -WhatIf            # print the wt.exe command line, launch nothing
```

| Key | Action |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | move |
| `Space` | mark / unmark |
| `Enter` | open all marked (or the highlighted row if none marked) |
| `Ctrl+N` | start a **new conversation**: a menu lists the folder ccr was started from first (`here`, known or not), then every folder past sessions used, most recently used first (with session counts, filterable). `Enter` on a folder asks **which tool** — `claude` or `codex` (`c` / `x` jump straight there) — then, for Claude, a session name (empty = auto title) and starts `claude --name <name>` in the current tab; Codex starts directly (no start-name flag — `/rename` inside). `Esc` steps back one level. `ccr -n` jumps straight to this menu, and `ccr -n TEXT` prefills the name box with TEXT. |
| `Del` | **permanently delete** the highlighted conversation, after a full-screen confirmation showing title, folder, dates, size and the last prompt/reply. Claude: removes the transcript and its sidecar folder; Codex: goes through `codex delete` so the catalog stays consistent. Running sessions are refused. No undo. |
| any character | filter (matches tool, title and path) |
| `Backspace` | edit filter |
| `Esc` | clear filter, then cancel |

Selected sessions reopen with `claude --resume <id>` / `codex resume <id>`, each starting in the session's recorded working directory (Claude requires it; for Codex it keeps the agent's working root correct). The first selection resumes in the current tab; with a single selection `ccr` is simply "resume here". `-NewWindow` sends everything to a fresh window and leaves the current tab alone.

## How it works

No database, no index: the picker enumerates the same on-disk session files the tools' own resume features read.

- **Claude Code** — `<config>\projects\<slug>\<uuid>.jsonl`. Reads a 16 KB head window (cwd, exclusion markers) and a 128 KB tail window (title lines, last timestamp) per file. Sidechain/daemon transcripts are excluded, matching the built-in picker.
- **Codex** — `~\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl`. Session id from the filename, last activity from file mtime, cwd and title from a bounded streaming read of the first lines.

### A note on Codex titles

Current Codex versions do **not** auto-generate conversation titles — what its own picker shows *is* the first user message. If you `/rename` a thread inside Codex, ccr picks the name up: it reads a private snapshot of Codex's catalog (`state_N.sqlite`, `threads.name` / distinct `title`) via Windows' built-in `winsqlite3.dll`, plus the legacy `session_index.jsonl` where older Codex versions stored thread names, and overlays those on the file-derived title — falling back silently whenever neither is readable. Renaming the Windows Terminal *tab* is invisible to Codex and to ccr; rename the thread inside Codex.

## macOS / Linux — native version

`ccr.py` is a port of the same tool with no PowerShell involved: Python 3 (stdlib only) for the data, [fzf](https://github.com/junegunn/fzf) for the picker, and **real terminal tabs** — iTerm2 and Terminal.app via AppleScript, tmux windows when you're inside tmux. Same files, same rules, same features as the PowerShell version (titles, `(cleared)` tags, `run` flags, Codex catalog titles, delete via `codex delete`, new-conversation flow, [multi-account mode](docs/multi-account.md), self-update), **plus Codex desktop-app support** (below). The two scripts carry the same version number and move in lockstep.

```sh
brew install fzf          # picker UI (python3 comes with the Xcode command line tools)
curl -fsSL https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.sh | sh
ccr
```

The installer puts `ccr` in `~/.local/bin` (tells you if that isn't on your PATH); afterwards `ccr --update` refreshes it from the main branch, and `ccr --channel test` installs the test branch side by side as `ccrtest` (`ccrtest --update` refreshes that one).

| | |
|---|---|
| `ccr` · `ccr kit` · `ccr -n [name]` · `ccr --tool codex` · `ccr --top 0` · `ccr --new-window` · `ccr --dry-run` | same meanings as the PowerShell flags |
| `ccr --root work` · `ccr --accounts` · `ccr --add-account work [--copy-settings]` · `ccr --remove-account work` · `ccr --disable-accounts` | multi-account mode, same meanings as the PowerShell flags; the config is `$CCR_CONFIG` or `~/.config/ccr/ccr.json` (same keys) |
| `ccr --update` · `ccr --channel test` | self-update from the release channels (see above). ccr also checks its channel once an hour at start and installs a newer version before the run, with a message and a one-run `ccr updated vX -> vY` line in the picker; `CCR_AUTO_UPDATE=0` turns that off (`~/.config/ccr/ccr.state.json` keeps the installed commit) |
| `ccr --terminal` | resume Codex **app** conversations with `codex resume` in a terminal instead of handing them back to the app |
| `ccr --tabs` | Terminal.app: extra sessions as tabs instead of windows (see below); iTerm2 and tmux use tabs either way |
| type | fuzzy filter (fzf); `cleared`, `run`, `app` and the account label are searchable words |
| `Tab` | mark / unmark (fzf convention — Space types into the filter). Marked rows take a green **●** in the gutter and the counter beside the prompt reads `49/128 · 2 marked`. |
| `Enter` | open the marked sessions (or the highlighted one); the first takes over the current terminal, the rest become tabs in iTerm2 and tmux, separate windows in Terminal.app |
| `Ctrl-O` | multi-account: open the marked sessions **under another account** — a menu asks which; each conversation moves into that account's dir first (the PowerShell picker does the same with Space cycling per row) |
| `Ctrl-A` | the account page: turn multi-account mode on, add an account (`+`), remove one (`Del`), copy the default account's settings to one (`Ctrl-S`), turn the mode off (`X`). Same key and page as the PowerShell picker |
| `Ctrl-N` | new conversation: folder → tool → name → account (when the tool has several). The folder list starts with **`here`** (the folder ccr runs in) and **`+ new folder`** (`Ctrl-O` jumps straight there) — type a path, `~` and relative paths welcome, and ccr offers to create it, so a brand-new project needs nothing prepared. The tool step offers **`codex app`** wherever the desktop app is installed. |
| `Del` | delete the highlighted conversation after a confirmation |
| `Esc` | cancel |

### Codex app vs Codex CLI

Codex conversations can start in two places, and they do not reopen the same way. Each rollout records who
opened it (`session_meta.originator`: `Codex Desktop` for the app, a `codex_cli_*` token for the CLI), so ccr
shows the difference in the tool column and routes each row to where it belongs:

```
  codex app   22h  Review flusso completo locale   ~/Documents/bnb-monitor
  codex        3d  migrate build to vite           ~/repos/web-app
```

- `codex app` → opened with the app's own deeplink, `codex://threads/<id>`, which focuses that thread in the
  Codex desktop app (the ChatGPT app, bundle id `com.openai.codex`). No terminal tab is spent, so a selection
  of app conversations leaves your current tab alone.
- `codex` → resumed as before, `codex resume <id>` in a tab, in the session's recorded folder.

This matters when the `codex` CLI is not on your `PATH` — the usual case when Codex arrived as the desktop app —
because `codex resume` would have nothing to run. `--terminal` forces the old behaviour for app conversations,
and `--dry-run` prints the exact `open codex://…` line instead of launching. On a machine with no URL handler
at all, app conversations fall back to `codex resume` with a notice.

Type `app` in the filter to list just those. Note that the `run` flag still only sees CLI sessions (it scans for
`codex resume <uuid>` processes); reopening a thread already open in the app just refocuses it, so nothing breaks.

**Starting one.** `Ctrl-N` → folder → `codex app` opens `codex://threads/new?path=<folder>`, and the app treats that
folder as the thread's workspace root — registering it as a project when it is not one yet. That is what makes the
`+ new folder` entry useful: type a path for a project that does not exist, let ccr create the directory, and the new
Codex conversation starts there with the project already set up. A first message typed at the prompt (or the trailing
text of `ccr -n TEXT`) is prefilled in the composer; nothing is sent for you.

The legend sits above the prompt rather than under it, one line of keys and one of filter hints, with the keys
picked out in colour and the rest kept quiet — and the two-step new-conversation menu labels which step you are on
and which folder you chose. A preview pane shows the full title, folder, how the row will open, last-used time and
session id of the highlighted row. Everything beyond the plain picker is version-gated, so an older fzf loses the
marked counter or the line highlight rather than refusing to start. Outside iTerm2/Terminal.app/tmux (e.g. a bare SSH shell) only the first selection can start, in the current terminal; ccr says so.

**Terminal.app gets windows, and asks for nothing.** Terminal's AppleScript dictionary has `do script`, which opens a
window — an app sending itself an Apple event needs no rights, so nothing is ever prompted. It has no verb for a new
*tab* at all: that takes a `Cmd-T` keystroke through System Events, a different app, which macOS gates behind
*Privacy & Security → Automation*. ccr does not go looking for that permission, so extra sessions open as separate
windows. Pass `--tabs` if you would rather have tabs and don't mind granting it once — and if you decline, or the
keystroke fails for any other reason, ccr says what happened and opens the window anyway. iTerm2 creates tabs
natively and asks for nothing, and so do tmux, `--new-window`, and Codex conversations sent to the desktop app.

*The Python port is verified against the same session data as the PowerShell version. The Terminal.app path has since
been exercised on a Mac; the iTerm2 scripting is still written from the dictionary alone — report anything odd with
`ccr --dry-run`, which prints the exact AppleScript it would run.*

## License

[MIT](LICENSE)
