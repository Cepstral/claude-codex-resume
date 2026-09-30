# MAP: a multi-agent panel

You ask one question; two to four **fresh** agents — any mix of Claude Code and
Codex, each with its own model, effort and account — work on it in the folder you
pick, read each other's answers round after round, and stop when they all agree.
ccr runs them headless in the tab you started it from, shows every reply as it
lands, and ends with one final answer, a saved transcript, and the agents'
sessions in the picker, ready to be resumed. Each agent can also have a tab of
its own that shows live what it reads, runs, changes and writes. Every agent
works like its CLI in auto mode, web search included, and the rules put
precision before speed. A MAP that ends without agreement can go on later: the
same agents, in their own sessions, from the next round.

MAP stands for *multi-agent panel*; the rules the agents get just call it a panel.

```powershell
ccr -Panel "why does the nightly import skip rows?"     # quote the question; -MAP works too
ccr -Panel -Continue                                     # go on with a saved MAP
```

Or **`Ctrl+P`** in the picker. Both start from the question, then ask for the
folder, then for the agents; `Enter` on the agents page starts. `Ctrl+N` →
folder → **`MAP`** (`p` jumps there) works too: the question, then the agents.
`ccr --panel` works on both platforms, and `-Panel` / `--panel` work without a
question too. `Ctrl+R` on the question page goes on with a saved MAP instead
([Going on with a MAP](#going-on-with-a-map)).

## Setting it up

**The question page** comes first: the rules ccr puts in front of the question
(agent 1's copy; the folder and the agents are filled in once chosen), read-only,
and below them the box for the question. `Enter` goes on; `Shift+Enter` or
`Ctrl+J` adds a line, and so does an `Enter` inside a paste, so a pasted
multi-line question arrives whole. `Ctrl+O` opens `$VISUAL` / `$EDITOR` /
Notepad on the text; an editor that returns at once (Windows 11 Notepad, VS Code
without `--wait`) gets a "save, close, then press Enter here".

**The folder** comes next: the same list as `Ctrl+N`, the folder ccr runs in
first.

**The panel page**, the last one, lists every agent's fields, one per line: **tool**,
**model**, **effort**, and **account** when that tool has several accounts, then
**tabs** (one tab per agent, on by default) and the **round limit** (2–20, default 4). At the bottom, the round-1 command line of the
highlighted agent, so the permissions are always in view.

| Key | Action |
|---|---|
| `↑` `↓` | field |
| `←` `→` | change the value: the models and efforts are the lists of the `Shift+Enter` page |
| `Del` / `Backspace` | back to *no override*: the model or effort the account is configured to use |
| `+` | add an agent: a copy of the highlighted one (at most 4) |
| `-` | remove the highlighted agent (at least 2 stay) |
| `M` | make the highlighted agent the **master**; again to take it back |
| `Enter` / `Esc` | **start the MAP** / back to the folder |

The first panel is codex + claude on their configured models; afterwards ccr
remembers the last one (`ccr.json`, key `panel`). An account that is not on this
PC falls back to the tool's default account.

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
- **A long turn is never cut.** Checking sources takes time, so ccr stops no
  turn on its own: after 20 minutes of one turn it rings the bell and asks on the
  status line — `Enter` keeps waiting (it asks again 20 minutes later), `S` stops
  that turn, and the agent leaves the panel. `ccr.json` →
  `"panel": { "askAfterMinutes": 45 }` moves the question, `0` never asks (the
  old `turnTimeoutMinutes` counts as `askAfterMinutes` when that is not set).
- **An agent whose turn fails** leaves the panel too; below two agents the panel
  stops, and after a failure in round 1 ccr asks whether to go on without it.
  There are no retries: the tools retry on their own.

While agents work, one status line shows the round, who is still thinking and for
how long, how many agreed in the round before, and the tokens so far. Each reply
prints in its tool's colour with its `AGREE` / `CONTINUE` state.

**At the end** ccr prints the outcome, where the transcript is, and one line per
agent with its session id and tokens; with a master also `git status --short` of
the folder; without agreement also how to go on (`ccr -Panel -Continue`). The
sessions are ordinary sessions: they appear in the picker as
`MAP 1/2 · <question>` (`★` marks the master) and resume like any other.

**The transcript** is a Markdown file: the final answer first, then every round
agent by agent, then the rules agent 1 received. It is saved under
`%LOCALAPPDATA%\ccr\panels\` (Windows) or `~/.cache/ccr/panels/` (macOS, Linux),
with the MAP's state next to it for going on.

## Going on with a MAP

Any saved MAP can go on, usually one that ended without agreement — at the round
limit, stopped with `Ctrl+C`, or left with fewer than two agents: the same
agents, in their own sessions, from the next round, on the same question, with a
message from you if you like.

```powershell
ccr -Panel -Continue        # -Continue alone too; ccr --panel --continue on macOS / Linux
```

Or `Ctrl+R` on the question page.

1. **The saved MAPs**, newest first (the last 30): date, outcome and question; the
   highlighted one also shows its agents. `Enter` picks one.
2. **The message page**: the question, how many rounds ran and how the MAP ended,
   and every agent, with the reason when its last turn did not finish. Below, a
   message to every agent for the next round — the answers to their questions, a
   new hint, a correction. It is optional: `Enter` goes on with the box empty.
3. **The panel page** keeps the agents as they were — no `+`, `-` or `M`, no tool
   or account change — but their model and effort can change. The round limit
   counts the rounds already run and goes up to 20.

In the first new round every agent gets, before the usual round message:

```
The panel goes on after a pause.
Your previous turn did not finish (<why>): pick up from where you were.
Message from the user to every agent:
<your message>
```

The second line goes only to an agent whose last turn did not finish, the last
two only when you wrote a message. Both tools resume a session whose last turn
was cut short in the middle of a command. An agent that never replied has no
session to go on with: it starts a new one, with the rules. The new transcript
holds every round, the old ones included; the old transcript stays as it was.
ccr keeps each MAP's state in a `.json` of the same name next to its transcript;
a MAP saved before v0.70 is read back from its transcript.

## The agents' tabs

With **tabs** on (the default), the start opens one tab per agent: in Windows
Terminal a tab of the window ccr runs in, titled `MAP 2/3 · claude opus max`;
inside tmux a window in the background. Each tab follows its agent live:

- its model, permission mode and tools, as the agent reports them;
- every round as it runs: the files it reads, what it searches, the commands it
  runs (and those that fail), the files it changes, its reasoning in brief, and
  its messages;
- the end of each round, with its `AGREE` / `CONTINUE`, and ccr's notes.

The tabs only watch: ccr drives every agent, one turn at a time, on the agent's
own session, and a CLI opened on the same session at the same time would be a
second process writing to the same conversation. When the MAP ends, each tab
says how, and **`Enter` there opens the agent's session** — `claude --resume` /
`codex resume`, under its account — so from then on the tab is that agent's
normal CLI, to go on with it. Any other key closes the tab. Windows Terminal
moves to each tab it opens; ccr's own tab keeps the summary.

ccr checks that the tabs started: a terminal returns before its new tabs exist
and says nothing when it drops a request, so ccr waits a few seconds for each
tab's viewer and otherwise says in the header how many opened. **`T`** during
the MAP opens the agents' tabs again — after closing one, or when the terminal
did not open them; each new tab replays its agent's record from the start.

Nobody can answer an agent during the MAP: the rules tell every agent to state
its assumptions instead of asking. A question an agent writes anyway reaches the
other agents, and you, in its tab; you answer it in that agent's session at the
end.

The tabs read the run's live record: `agent-<i>.jsonl` (each turn's start and end)
and each turn's output, in the run's folder under the temporary folder
(`ccr-panel-<pid>-<time>`). With tabs the folder stays after the run for them,
and the next MAP removes the ones older than a day.

## The rules (fixed, shown before the start)

```
[ccr panel] <first line of the question>

You are agent {i} of {n} on a panel of AI agents working in {folder}.
The other agents: agent {j}: {tool model effort}[ (master)]; ...
{role}
How the panel works:
- Precision comes first, speed does not matter: check every fact in primary sources -
  search the web for laws, official documents and documentation, read the files,
  compute with code - and cite the sources you rely on.
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
- reviewer, with a master: *You are a reviewer: read the files, search the web and
  run what you need, but do not change any file. The master's changes are already
  in the folder when you read: review them.*
- reviewer, no master: *… but do not change any file; nobody on the panel changes files.*

From round 2 an agent receives `Round {k}. Latest messages from the other agents:`
and one `<agent n="…" tool="…" model="…" consensus="…">…</agent>` block per other
agent (the CONSENSUS line moves into the tag; a message over 20,000 characters is
cut and marked `[truncated by ccr]`).

## Permissions: every agent works like its CLI

Every turn is a fresh headless process in the MAP's folder, with the rules and
the messages on stdin, under the agent's account and with that account's
settings, as if you had started the CLI there: for Claude Code `settings.json`
(hooks, allow rules, MCP servers) and the folder's `CLAUDE.md`, for Codex
`config.toml`, plugins, apps and rules, and the folder's `AGENTS.md`. Flags that
do not persist between turns — model, effort, permission mode — are passed on
every turn, the model and effort always explicitly (the configured ones when you
picked *no override*). Nobody can answer a permission prompt during a MAP, so
whatever would ask is denied instead.

| | Claude Code | Codex |
|---|---|---|
| **every agent** | `claude -p --permission-mode <mode> --permission-prompts none --allowedTools WebSearch,WebFetch`. `<mode>` is `auto` when your `settings.json` default mode is `auto`: the agent reads, searches, runs commands and fetches pages as far as the auto-mode classifier allows, as in the CLI. Otherwise the master works in `acceptEdits` and a reviewer in `default`. Web search and fetch are allowed either way | `codex exec -c web_search=live`: its web search looks things up live, like `codex --search` (a flag `exec` does not take). An account whose `config.toml` sets `web_search` keeps its own choice |
| **reviewer** | `--disallowedTools Edit,Write,MultiEdit,NotebookEdit`: every tool but the ones that edit files. ccr checks the tool list the agent reports and fails a reviewer that got any of them | `-s read-only`: it runs commands, reads and searches; the sandbox refuses every write, `apply_patch` included |
| **master** | the editing tools too | `-s workspace-write`: edits and commands inside the folder |
| a model without auto mode | it silently runs in `default` mode (haiku, for one): ccr sees it in the agent's first turn, and from the next one the master works in `acceptEdits`, a reviewer in `default`, with a note | — |
| session | `--session-id <uuid chosen by ccr>`, then `--resume <uuid>`; the reply must carry that id | the `thread.started` id of the first turn, then `exec … resume <id>`; a turn that opens another thread is an error. The name `MAP i/n · …` goes to the account's `session_index.jsonl` |
| account | `CLAUDE_CONFIG_DIR` set on the process when claude has several accounts | `CODEX_HOME` set on the process when codex has several accounts |

**What "no file changes" means for a reviewer.** A Codex reviewer is held by its
sandbox. A Claude reviewer has no tool that edits files, but in auto mode it runs
commands, and a command can write: what keeps it from doing so is the rules and
auto mode's classifier, as in the CLI (in a probe, a reviewer asked to write a
file refused, and did not look for a way around). In `default` mode a Claude
reviewer runs only what your allow rules let through without asking.

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
checking a panel works; the answers of the strong ones are the point. And
with precision first, one turn of a strong model checking its sources can take
well over 20 minutes: that is why ccr asks instead of cutting it.

## Several PCs

The Claude agents' sessions live in the Claude data dir like any other session,
so when that dir is synced they are on every PC; the Codex ones stay in the PC's
`~/.codex`, and the transcripts and the tabs' live records on the PC itself. `ccr.json`
(and with it the last panel) travels with the script folder. So a MAP goes on
on the PC it ran on: its transcript and state are there, and so are its Codex
sessions.

## macOS / Linux (`ccr.py`)

The same panel, the same rules and flags, with fzf menus instead of the pages:

| PowerShell | Python |
|---|---|
| `Ctrl+P`: question page → folder → panel page | `Ctrl-P`: the question at the prompt → the folder (fzf) → the agents (fzf); same order |
| the panel page: fields changed with `←` `→` | an fzf list: `▶ start`, the agents, `+ add agent`, `rounds`. `Enter` on an agent opens its settings (tool, model, effort, account, master, remove) — the model and effort menus of `Ctrl-E` — and `Del` removes it. The preview shows the agent's round-1 command |
| the question page: `Enter`, `Shift+Enter` / `Ctrl+J` for a new line, paste, `Ctrl+O` editor | one line typed at the prompt, or a multi-line paste (taken whole and shown back before it starts); an empty line — or `e` to edit a question already there — opens `$VISUAL` / `$EDITOR` / `nano` / `vi` |
| the agents' tabs: Windows Terminal tabs (tmux windows in the background inside tmux); at the end any key but `Enter` closes | an iTerm2 tab, a Terminal.app window or a tmux window in the background; at the end `Enter` opens the session, `Ctrl-C` closes. Not on Windows, where `ccr.py` has no tab backend |
| `Ctrl+C` read as a key; agents in hidden child processes | `Ctrl-C`, and `SIGHUP` / `SIGTERM` too, end every agent's process group (`taskkill /T` on Windows) |
| a long turn: `Enter` keeps waiting, `S` stops it | `Enter` keeps waiting, `s` + `Enter` stops it (the terminal reads whole lines) |
| `T`: the agents' tabs again | `t` + `Enter` |
| going on: `Ctrl+R` on the question page or `ccr -Panel -Continue`; the saved MAPs, the message page, the panel page (model, effort, round limit) | `r` at the question prompt or `ccr --panel --continue`; the saved MAPs in fzf, the message at the prompt (`e` for the editor), the agents list (model, effort, round limit) |
| transcript in `%LOCALAPPDATA%\ccr\panels\` | transcript in `~/.cache/ccr/panels/` |
