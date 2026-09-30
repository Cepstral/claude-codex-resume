# A panel: several agents discuss one question until they agree

You ask one question; two to four **fresh** agents — any mix of Claude Code and
Codex, each with its own model, effort and account — work on it in the folder you
pick, read each other's answers round after round, and stop when they all agree.
ccr runs them headless in the tab you started it from, shows every reply as it
lands, and ends with one final answer, a saved transcript, and the agents'
sessions in the picker, ready to be resumed.

```powershell
ccr -Panel "why does the nightly import skip rows?"     # quote the question
```

Or `Ctrl+N` in the picker → folder → **`panel`** (`p` jumps there). `ccr --panel`
works on both platforms, and `-Panel` / `--panel` work without a question too.

## Setting it up

**The panel page** (Windows) lists every agent's fields, one per line: **tool**,
**model**, **effort**, and **account** when that tool has several accounts, then the
**round limit** (2–8, default 4). At the bottom, the round-1 command line of the
highlighted agent, so the permissions are always in view.

| Key | Action |
|---|---|
| `↑` `↓` | field |
| `←` `→` | change the value: the models and efforts are the lists of the `Shift+Enter` page |
| `Del` / `Backspace` | back to *no override*: the model or effort the account is configured to use |
| `+` | add an agent: a copy of the highlighted one (at most 4) |
| `-` | remove the highlighted agent (at least 2 stay) |
| `M` | make the highlighted agent the **master**; again to take it back |
| `Enter` / `Esc` | on to the question / back |

The first panel is codex + claude on their configured models; afterwards ccr
remembers the last one (`ccr.json`, key `panel`). An account that is not on this
PC falls back to the tool's default account.

**The question page** shows the rules ccr puts in front of the question (agent
1's copy), then an input box. `Enter` starts; `Shift+Enter` or `Ctrl+J` adds a
line, and so does an `Enter` inside a paste, so a pasted multi-line question
arrives whole. `Ctrl+O` opens `$VISUAL` / `$EDITOR` / Notepad on the text; an
editor that returns at once (Windows 11 Notepad, VS Code without `--wait`) gets
a "save, close, then press Enter here".

## How a panel runs

- **Round 1**: every agent answers on its own, in parallel, and ends with
  `CONSENSUS: CONTINUE`.
- **Every later round**: each agent gets the other agents' latest messages and
  revises: it checks them against the code, says what is wrong or missing,
  adopts what is better and restates the best solution in full.
- **With a master**, round 1 stays parallel and nobody changes anything; from
  round 2 the master works **first** (it may change files), then the reviewers
  run in parallel on the updated folder and the master's fresh message.
- **Consensus** counts from round 2: when every active agent ends with
  `CONSENSUS: AGREE`, the panel stops. The marker is read from the last three
  non-empty lines, markdown stripped; a reply without it counts as `CONTINUE`
  (shown as *no marker*) and that agent gets a reminder.
- **At the round limit** ccr asks whether to go on for 2 more rounds.
- **The final turn** goes to the master (else the first agent still in): with
  consensus it writes the agreed solution in full, without it the shared points
  and the open disagreements, agent by agent.
- **`Ctrl+C`** stops everything at once: every agent process tree is ended.
- **A turn that takes longer than 20 minutes** is ended and its agent leaves the
  panel (`ccr.json` → `"panel": { "turnTimeoutMinutes": 30 }` changes the limit).
  So does an agent whose turn fails; below two agents the panel stops, and after
  a failure in round 1 ccr asks whether to go on without it. There are no retries:
  the tools retry on their own.

While agents work, one status line shows the round, who is still thinking and for
how long, how many agreed in the round before, and the tokens so far. Each reply
prints in its tool's colour with its `AGREE` / `CONTINUE` state.

**At the end** ccr prints the outcome, where the transcript is, and one line per
agent with its session id and tokens; with a master also `git status --short` of
the folder. The sessions are ordinary sessions: they appear in the picker as
`panel 1/2 · <question>` (`★` marks the master) and resume like any other.

**The transcript** is a Markdown file: the final answer first, then every round
agent by agent, then the rules agent 1 received. It is saved under
`%LOCALAPPDATA%\ccr\panels\` (Windows) or `~/.cache/ccr/panels/` (macOS, Linux).

## The rules (fixed, shown before the start)

```
[ccr panel] <first line of the question>

You are agent {i} of {n} on a panel of AI agents working in {folder}.
The other agents: agent {j}: {tool model effort}[ (master)]; ...
{role}
How the panel works:
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
<the question>
```

`{role}` is one of:

- master: *You are the master: the only agent allowed to change files, and only
  inside this folder. Do not change files in round 1. From round 2 you may change
  files to try or implement what the panel converges on; list every change. Never
  commit or push.*
- reviewer, with a master: *You are a reviewer: read and search the files as much
  as you need, but do not change them. The master's changes are already in the
  folder when you read: review them.*
- reviewer, no master: *… but do not change them; nobody on the panel changes files.*

From round 2 an agent receives `Round {k}. Latest messages from the other agents:`
and one `<agent n="…" tool="…" model="…" consensus="…">…</agent>` block per other
agent (the CONSENSUS line moves into the tag; a message over 20,000 characters is
cut and marked `[truncated by ccr]`).

## Permissions: what each agent can do

Every turn is a fresh headless process in the panel's folder, with the rules and
the messages on stdin. Flags that do not persist between turns — model, effort,
permission mode — are passed on every turn, and the model and effort always
explicitly (the configured ones when you picked *no override*).

| | Claude Code | Codex |
|---|---|---|
| **reviewer** (read-only) | `claude -p --restricted --tools Read,Grep,Glob --strict-mcp-config --permission-mode dontAsk --permission-prompts none`: the three read tools, nothing else — no shell, no edits, no MCP server, file tools confined to the folder, your settings (hooks, allow rules) not loaded. ccr checks the tool list the agent reports and fails a reviewer that got anything more | `codex exec -s read-only --disable plugins --disable apps --ignore-rules`: it can run commands, so it can read and search, but the sandbox refuses every write, `apply_patch` included. Plugins and apps (connectors) are off because the sandbox does not cover them; `--ignore-rules` drops the execpolicy `.rules` that could let a command run outside the sandbox |
| **master** | `claude -p --permission-mode <mode> --permission-prompts none`: `auto` when your `settings.json` default mode is `auto` (edits, plus the commands its classifier allows), else `acceptEdits` (edits in the folder, no commands). A model without auto mode (haiku, for one) silently runs in `default` mode, where nothing is allowed: ccr sees it in round 1, when the master must not change anything anyway, and switches it to `acceptEdits` from its next turn, with a note | `codex exec -s workspace-write`: edits and commands inside the folder |
| project instructions | `--restricted` does **not** load the folder's `CLAUDE.md`; the rules' first point asks every agent to read it (a reviewer can: it is inside the folder) | `AGENTS.md` loads as usual |
| session | `--session-id <uuid chosen by ccr>`, then `--resume <uuid>`; the reply must carry that id | the `thread.started` id of the first turn, then `exec … resume <id>`; a turn that opens another thread is an error. The name `panel i/n · …` goes to the account's `session_index.jsonl` |
| account | `CLAUDE_CONFIG_DIR` set on the process when claude has several accounts | `CODEX_HOME` set on the process when codex has several accounts |

**Windows and the Microsoft Store PowerShell.** Codex's Windows sandbox runs the
agent's shell with a restricted token, and a restricted token cannot start
programs kept under `WindowsApps` — the Store edition of PowerShell 7 among
them. A `pwsh` from the Store adds its own folder to `PATH`, codex agents
started from it picked that `pwsh`, and every command they ran failed with
`CreateProcessAsUserW failed: 5`, reads included (verified with codex 0.159).
ccr starts codex agents with a `PATH` without the `WindowsApps` entries; codex
then finds a PowerShell it can start — PowerShell 7 under Program Files, which
it finds even when that is not on `PATH`. Codex sessions you start yourself from a Store `pwsh`
inherit the same `PATH`; PowerShell 7 installed from the MSI avoids the
problem for them too.

**What a panel costs.** Every turn re-sends the rules and the other agents'
latest messages, and each agent keeps its own conversation: a panel of two
agents over four rounds is about nine turns. The cheap models are fine for
checking a panel works; the answers of the strong ones are the point.

## Several PCs

The Claude agents' sessions live in the Claude data dir like any other session,
so when that dir is synced they are on every PC; the Codex ones stay in the PC's
`~/.codex`, and the transcripts in the PC's own `panels` folder. `ccr.json`
(and with it the last panel) travels with the script folder.

## macOS / Linux (`ccr.py`)

The same panel, the same rules and flags, with fzf menus instead of the pages:

| PowerShell | Python |
|---|---|
| the panel page: fields changed with `←` `→` | an fzf list: `▶ start`, the agents, `+ add agent`, `rounds`. `Enter` on an agent opens its settings (tool, model, effort, account, master, remove) — the model and effort menus of `Ctrl-E` — and `Del` removes it. The preview shows the agent's round-1 command |
| the question page: `Enter`, `Shift+Enter` / `Ctrl+J` for a new line, paste, `Ctrl+O` editor | one line typed at the prompt, or a multi-line paste (taken whole and shown back before it starts); an empty line — or `e` to edit a question already there — opens `$VISUAL` / `$EDITOR` / `nano` / `vi` |
| `Ctrl+C` read as a key; agents in hidden child processes | `Ctrl-C`, and `SIGHUP` / `SIGTERM` too, end every agent's process group (`taskkill /T` on Windows) |
| transcript in `%LOCALAPPDATA%\ccr\panels\` | transcript in `~/.cache/ccr/panels/` |
