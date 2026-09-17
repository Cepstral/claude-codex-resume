# Two (or more) accounts, decided per conversation

Both tools keep one login per **data dir** — Claude Code in `CLAUDE_CONFIG_DIR` (default
`~/.claude`), Codex in `CODEX_HOME` (default `~/.codex`): the OAuth tokens, the logged-in
identity, settings, and every transcript live there. ccr's multi-account mode is built on
exactly that: **one dir per account and tool**, each with the tool's own normal login inside.
A conversation belongs to the account whose dir it lives in, so ccr always resumes it under the
right account with no bookkeeping — and ccr never touches a credential.

## Setup (once) — from inside the picker, or from the command line

Press `Ctrl+A` in the picker. The first time you get the activation page:

```
Multi-account mode

  You are about to turn multi-account mode on. Nothing is moved or logged out:
  the dirs claude and codex use today become the 'default' account, and every
  session you see now belongs to it.
    claude   ~\.claude
    codex    ~\.codex

  Next you choose a tool and a label for the additional account. ccr creates a
  dir for it next to the default one (.claude-<label> / .codex-<label>) and runs that tool's
  own login there, so each account keeps its own credentials and settings.
  Afterwards Ctrl+A lists the accounts, adds more, or turns the mode off again.

  [Enter] continue    Esc: back, nothing changes
```

`Enter` asks which tool (claude or codex — accounts are per tool, so a second Claude account
does not force a second Codex one) and a label (say `work`), then leaves the picker and runs
that tool's own interactive login **in the fresh dir** (`claude auth login` / `codex login` — a
browser opens, you sign in with the work account). The entry lands in `ccr.json` next to
`Resume-CcSessions.ps1`, and the picker reopens with an account column.

From then on `Ctrl+A` shows the account page:

```
Accounts  C:\Users\me\OneDrive\.claude\pwsh\ccr.json
↑↓ move · + add · Del remove · S copy settings from default (claude: statusline, codex: config.toml) · X turn off · Esc back
  (default)  claude  ~\OneDrive\.claude   me@gmail.com
  (default)  codex   ~\.codex             me@gmail.com
  work       claude  ~\.claude-work       me@company.com
  work       codex   ~\.codex-work        me@company.com

  A session always resumes under the account whose dir it lives in. In the picker, Space cycles the account a row opens under.
```

Rows are grouped by account, the default one in parentheses; the email comes from the dir's own
files (`.claude.json` / `auth.json`), so the page opens instantly. `Enter` and `Esc` go back.

- `+` adds another account (tool, then label, then that tool's login). A checklist offers to
  **copy the default account's settings**: for Claude the status line (the `statusLine` entry is
  merged into the new dir's `settings.json` and the `statusline*` script files are copied next
  to it; Claude resolves the script through `CLAUDE_CONFIG_DIR`, so it works unchanged there),
  for Codex `config.toml` (model, effort, features, per-project trust).
- `S` does the same copy for an existing account (the highlighted row, its own tool).
- `Del` removes the highlighted account: every session it holds **moves to the `default`
  account** of that tool and keeps working there; the dir and its login stay on disk, ccr just
  forgets them. Refused while one of its sessions is running.
- `X` turns multi-account mode off: the same for every account at once, and `ccr.json` goes back
  to no accounts, so both tools are on their single default dir again.

The same from the command line:

```powershell
ccr -AddAccount work              # both tools;  -Tool claude / -Tool codex for one; -CopySettings copies the default's settings
ccr -Accounts                     # what is configured, and who is logged in where
ccr -RemoveAccount work           # sessions move to 'default', entry forgotten
ccr -DisableAccounts              # everything back to the single default dirs
```

`ccr -Accounts` on a two-account setup:

```
accounts in C:\Users\me\OneDrive\.claude\pwsh\ccr.json  (default: default)
  claude default      ~\OneDrive\.claude          me@gmail.com (max)
  claude work         ~\.claude-work              me@company.com (max)
  codex  default      ~\.codex                    Logged in using ChatGPT
  codex  work         ~\.codex-work               Logged in using ChatGPT
```

Identity comes from `claude auth status` / `codex login status` run with that dir selected, so
this is also the quickest way to see that a login went through.

### By hand instead

The same result is a `ccr.json` (`$env:CCR_CONFIG` can point elsewhere) plus one login per dir:

```json
{
  "claudeRoots": { "default": "~/.claude", "work": "~/.claude-work" },
  "codexRoots":  { "default": "~/.codex",  "work": "~/.codex-work"  },
  "defaultRoot": "default"
}
```

```powershell
$env:CLAUDE_CONFIG_DIR = "$HOME\.claude-work"; claude auth login; Remove-Item Env:CLAUDE_CONFIG_DIR
$env:CODEX_HOME        = "$HOME\.codex-work";  codex login;      Remove-Item Env:CODEX_HOME
```

`~` and `%VAR%` expand. `defaultRoot` is what `Ctrl+N` preselects. A tool with no entry keeps
its single default dir (you can split only Claude, or only Codex). Copy `settings.json`, your
global `CLAUDE.md`, or `config.toml` into the new dirs if you want the same behavior there —
each dir is independent (see the table at the end); project trust is asked again per dir.

Without a `ccr.json`, nothing changes: ccr uses the default dirs as before.

`ccr.json` also holds two keys unrelated to accounts: `"usageColumn": true` starts the picker with
the token-usage column on (what `Ctrl+K` toggles), and `"usageHours": 24` sets its window (default 5).
A `ccr.json` with only those keys does not turn multi-account mode on.

## What the picker shows

An **account column** appears between the tool and the age (magenta), for both tools, the
default account in parentheses; above the rows, a legend lists the accounts (see
[Space](#opening-a-conversation-under-another-account--space) below):

```
filter>                                                 88/121 · 2 marked
Multi-account mode active.
  claude  1 (default)  ~\.claude       me@gmail.com
          2 work       ~\.claude-work  me@company.com
  codex   3 (default)  ~\.codex        me@gmail.com
          4 work       ~\.codex-work   me@company.com
↑↓ move · Space cycles the account (dot = as is) · Enter open · Ctrl+N new · Ctrl+A accounts · Del delete · Esc cancel
● claude work       1h  Billing API pagination          D:\repos\api-server
  claude (default)  3h  Home automation bridge          D:\repos\home
  codex  work       5h  migrate build to vite           D:\repos\web-app
● claude (default)  1d  Physics simulation viral videos D:\repos\sim-shorts
  codex  (default)  2d  sono affidabili i nebulizzatori ~
```

The label is part of the filter text, so typing `work` narrows to that account, exactly like
typing a title or a folder.

## Resuming

Mark any mix of accounts and press Enter. Every session starts under **its own** dir; the
first selection takes over the current tab, the others open as tabs. `-WhatIf` shows what that
means concretely — the config dir is set for each launched process, never globally:

```
PS> ccr -WhatIf
this tab: $env:CLAUDE_CONFIG_DIR='C:\Users\me\.claude-work'; claude --resume 7faabc0d-…   (cd D:\repos\api-server)
wt.exe -w 0 new-tab -d D:\repos\web-app --title codex · migrate build to vite pwsh.exe -NoExit -Command $env:CODEX_HOME='C:\Users\me\.codex-work'; codex resume 01a09b9c-…
```

On macOS/Linux inside tmux the same lands as `CLAUDE_CONFIG_DIR='…' claude --resume …` /
`CODEX_HOME='…' codex resume …` per window. The value is a path, not a secret, so it is fine
on a command line.

In the current tab the variable is set only for the duration of the tool process and restored
afterwards: typing `claude` or `codex` by hand later still uses your default account.

## Only one account

```powershell
ccr -Root work          # list (and act on) the work account only
ccr -Root work -n       # new work conversation: folder → name, no account question
ccr work                # same listing via the filter word; other accounts stay one Backspace away
```

`-Root` with an unknown label errors out and prints the configured names.

## New conversation

`Ctrl+N` (or `ccr -n`) → pick the folder → choose the **tool** (`claude` / `codex`) → for
Claude, type the name → **choose the account**:

```
account for the new conversation
↑↓ move · Enter choose · Esc back
  default      ~\.claude  (default)
  work         ~\.claude-work
```

Enter starts `claude --name "<name>"` (or `codex`) in the chosen dir; Esc steps back one level.
The account question only appears for a tool that has several accounts configured.

## Opening a conversation under another account — Space

While multi-account mode is on, the picker shows a fixed legend under the filter line, one
line per **tool and account**, numbered straight through (claude first, then codex) with the
number in the tool's colour. `Space` cycles the account the highlighted row will open under:
its own account first (a green dot — a plain open), then the other accounts **of its tool**
(the entry's number, in the tool's colour), then none — a claude row only ever shows claude
numbers, a codex row only codex numbers.

```
filter>                                                              198/198 · 1 marked · 1 re-homed
Multi-account mode active.
  claude  1 (default)  ~\OneDrive\.claude           me@company.com
          2 personal   ~\OneDrive\.claude-personal  me@gmail.com
  codex   3 (default)  ~\.codex                     me@company.com
          4 personal   ~\.codex-personal            me@gmail.com
↑↓ move · Space cycles the account (dot = as is) · Enter open · Ctrl+N new · Ctrl+A accounts · Del delete · Esc cancel
2 claude (default)  1h  Billing API pagination          D:\repos\api-server
● claude personal   3h  Home automation bridge          D:\repos\home
4 codex  (default)  5h  migrate build to vite           D:\repos\web-app
```

The email is read per dir from that dir's own files, so the same account can show different
logins for the two tools. The `Ctrl+A` page uses the same one-row-per-tool grouping.

`Enter` opens every marked row under the chosen account. When the digit differs from the account
the conversation currently belongs to, ccr first **moves** the conversation into that account's
dir (Claude: the transcript and its sidecar folder into the same project slug; Codex: the
rollout file into the same `sessions/YYYY/MM/DD` path — the new home indexes it on first
resume — plus a line in the new dir's `session_index.jsonl` carrying the `/rename` name, if the
thread had one), then resumes it there. From then on it lives in that account. The header counts the
rows to be re-homed, `-WhatIf` lists the moves, and a running session is refused (close its tab
first). Only accounts that have a dir for the row's tool are offered. The identity next to each
account appears once the account page (`Ctrl+A`) has looked it up.

This is not a mode you switch on per run: it is simply how the picker works while accounts are
configured. `X` on the account page turns it off and moves every conversation back to `default`.

## Moving a conversation to the other account

Claude transcripts are not tied to an account. Move the session's `.jsonl` — and its sidecar
folder of the same name, if present — from `<dirA>/projects/<slug>/` to
`<dirB>/projects/<slug>/` (same `<slug>`; it is derived from the project folder, not the
account). ccr lists it under the new account on the next run and resumes it there. Remote
Control links do not move: they are cloud objects owned by the account that created them.
Codex keeps a catalog beside its rollout files, so moving a Codex rollout by hand is not
supported — start the thread again under the other account instead.

## macOS / Linux (`ccr.py`)

Everything above exists in the Python version with the same `ccr.json` keys (file: `$CCR_CONFIG` or
`~/.config/ccr/ccr.json`) and the same flags spelled the fzf way:

| PowerShell                              | Python                                          |
|-----------------------------------------|-------------------------------------------------|
| `-Root work` · `-Accounts` · `-AddAccount work -CopySettings` · `-RemoveAccount work` · `-DisableAccounts` | `--root work` · `--accounts` · `--add-account work --copy-settings` · `--remove-account work` · `--disable-accounts` |
| `Ctrl+A` account page: `+` add, `Del` remove, `S` copy settings, `X` off | `Ctrl-A` account page: `+` and `X` are rows you pick with Enter, `Del` removes, `Ctrl-S` copies settings |
| `Space` cycles the account a row opens under (per row) | `Tab` marks rows as usual, `Ctrl-O` opens the marked rows under an entry chosen from a menu with the legend's numbering (one target for the whole selection) |
| account column and legend in the picker | the same; the label is part of the fuzzy-searchable text |
| `-WhatIf` lists the moves and the `$env:` prefixes | `--dry-run` lists the moves and the `CLAUDE_CONFIG_DIR='…'` / `CODEX_HOME='…'` prefixes |

## What is shared and what is per account

| Per dir (= per account)                                        | Shared                                              |
|----------------------------------------------------------------|-----------------------------------------------------|
| Claude: login, `.claude.json` (identity, project trust, MCP)   | the projects themselves and their `CLAUDE.md` files |
| Claude: `settings.json`, global `CLAUDE.md`, skills, plugins   | ccr and its `ccr.json`                              |
| Claude: transcripts, auto-memory, prompt history               |                                                     |
| Codex: `auth.json`, `config.toml`, sessions, thread catalog    |                                                     |
| Remote Control sessions and rate limits                        |                                                     |

## Why not one dir with a token per conversation?

Claude Code honors `CLAUDE_CODE_OAUTH_TOKEN` per process, which would allow one shared dir
with per-session accounts. It also makes ccr a credential handler (secure storage on three
OSes, token expiry, keeping secrets off command lines), and Remote Control under a token session
is unverified. The per-dir design keeps ccr out of the auth business entirely.

## Claude ↔ Codex parity

Every account feature exists for both tools. Where a concept has no counterpart, this table says so.

| Concept                                   | Claude Code                                              | Codex CLI                                                        |
|-------------------------------------------|----------------------------------------------------------|------------------------------------------------------------------|
| Data dir per account                      | `CLAUDE_CONFIG_DIR`, created next to the default dir     | `CODEX_HOME`, created next to the default dir                    |
| Login inside the dir                      | `claude auth login`                                      | `codex login`                                                    |
| Reuse of an existing dir with a login     | `.credentials.json` present → no new login               | `auth.json` present → no new login                               |
| Identity shown in the picker              | `.claude.json` → `oauthAccount.emailAddress`             | `auth.json` → email claim of the OpenID token                    |
| Settings copied from the default account  | status line (`statusLine` + `statusline*` files)         | `config.toml`                                                    |
| First-start wizard skipped in a new dir   | `.claude.json` seeded with `hasCompletedOnboarding`      | **not applicable** — Codex has no wizard to skip                 |
| Moving a conversation between accounts    | transcript + sidecar folder, same project slug           | rollout file, same `sessions/YYYY/MM/DD` path                    |
| Title after a move                        | travels with the transcript                              | the `/rename` name is appended to the new dir's `session_index.jsonl` (the legacy index Codex still honours; ccr never writes its sqlite catalog) — best effort |
| Per-launch account selection              | `CLAUDE_CONFIG_DIR` set on the process                   | `CODEX_HOME` set on the process; the desktop app cannot be given one, it always runs as the default account |
| Delete a conversation                     | transcript + sidecar removed                             | `codex delete <id>`                                              |
