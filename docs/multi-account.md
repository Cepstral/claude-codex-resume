# Two (or more) accounts, decided per conversation

Both tools keep one login per **data dir** — Claude Code in `CLAUDE_CONFIG_DIR` (default
`~/.claude`), Codex in `CODEX_HOME` (default `~/.codex`): the OAuth tokens, the logged-in
identity, settings, and every transcript live there. ccr's multi-account mode is built on
exactly that: **one dir per account and tool**, each with the tool's own normal login inside.
A conversation belongs to the account whose dir it lives in, so ccr always resumes it under the
right account with no bookkeeping — and ccr never touches a credential.

## Setup (once) — from inside ccr

```powershell
ccr -AddAccount work
```

The first time, ccr asks for a label for your **current** login/dirs (say `personal`) so the
sessions you already have keep an account name. Then, for each tool, it creates a fresh dir
(`~/.claude-work`, `~/.codex-work`), runs the tool's own interactive login **in that dir**
(`claude auth login` / `codex login` — a browser opens, you sign in with the work account),
and records the entry in `ccr.json` next to `Resume-CcSessions.ps1`. `-Tool claude` or
`-Tool codex` limits it to one tool.

```powershell
ccr -Accounts              # what is configured, and who is logged in where
ccr -RemoveAccount work    # forget the entry; dirs and sessions are NOT deleted
```

`ccr -Accounts` on a two-account setup:

```
accounts in C:\Users\me\OneDrive\.claude\pwsh\ccr.json  (default: personal)
  claude personal     ~\OneDrive\.claude          me@gmail.com (max)
  claude work         ~\.claude-work              me@company.com (max)
  codex  personal     ~\.codex                    Logged in using ChatGPT
  codex  work         ~\.codex-work               Logged in using ChatGPT
```

Identity comes from `claude auth status` / `codex login status` run with that dir selected, so
this is also the quickest way to see that a login went through.

### By hand instead

The same result is a `ccr.json` (`$env:CCR_CONFIG` can point elsewhere) plus one login per dir:

```json
{
  "claudeRoots": { "personal": "~/.claude", "work": "~/.claude-work" },
  "codexRoots":  { "personal": "~/.codex",  "work": "~/.codex-work"  },
  "defaultRoot": "personal"
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

## What the picker shows

An **account column** appears between the tool and the age (magenta), for both tools:

```
filter>                                                 88/121 · 2 marked
↑↓ move · Space mark · Enter open · Ctrl+N new · Del delete · Esc cancel · type to filter · v0.20
● claude work      1h  Billing API pagination          D:\repos\api-server
  claude personal  3h  Home automation bridge          D:\repos\home
  codex  work      5h  migrate build to vite           D:\repos\web-app
● claude personal  1d  Physics simulation viral videos D:\repos\sim-shorts
  codex  personal  2d  sono affidabili i nebulizzatori ~
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
  personal     ~\.claude  (default)
  work         ~\.claude-work
```

Enter starts `claude --name "<name>"` (or `codex`) in the chosen dir; Esc steps back one level.
The account question only appears for a tool that has several accounts configured.

## Opening a conversation under another account — Ctrl+M

Press `Ctrl+M` in the picker (Ctrl+A works too, for terminals that deliver Ctrl+M as Enter).
The hint line turns into the account list, and `Space` now cycles a **number** on the
highlighted row instead of the dot:

```
ACCOUNT MODE: 1 personal (me@gmail.com) · 2 work (me@company.com) · Space cycles the number · Enter open · + add account · Ctrl+M back
1 claude work      1h  Billing API pagination          D:
epospi-server
2 claude personal  3h  Home automation bridge          D:
epos\home
  codex  work      5h  migrate build to vite           D:
epos\web-app
```

`Enter` opens every numbered row **under that account**. When the number differs from the
account the conversation currently belongs to, ccr first **moves** the conversation into that
account's dir (Claude: the transcript and its sidecar folder into the same project slug;
Codex: the rollout file into the same `sessions/YYYY/MM/DD` path — the new home indexes it on
first resume), then resumes it there. From then on it lives in that account. The counter in the
header shows how many rows will be re-homed, `-WhatIf` lists the moves, and a running session is
refused (close its tab first). Only accounts that have a dir for the row's tool are offered.

`Ctrl+M` again returns to normal green marks; numbered rows keep their numbers.

### Adding an account from the picker — `+`

You do not need the command line for the first setup: press `Ctrl+M`, then `+` (or `Insert`).
ccr asks the new label and, if no account exists yet, a label for the current login (the one
whose sessions you already see). It then leaves the picker, runs the same login flows as
`ccr -AddAccount`, waits for a key, and reopens the picker with the new account column.

## Moving a conversation to the other account

Claude transcripts are not tied to an account. Move the session's `.jsonl` — and its sidecar
folder of the same name, if present — from `<dirA>/projects/<slug>/` to
`<dirB>/projects/<slug>/` (same `<slug>`; it is derived from the project folder, not the
account). ccr lists it under the new account on the next run and resumes it there. Remote
Control links do not move: they are cloud objects owned by the account that created them.
Codex keeps a catalog beside its rollout files, so moving a Codex rollout by hand is not
supported — start the thread again under the other account instead.

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
