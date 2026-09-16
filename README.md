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
- **Several accounts, decided per conversation** — for Claude Code and Codex alike: `ccr -AddAccount work` creates a data dir per tool, runs each tool's own login in it, and records it; the picker gains an account column, every session resumes under its own account, `Ctrl+N` asks which account a new conversation goes to, `Ctrl+M` lets you open any conversation under another account (it moves there), `-Root work` narrows to one, `-Accounts` shows who is logged in where. Details and examples: [docs/multi-account.md](docs/multi-account.md).
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

From a clone: `.\install.ps1`. To update later, just re-run it (open tabs pick the new version up by themselves on the next `ccr`).

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
| `Ctrl+N` | start a **new conversation**: a menu lists every folder past sessions used, most recently used first (with session counts, filterable). `Enter` on a folder asks **which tool** — `claude` or `codex` (`c` / `x` jump straight there) — then, for Claude, a session name (empty = auto title) and starts `claude --name <name>` in the current tab; Codex starts directly (no start-name flag — `/rename` inside). `Esc` steps back one level. `ccr -n` jumps straight to this menu, and `ccr -n TEXT` prefills the name box with TEXT. |
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

`ccr.py` is a port of the same tool with no PowerShell involved: Python 3 (stdlib only) for the data, [fzf](https://github.com/junegunn/fzf) for the picker, and **real terminal tabs** — iTerm2 and Terminal.app via AppleScript, tmux windows when you're inside tmux. Same files, same rules, same features as the PowerShell version (titles, `(cleared)` tags, `run` flags, Codex catalog titles, delete via `codex delete`, new-conversation flow).

```sh
brew install fzf          # picker UI (python3 comes with the Xcode command line tools)
curl -fsSL https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.sh | sh
ccr
```

The installer puts `ccr` in `~/.local/bin` (tells you if that isn't on your PATH); re-run it to update.

| | |
|---|---|
| `ccr` · `ccr kit` · `ccr -n [name]` · `ccr --tool codex` · `ccr --top 0` · `ccr --new-window` · `ccr --dry-run` | same meanings as the PowerShell flags |
| type | fuzzy filter (fzf); `cleared` and `run` are searchable words |
| `Tab` | mark / unmark (fzf convention — Space types into the filter) |
| `Enter` | open the marked sessions (or the highlighted one); the first takes over the current terminal, the rest become tabs |
| `Ctrl-N` | new conversation: folder → tool → name |
| `Del` | delete the highlighted conversation after a confirmation |
| `Esc` | cancel |

A preview pane shows the full title, folder, last-used time and session id of the highlighted row. Outside iTerm2/Terminal.app/tmux (e.g. a bare SSH shell) only the first selection can start, in the current terminal; ccr says so.

*The Python port is verified against the same session data as the PowerShell version, but the macOS tab scripting (osascript) was written from the iTerm2/Terminal.app dictionaries and not yet exercised on a Mac — report anything odd with `ccr --dry-run`, which prints the exact AppleScript it would run.*

## License

[MIT](LICENSE)
