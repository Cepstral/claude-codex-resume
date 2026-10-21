# claude-codex-resume

**One picker for your Claude Code and Codex CLI conversations.** `ccr` lists the past sessions of both tools in one list, most recent first. Mark the ones you want, press `Enter`, and each one reopens in a terminal tab of its own — in the folder it was started in, with the right resume command (`claude --resume <id>` / `codex resume <id>`).

| | Windows | macOS / Linux |
|---|---|---|
| Script | `Resume-CcSessions.ps1` | `ccr.py` |
| Needs | [PowerShell 7+](https://github.com/PowerShell/PowerShell), [Windows Terminal](https://github.com/microsoft/terminal) | Python 3 (stdlib only), [fzf](https://github.com/junegunn/fzf) |
| Extra conversations open as | tabs of the current Windows Terminal window | iTerm2 tabs, Terminal.app windows, tmux windows |

Both need [Claude Code](https://code.claude.com) and/or [Codex CLI](https://github.com/openai/codex). The two scripts are the same tool: same version number, same features, same config file (`ccr.json`), same flags. A few keys differ because fzf cannot read them — see [Keys](#keys). (The PowerShell script also runs on macOS/Linux under `pwsh`, with tmux windows, but `ccr.py` is the native choice there.)

```
filter>                                                       49/128 · 2 marked
  claude  ~/.claude  me@example.com  5h 9% → 00:19 · week 50% → Wed 7 09:59
  codex   ~/.codex   me@example.com  5h 0% · week 2% → Wed 7 07:57
↑↓ move · Tab mark · Enter open · Ctrl+N new · Ctrl+G/F1 guide: all the keys · Esc cancel · type to filter
  claude    run  API rate limiting bug          ~/repos/api-server
● codex      1h  migrate build to vite          ~/repos/web-app
  claude     3h  Docs restructure               ~/repos/docs
● claude    23h  dotfiles cleanup               ~/dotfiles
```

On top, one line per tool (and per account): who is logged in and how much of the usage limits is used. A green `●` is a marked row. A red `run` in the age column means the conversation is running right now on this PC, so you don't open a second copy by accident.

## Why

If you work with several CLI agent sessions at once — one terminal window, one tab per conversation — reopening them after a reboot means `cd`-ing around and running `claude --resume` / `codex resume` once per tab. `ccr` does the whole thing in one go.

## Install

### Windows

One line, no admin rights. It copies the script next to your PowerShell profile and adds a load line to the profile (both idempotent):

```powershell
irm https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.ps1 | iex
```

From a clone: `.\install.ps1`. If the raw URL isn't reachable (e.g. a private fork), go through the authenticated [GitHub CLI](https://cli.github.com) — the installer then downloads the script via `gh` too:

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

### macOS / Linux

```sh
brew install fzf          # the picker UI (python3 comes with the Xcode command line tools)
curl -fsSL https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.sh | sh
ccr
```

The installer puts `ccr` in `~/.local/bin` and tells you if that folder isn't on your PATH.

### Updates

ccr **updates itself**: on the first run in a shell, and then once an hour, it asks GitHub for the head of its channel. A newer version is installed before the run, with a message, and the picker's first line says `ccr updated v0.48 -> v0.49` for that run. It never downgrades, and a copy that sits in a git checkout is never auto-updated. On Windows, tabs that are already open pick the new version up on their next `ccr`.

| | Windows | macOS / Linux |
|---|---|---|
| update now | `ccr -Update` | `ccr --update` |
| try the `test` branch, side by side | `ccr -Channel test`, then run `ccrtest` | `ccr --channel test`, then run `ccrtest` |
| update the test copy | `ccrtest -Update` | `ccrtest --update` |
| turn the automatic check off | `$env:CCR_AUTO_UPDATE = '0'` | `CCR_AUTO_UPDATE=0` |
| where the installed commit and last check are kept | `%LOCALAPPDATA%\ccr\ccr.state.json` | `~/.cache/ccr/ccr.state.json` |

The state file is per machine, never in a synced folder, so a second PC does not think it is up to date. `ccrtest` takes the same parameters and keys as `ccr`.

## Usage

Every flag works in both spellings on both systems: the PowerShell one (`-Update`, `-Root work`, `-WhatIf`, any case) and the GNU one (`--update`, `--root work`, `--dry-run`).

```powershell
ccr                    # pick from the 200 most recent sessions (-Top 0 = all)
ccr kit                # the picker, already filtered to "kit"
ccr -Tool codex        # only codex sessions
ccr -Top 100           # deeper history
ccr -n                 # new conversation: pick a folder, name it, go
ccr -n Kitchen         # same, name box prefilled with "Kitchen"
ccr -NewWindow         # open the tabs in a fresh window instead of the current one
ccr -WhatIf            # print what would run, launch nothing (--dry-run)
ccr -Usage             # start with the token-usage column on
ccr -Panel "why does the import skip rows?"   # a MAP: 2-4 fresh agents discuss one question until they agree (quote it; -MAP too)
ccr -Panel -Continue   # go on with a saved MAP: the same agents, in their own sessions
ccr -Install codex     # install or update codex with its official installer (-Install all: both tools)
ccr -AddAccount work   # add a second account (see "Several accounts")
ccr -Root work         # list only the conversations of the account "work"
ccr -Accounts          # which accounts exist, and who is logged in where
```

macOS / Linux only: `--terminal` (resume Codex **app** conversations with `codex resume` in a terminal instead of handing them to the app) and `--tabs` (Terminal.app: extra sessions as tabs instead of windows). See [macOS / Linux notes](#macos--linux-notes).

## Keys

The line above the list names only the essentials. **`Ctrl+G`** (or **`F1`**) opens the **guide**: every key with a short explanation, what the signs on a row mean and what the table on top shows. `F1` is there for when another program takes `Ctrl+G` before the terminal sees it — on a Mac, Grammarly's AI Chat does (on a Mac keyboard `F1` is `fn`+`F1`).

| What | Windows | macOS / Linux |
|---|---|---|
| move | `↑` `↓` `PgUp` `PgDn` `Home` `End` | `↑` `↓` `PgUp` `PgDn` |
| filter (`Space` separates words; every word must match) | type; `Backspace` edits | type (fuzzy) |
| mark / unmark a row | `Tab` | `Tab` |
| [open](#opening-enter) the marked rows, or the highlighted one | `Enter` | `Enter` |
| open them with a [model and an effort](#model-and-effort) | `Shift+Enter` or `Ctrl+E` | `Ctrl-E` |
| [new conversation](#new-conversation) | `Ctrl+N` | `Ctrl-N` |
| [MAP](#map-the-multi-agent-panel), the multi-agent panel | `Ctrl+P` | `Ctrl-P` |
| [accounts page](#several-accounts) | `Ctrl+A` | `Ctrl-A` |
| open under [another account](#several-accounts) | `Tab` cycles the account of the row | `Tab` cycles it too (fzf 0.39+; older fzf: `Tab` marks, `Ctrl-O` picks the account) |
| [token usage](#usage-limits-and-token-usage): column / details | `Ctrl+K` / `Ctrl+J` | `Ctrl-K` / `Ctrl-J` |
| ask the [usage limits](#usage-limits-and-token-usage) again | `Ctrl+L` | `Ctrl-L` |
| [find the window](#running-conversations-find-and-close) of a running conversation | `Ctrl+F` | `Ctrl-F` |
| [close](#running-conversations-find-and-close) a running conversation | `Ctrl+X` | `Ctrl-X` |
| [install / update](#installing-or-updating-claude-code-and-codex) Claude Code or Codex | `Ctrl+I` or `Ctrl+T` | `Ctrl-T` (fzf reads `Ctrl-I` as `Tab`) |
| [delete](#delete) | `Del` or `Ctrl+D` | `Del` (`fn`+`⌫`) or `Ctrl-D` |
| the guide | `Ctrl+G` or `F1` — any key goes back | `Ctrl-G` or `F1` — `Enter` goes back |
| cancel | `Esc` (clears the filter first) | `Esc` (clears the filter first; fzf 0.45+) |

### The signs on a row

| Sign | Meaning |
|---|---|
| green `●` | marked |
| red `run` in the age column | running on this PC |
| red `bg` | a Claude background session (`claude --bg`) running on this PC |
| yellow `@host` | open on another PC that shares the data dir — see [Several PCs](#several-pcs-on-one-onedrive) |
| yellow `(cleared)` after the title | a Claude conversation that a `/clear` replaced — see [below](#cleared-and-archived-conversations) |
| dim `(archived)` after the title | a Codex conversation put away with `codex archive` |
| `codex app` in the tool column | a conversation of the Codex desktop app (macOS) — see [Codex app vs Codex CLI](#codex-app-vs-codex-cli) |

These are also filter words: type `run`, `bg`, `@host` (or `elsewhere`), `cleared`, `archived`, `app` or the label of a non-default account to list only those rows. The filter also matches tool, title and folder.

A title or folder too long for its column scrolls through it on the highlighted row (Windows); on macOS / Linux the preview pane shows the full title, folder, account, how the row will open, last-used time and session id.

## What the keys do

### Opening (Enter)

Every selected conversation reopens with `claude --resume <id>` / `codex resume <id>`, each in the folder it was started in (Claude requires it; for Codex it keeps the agent's working root right). The **first one takes over the tab you ran `ccr` in**, so with a single selection `ccr` is simply "resume here"; the others open as new tabs (Windows Terminal: `wt -w 0`; iTerm2 tabs, Terminal.app windows, tmux windows). `-NewWindow` sends everything to a fresh window and leaves the current tab alone.

- Enter on an `@host` row asks first: two processes would append to the same transcript.
- A conversation whose tool is not installed on this PC is skipped, with a hint, instead of failing in its tab.
- Under several accounts, each conversation starts under its own account (see [Several accounts](#several-accounts)).

### Model and effort

`Shift+Enter` or `Ctrl+E` (Windows), `Ctrl-E` (macOS / Linux) opens the same rows, but first lets you pick a model and an effort for each tool in the selection:

| | Claude Code | Codex |
|---|---|---|
| flags | `--model` / `--effort` | `-m` / `-c model_reasoning_effort=` |
| models offered | the aliases `claude --model` knows (`fable`, `opus`, `opus[1m]`, `sonnet`, …), the extra options Claude Code caches, the model in `settings.json` | the models of Codex's own `/model` menu (`models_cache.json`) |
| efforts offered | `low` … `max` | only the levels the chosen model supports |

Every field starts at *no override* — the same as a plain `Enter` — with the value the account's settings name shown next to it.

- **Windows**: one page, a section for the Claude rows and one for the Codex rows (each only when the selection has such rows). `↑` `↓` pick a field, `←` `→` change it, `Del` puts it back to *no override*, `Enter` resumes, `Esc` goes back to the list.
- **macOS / Linux**: fzf menus in a row — model, then effort, for each tool (fzf cannot tell Shift+Enter from Enter). Codex **app** conversations open in the app, which takes no model from outside: ccr says so, and `--terminal` resumes them in a terminal with the flags.

### New conversation

`Ctrl+N` in the picker, or `ccr -n` (`ccr -n TEXT` prefills the name box). Step by step, `Esc` going back one step:

1. **Folder.** First `here` (the folder ccr was started from, known or not), then every folder past sessions used, most recently used first, with session counts; type to filter. On macOS / Linux there is also **`+ new folder`** (`Ctrl-O` jumps to it): type a path (`~` and relative paths welcome) and ccr offers to create it, so a brand-new project needs nothing prepared. *(Not in the PowerShell picker yet.)*
2. **Tool.** `claude`, `codex` or `MAP` (`c` / `x` / `p` jump straight there). On macOS also **`codex app`** wherever the desktop app is installed.
3. **Name** (Claude only; empty = automatic title). ccr starts `claude --name <name>` in the current tab. Codex starts directly: it has no flag for a name at start, use `/rename` inside it.
4. **Account**, only when that tool has more than one.

`MAP` asks for the question instead, then sets up its agents — the same pages as `Ctrl+P`.

### Several accounts

**Yes, you can use several Claude accounts — and several Codex accounts — and choose one per conversation, right from the picker.** Each account is a data dir of its own next to the tool's default one (`~/.claude-work` next to `~/.claude`, `~/.codex-work` next to `~/.codex`), with the tool's own normal login inside. ccr never handles a credential: it only sets `CLAUDE_CONFIG_DIR` / `CODEX_HOME` on the process it starts.

**Adding a second Claude account:**

1. In the picker press `Ctrl+A`. The first time, a page explains what happens: the dirs you use today become the `default` account; nothing is moved or logged out. Press `Enter`.
2. Choose the tool (`claude`) and type a label, for example `work`.
3. If your default account has a status line, ccr offers to copy it (for Codex: its `config.toml`).
4. ccr creates `~/.claude-work`, leaves the picker and runs `claude auth login` inside it: a browser opens and you sign in with the other account. Then the picker comes back with an **account column**.

From the command line the same is `ccr -AddAccount work -Tool claude` (`--add-account work --tool claude`); without `-Tool` both tools get an account `work`; `-CopySettings` copies the default's settings.

**From then on:**

- every conversation resumes under the account it belongs to — the dir its file lives in;
- `Ctrl+N` asks which account a new conversation goes to;
- **to move a conversation to another account**: press `Tab` on its row until it shows the target account's number in the table on top, then `Enter` (fzf older than 0.39: mark with `Tab`, press `Ctrl-O` and choose). In multi-account mode `Tab` steps through: none → its own account (green `●`) → each other account of its tool → none. ccr moves the conversation's files into that account's dir and resumes it there;
- `Ctrl+A` lists the accounts and who is logged in where, and adds one, logs this PC in to an account added on another PC, copies the default's settings, removes one (its conversations move to `default`) or turns the mode off;
- `-Root work` lists one account only; `-Accounts` shows who is logged in where.

The account page, the keys of each side, the `ccr.json` format and what is shared between accounts: [docs/multi-account.md](docs/multi-account.md).

### Local accounts: Ollama on this computer only

**For work that must not leave the computer** (a confidentiality agreement, say), an account can be **local**: its conversations only ever reach the [Ollama](https://ollama.com) server running on this computer. They never go to Anthropic, OpenAI, ollama.com or a synced folder, and need no login.

```
ccr -AddAccount local -Local              # both tools; -Tool claude / -Tool codex for one
ccr --add-account local --local           # the same, GNU spelling
```

or `Ctrl+A` → add → tool → label → tick **Local**. ccr creates the account's dirs outside any synced folder (`%LOCALAPPDATA%\ccr-local\<label>\claude` and `\codex`; macOS / Linux `~/.local/share/ccr-local/...`). It points them at Ollama and lists them in `ccr.json` under `claudeLocalRoots` / `codexLocalRoots`. A ccr older than 0.88 does not read those keys, so it cannot start the plain tools there.

- **Starting:** `Ctrl+N` → folder → tool → the local account → the **model**, among those installed in Ollama (`ollama pull <model>` adds one), with the last one picked first. A resume uses the model the conversation last ran with; `Shift+Enter` / `Ctrl+E` picks another (no effort for these rows).
- **What ccr checks before every start**, refusing with the fix when something is off:
  - Ollama answers on `127.0.0.1:11434`;
  - Ollama's cloud is off, in its settings (`OLLAMA_NO_CLOUD=1`, or `"disable_ollama_cloud": true` in `~/.ollama/server.json`) and on the running server, which must refuse a cloud model. Otherwise a model picked inside the session (`/model something:cloud`) would reach ollama.com;
  - the account's dir still points at Ollama;
  - the model is installed and is not a cloud one.
- **How the dir keeps the tools local**, even when started by hand:
  - **Claude:** its `settings.json` sets `ANTHROPIC_BASE_URL` to Ollama and turns off non-essential traffic, the auto-updater, error reporting and claude.ai MCP servers, and it denies `WebFetch` / `WebSearch`. ccr passes the file again with `--settings`, so a project's own `.claude/settings*.json` cannot redirect it.
  - **Codex:** its `config.toml` has Ollama as the model provider. Plugins, apps, web search, the update check, usage metrics, feedback uploads and the start-up tips are off. ccr passes the provider again with `-c`.
  - Checked with Claude Code 2.1.306, Codex 0.160 and Ollama 0.40.7, both in their own UI and headless: the only connection either tool opened was `127.0.0.1:11434`.
- **Kept apart:**
  - `Tab` never moves a conversation into or out of a local account;
  - a MAP never uses one, since its agents share what they find with cloud agents;
  - a local account takes no copied settings and has no usage limits;
  - removing it (`-RemoveAccount local`, `Ctrl+A` → `Del`) only forgets the entry. The conversations stay in its dir.
- **Not covered:** what the agent writes into the working folder lives there. `Ctrl+N` warns when that folder is inside OneDrive, Dropbox, Google Drive or iCloud.

### Usage limits and token usage

**The table on top** — there with a single account too — shows one line per tool and account: the dir, who is logged in and how far it is into its usage limits, e.g. `5h 9% → 00:19 · week 50% → Wed 7 09:59 · Fable 40%` (used, then the reset time after `→`; yellow from 70%, red from 90%). The figures are the tools' own, asked live in the background: Claude Code's `/usage` (`claude -p "/usage"`, a local command — no model call, no cost) and Codex's app-server (`account/rateLimits/read`); ccr reads no credential.

- Answers are kept for 5 minutes; **`Ctrl+L`** asks every account again. The table redraws as each answer lands (macOS: with fzf 0.45 or later, through `fzf --listen`; with an older fzf at the next redraw).
- `…` marks an account still being asked, `(as of HH:mm)` figures that could not be refreshed. A window at 0% shows no reset time; a window whose reset has passed reads 0%.
- `CCR_LIMITS=0`, or `"limits": false` in `ccr.json`, turns the asking off.

**`Ctrl+K`** toggles a **token-usage column**: each session's total tokens in the last 5 hours and, in parentheses, its share of the tokens of all listed sessions (`5.4M (90%)`). It answers "which sessions ate the quota?". Sessions with no turn in the window stay blank; the order of the list does not change. `-UsageHours 24` changes the window, `ccr -Usage` starts with the column on, and `"usageColumn": true` / `"usageHours": 24` in `ccr.json` make that the default. The data comes from the transcripts: Claude's per-turn usage blocks, Codex's per-turn `token_count` events.

**`Ctrl+J`** shows the **usage details** of the highlighted row: fresh input / cache write / cache read / output (thinking), the models used, a 30-minute timeline of the window and — Codex only — the rate-limit meter the session saw at its last turn (5 h and 7 d; Claude Code does not record its meter, and the page says so). Below that, the window split session by session, largest first, with each one's share.

### Running conversations: find and close

**`Ctrl+F` finds the window** of the highlighted `run` row, for when many terminal windows are open:

- **Windows**: its Windows Terminal window comes to the front, restored if minimized, with the conversation's tab selected; it flashes in the taskbar and the tab's title blinks three times. ccr finds the tab by giving the conversation's console a unique title for a moment and looking for it among the window's tabs (UI Automation), so with a profile that sets `suppressApplicationTitle` you get the window only. A classic console window works too.
- **macOS / Linux**: the iTerm2 session or Terminal.app tab whose tty is the conversation's comes to the front, selected (un-minimized), and its background blinks amber three times before going back to its profile's colors. In tmux its window and pane are selected first (the pane blinks), then the terminal showing that session. This is AppleScript, so the first use from another app asks for Automation rights.

Not for a background session (it has no window) or one open on another PC.

**`Ctrl+X` closes** the running conversation of the highlighted row — a stray background session, a tab you forgot. After a confirmation (pid, kind, status, and a warning when it is busy), a Claude background session is stopped with Claude's own `claude stop`, anything else by ending its process: politely first, by force after 3 s. The conversation itself is kept and can be resumed. Refused for a conversation open on another PC.

### Delete

`Del` or `Ctrl+D` **permanently deletes** the marked conversations, or the highlighted one, after a full-screen confirmation: for one row its title, folder, dates, size and the last prompt and reply; for several, one list with each row's folder, age and size, and the total. **No undo.**

- Claude: the transcript and its sidecar folder are removed. Codex: ccr runs `codex delete --force <id>` (it has already asked you; codex 0.159 refuses to ask without a terminal of its own), so Codex's catalog stays consistent.
- Running conversations are refused; marked together with others, they are kept and named on the confirmation.
- `Ctrl+D` is for keyboards without a `Del` key (on a Mac keyboard `fn`+`⌫` is `Del`). On Windows `Ctrl+Backspace` deletes too. On macOS / Linux it does only where the terminal sends it as `^H`, apart from `Backspace`: Terminal.app sends the same byte for both, so there it stays Backspace (and where `Backspace` itself sends `^H`, `stty erase ^H`, `Ctrl-H` stays the filter's backspace).

### Installing or updating Claude Code and Codex

`Ctrl+I` or `Ctrl+T` (Windows), `Ctrl-T` (macOS / Linux), or `ccr -Install codex` / `-Install all` from the command line. A page shows each tool's state on this PC (version and how it was installed, or *not installed*) and what `Enter` would run; a confirmation shows the exact command first, and `-WhatIf` only prints it.

| | Claude Code | Codex |
|---|---|---|
| not installed | its vendor's installer: `irm https://claude.ai/install.ps1 \| iex` (Windows), `curl -fsSL https://claude.ai/install.sh \| bash` | `irm https://chatgpt.com/codex/install.ps1 \| iex` (Windows), `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` |
| installed by its installer | `claude update` | the installer again (it updates in place; Codex has no update command) |
| installed with npm / winget / Homebrew | `npm install -g <package>@latest`, `winget upgrade`, `brew upgrade --cask` | the same |

- The installer is fetched from the official URL at that moment and run as published, in a separate shell; ccr keeps no copy of it. A tool installed any other way is left alone.
- Claude's installer keeps the `stable` channel when `settings.json` has chosen it. On Windows it puts `claude.exe` in `~\.local\bin` without adding that folder to the PATH, so ccr adds it to the User PATH.
- Codex's installer is always run with the default account's dir, where it keeps its package.

### MAP, the multi-agent panel

One question, two to four fresh agents (Claude Code and/or Codex, each with its own model, effort and account) that discuss it in a folder, round after round, until they all agree. An optional master may change files while the others review read-only. ccr shows every reply as it lands, ends with one final answer and a saved transcript, and leaves the agents' sessions in the picker. Every agent works like its CLI in auto mode, web search included; a long turn is never cut (after 20 minutes ccr asks).

- **Start it**: `Ctrl+P`, `ccr -Panel "question"` (`-MAP`), or `Ctrl+N` → folder → `MAP`. First the question (the rules ccr adds are shown above it), then the folder, then the agents; `Enter` on the agents page starts.
- **Writing the question**: Windows — `Shift+Enter` or `Ctrl+J` for a new line, `Ctrl+O` for an editor, a paste arrives whole. macOS / Linux — one line, a multi-line paste, or an empty line for `$EDITOR`.
- **Watching**: each agent gets a tab of its own that shows live what it reads, runs, changes and writes (Windows Terminal tabs; iTerm2 tabs, Terminal.app windows, tmux windows). At the end, that tab offers to open the agent's session. While the MAP runs, `T` (Windows) or `t` + `Enter` (macOS) opens the tabs again.
- **Going on**: a MAP that ends without agreement continues later with the same agents in their own sessions: `ccr -Panel -Continue`, or `Ctrl+R` on the question page (`r` at the question prompt on macOS).

Details: [docs/panel.md](docs/panel.md).

### Cleared and archived conversations

- **Cleared (Claude).** `/clear` starts a new session that keeps the tab's name, so the conversation it replaced would look like a duplicate. ccr tags it with a yellow `(cleared)`. The link is exact where Claude Code recorded a Remote Control bridge id (the new session's first id equals the old one's last); sessions older than those ids fall back to same folder and same name. Type `cleared` to list them all.
- **Archived (Codex).** `codex archive` moves a conversation's file out of `sessions` into `archived_sessions`, where a picker that reads only `sessions` would lose it. ccr lists those too, with a dimmed `(archived)` (type `archived` to list them all). Opening one, or moving it to another account, runs `codex unarchive` first, under its account: Codex refuses to resume an archived conversation.

## Several PCs on one OneDrive

When the Claude data dirs live in a synced folder, ccr works across the PCs that share it. Three things to know.

**The first listing on a new PC is slow — tell OneDrive to keep the folders local.** ccr reads only a small head and tail of every listed transcript, but OneDrive's Files On-Demand downloads the *whole* file on any read, so the first run pulls every transcript. Fix it once per folder (`.claude`, `.claude-<label>`):

- Windows Explorer: right-click the folder → **Always keep on this device**. Or from a terminal `attrib +P -U "<dir>"` and then `attrib +P -U "<dir>\*" /S /D` (given a folder path alone, `attrib` changes only the folder, not what is in it).
- macOS Finder: right-click the folder → **Always Keep on This Device**.

OneDrive then fetches everything once in the background and keeps new files local.

**A conversation open on the other PC is labelled `@host`.** Claude registers every running process in `<dir>/sessions/<pid>.json`, host included, and that registry syncs too. ccr shows a yellow `@host` instead of the age, asks before opening such a conversation here, and refuses to delete it or move it to another account until it is closed there. An entry left behind by a crash on the other PC ages out after 7 days. Codex has no such registry, and its `~/.codex` is normally not synced.

**What travels and what stays.** With the synced script folder travel the script and `ccr.json` (dirs under the home folder are written as `~\...`), and with it the list of accounts. An account added on another PC reads `not on this PC` until you log this PC in to it (`L` on the `Ctrl+A` page, `Ctrl-L` on macOS). Until then ccr starts nothing under it: a new conversation or a move there stops with `not on this PC - <dir> does not exist` and the command that fixes it, `ccr -AddAccount <label> -Tool <tool>` (Codex refuses a `CODEX_HOME` that does not exist, and Claude would start from an empty dir). What stays per machine: the auto-update record, and the Codex dirs.

## macOS / Linux notes

`ccr.py` has everything the PowerShell version has, **plus Codex desktop-app support** (below). Where the two pickers differ, it is because fzf cannot do the same thing; the [Keys](#keys) table lists every such key, and [docs/multi-account.md](docs/multi-account.md#macos--linux-ccrpy) the account ones.

### Codex app vs Codex CLI

Codex conversations can start in two places, and they do not reopen the same way. Each rollout records who opened it (`session_meta.originator`: `Codex Desktop` for the app, a `codex_cli_*` token for the CLI), so ccr shows the difference in the tool column and sends each row where it belongs:

```
  codex app   22h  Review flusso completo locale   ~/Documents/bnb-monitor
  codex        3d  migrate build to vite           ~/repos/web-app
```

- `codex app` → opened with the app's own deeplink, `codex://threads/<id>`, which focuses that thread in the Codex desktop app (the ChatGPT app, bundle id `com.openai.codex`). No terminal tab is spent, so a selection of app conversations leaves your current tab alone.
- `codex` → resumed as usual, `codex resume <id>` in a tab, in the session's recorded folder.

This matters when the `codex` CLI is not on your `PATH` — the usual case when Codex arrived as the desktop app — because `codex resume` would have nothing to run. `--terminal` forces `codex resume` for app conversations too, and `--dry-run` prints the exact `open codex://…` line instead of launching. On a machine with no URL handler at all, app conversations fall back to `codex resume` with a notice.

Type `app` in the filter to list just those. The `run` flag still only sees CLI sessions (it looks for `codex resume <uuid>` processes); reopening a thread already open in the app just brings it to the front, so nothing breaks.

**Starting one.** `Ctrl-N` → folder → `codex app` opens `codex://threads/new?path=<folder>`, and the app takes that folder as the thread's workspace root — registering it as a project when it is not one yet. Together with `+ new folder`, that means: type the path of a project that does not exist yet, let ccr create the directory, and the new Codex conversation starts there with the project already set up. A first message (typed at the prompt, or the text of `ccr -n TEXT`) is prefilled in the composer; nothing is sent for you.

### Terminal.app: windows, not tabs (unless you ask)

Terminal's AppleScript has `do script`, which opens a *window*; an app sending itself an Apple event needs no rights, so nothing is ever prompted. It has no verb for a new *tab*: that takes a `Cmd-T` keystroke through System Events, a different app, which macOS gates behind *Privacy & Security → Automation*. So by default extra sessions open as separate windows. Pass `--tabs` if you would rather have tabs and don't mind granting that permission once; if you decline, or the keystroke fails for another reason, ccr says what happened and opens a window anyway. iTerm2 makes tabs natively and asks for nothing, and so do tmux, `--new-window`, and Codex conversations sent to the desktop app.

Outside iTerm2, Terminal.app and tmux (e.g. a bare SSH shell) only the first selection can start, in the current terminal; ccr says so.

### The fzf picker

The key line sits above the prompt, with `Ctrl-G` / `F1` for the guide; on the new-conversation pages it says which step you are on and which folder you chose. Everything beyond the plain picker is version-gated, so an older fzf loses the marked counter or the line highlight rather than refusing to start. The config file is `$CCR_CONFIG` or `~/.config/ccr/ccr.json` (same keys as on Windows).

*The Python port is verified against the same session data as the PowerShell version, and the Terminal.app path on a Mac; the iTerm2 scripting is still written from its dictionary alone — report anything odd with `ccr --dry-run`, which prints the exact AppleScript it would run.*

## How it works

No database, no index: ccr reads the same files the tools' own resume features read, and nothing else — so it keeps working across tool updates. Reads are bounded (transcripts can reach hundreds of MB) and parsing is defensive: an unknown format degrades a row's title, never the listing.

- **Claude Code** — `<config>/projects/<slug>/<uuid>.jsonl` (`CLAUDE_CONFIG_DIR` is respected). ccr reads a 16 KB head (folder, exclusion markers) and a 128 KB tail (title lines, last timestamp) of each file. Sidechain and daemon transcripts are left out, as in the built-in picker. The order is exactly the one of `claude --resume` (`min(last message timestamp, file mtime)`), and so is the title: custom title → AI title → first prompt.
- **Codex** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (plus `archived_sessions/`). Session id from the file name, folder and title from the first lines, last activity from the time on the file's **last line** (every rollout line carries one) — not from the file's modified time, which the desktop app, the IDE extension and subagents leave at the conversation's start; the file time is only the fallback. A conversation can span several files: with paginated history (the Codex desktop app), editing a message starts a new page `rollout-<ts>-<id>_<page>.jsonl` and the conversation goes on there. ccr shows the pages as one row, with title and folder from the first page and last activity from the newest, and reads (`Ctrl+K`), moves and deletes all of them together.

The PowerShell version needs no modules (the Codex catalog is read with `winsqlite3.dll`, which ships with Windows); the Python version needs only the standard library and fzf.

### A note on Codex titles

A Codex conversation's name — the one `/rename` gives it, or the one Codex 0.159 gives by itself — lives in Codex's local catalog (`state_N.sqlite`, `threads.name` / distinct `title`) and in `session_index.jsonl`, never in the conversation's file. ccr reads both (on Windows a private snapshot of the catalog via `winsqlite3.dll`) and shows the name. Without one it shows your first message — never the text Codex puts there itself (the environment block, the `# AGENTS.md instructions` block; from the IDE extension, it takes the request inside its `# Context from my IDE setup` block). Renaming the terminal *tab* is invisible to Codex and to ccr; rename the thread inside Codex.

**On several PCs.** Since the name is not in the file, with Codex's `sessions` folder synced between PCs (a link into OneDrive, say) a name given on one PC would not reach the other, which would show the first message instead. So when `sessions` is a link, ccr keeps `<synced folder>.names.jsonl` next to it (`OneDrive\.codex-sessions\work.names.jsonl` for `...\work`), in the format of `session_index.jsonl`: every PC's ccr adds the names it knows, the newest name of a conversation wins wherever it was given, and a newer one from the other PC also goes into the local `session_index.jsonl`, so Codex itself shows it too. A PC passes its names on when ccr runs there.

## License

[MIT](LICENSE)
