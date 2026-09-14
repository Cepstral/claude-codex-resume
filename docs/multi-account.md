# Two (or more) Claude accounts, decided per conversation

Claude Code keeps one login per **config dir** (`CLAUDE_CONFIG_DIR`, default `~/.claude`):
the OAuth tokens, the logged-in identity, settings, memory, and every transcript live there.
ccr's multi-account mode is built on exactly that: **one config dir per account**, each with
a normal `claude auth login` inside. A conversation belongs to the account whose dir it lives
in, so ccr always resumes it under the right account with no bookkeeping — and ccr never
touches a credential.

## Setup (once)

1. Log the second account into its own dir. The dir is created on first use:

   ```powershell
   $env:CLAUDE_CONFIG_DIR = "$HOME\.claude-work"
   claude auth login          # browser flow: sign in with the work account
   claude auth status --text  # shows the work identity and this config dir
   Remove-Item Env:CLAUDE_CONFIG_DIR
   ```

   (Bash/zsh: `CLAUDE_CONFIG_DIR=~/.claude-work claude auth login`.)

2. Tell ccr about both dirs with a `ccr.json` **next to `Resume-CcSessions.ps1`**
   (or anywhere, with `$env:CCR_CONFIG` pointing at it):

   ```json
   {
     "claudeRoots": {
       "personal": "~/.claude",
       "work":     "~/.claude-work"
     },
     "defaultRoot": "personal"
   }
   ```

   `~` and `%VAR%` expand. `defaultRoot` is what `Ctrl+N` preselects.

3. Copy `settings.json` and your global `CLAUDE.md` into the second dir if you want the same
   behavior there (each dir is independent — see the table at the end). Project trust is asked
   again per dir the first time you open a folder under the other account.

Without a `ccr.json`, nothing changes: ccr uses `CLAUDE_CONFIG_DIR` / `~/.claude` as before.

## What the picker shows

An **account column** appears between the tool and the age (magenta). Codex rows have none —
Codex has its own single login.

```
filter>                                                 88/121 · 2 marked
↑↓ move · Space mark · Enter open · Ctrl+N new · Del delete · Esc cancel · type to filter · v0.19
● claude work      1h  Billing API pagination          D:\repos\api-server
  claude personal  3h  Home automation bridge          D:\repos\home
  codex            5h  migrate build to vite           D:\repos\web-app
● claude personal  1d  Physics simulation viral videos D:\repos\sim-shorts
  claude work      2d  Release notes 9.1               D:\repos\api-server
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
wt.exe -w 0 new-tab -d D:\repos\sim-shorts --title claude · Physics simulation viral videos pwsh.exe -NoExit -Command $env:CLAUDE_CONFIG_DIR='C:\Users\me\.claude'; claude --resume 507eb4d0-…
```

On macOS/Linux inside tmux the same lands as `CLAUDE_CONFIG_DIR='…' claude --resume …` per
window. The value is a path, not a secret, so it is fine on a command line.

In the current tab the variable is set only for the duration of the claude process and
restored afterwards: typing `claude` by hand later still uses your default account.

## Only one account

```powershell
ccr -Root work          # list (and act on) the work account only
ccr -Root work -n       # new work conversation: folder → name, no account question
ccr work                # same listing via the filter word; other accounts stay one Backspace away
```

`-Root` with an unknown label errors out and prints the configured names.

## New conversation

`Ctrl+N` (or `ccr -n`) → pick the folder → `Enter` → type the name → **choose the account**:

```
account for the new conversation
↑↓ move · Enter choose · Esc back
  personal     ~\.claude  (default)
  work         ~\.claude-work
```

Enter starts `claude --name "<name>"` in the chosen dir; Esc steps back to the name box.
`Tab` (new Codex session) skips the question — Codex has one login.

## Moving a conversation to the other account

Transcripts are not tied to an account. Move the session's `.jsonl` — and its sidecar folder
of the same name, if present — from `<dirA>/projects/<slug>/` to `<dirB>/projects/<slug>/`
(same `<slug>`; it is derived from the project folder, not the account). ccr lists it under the
new account on the next run and resumes it there. Remote Control links do not move: they are
cloud objects owned by the account that created them.

## What is shared and what is per account

| Per config dir (= per account)                       | Shared                                   |
|------------------------------------------------------|------------------------------------------|
| login, `.claude.json` (identity, project trust, MCP) | the projects themselves and their `CLAUDE.md` files |
| `settings.json`, global `CLAUDE.md`, skills, plugins | Codex (single login, own data dir)       |
| transcripts, auto-memory, prompt history             | ccr and its `ccr.json`                   |
| Remote Control sessions and rate limits              |                                          |

## Why not one dir with a token per conversation?

Claude Code honors `CLAUDE_CODE_OAUTH_TOKEN` per process, which would allow one shared dir
with per-session accounts. It also makes ccr a credential handler (secure storage on three
OSes, token expiry, keeping secrets off command lines), and Remote Control under a token session
is unverified. The per-dir design keeps ccr out of the auth business entirely.
