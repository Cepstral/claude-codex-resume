# ccr — project conventions

## Claude ↔ Codex parity (hard rule)

ccr treats Claude Code and Codex CLI as peers. **Every concept implemented for
one tool must be implemented for the other in the same change**, with the same
keys, parameters and wording: enumeration, titles, running flags, delete,
new-conversation flow, accounts (dirs, login, identity, moving sessions,
copying settings), self-update. Files are read the way each tool's own resume
feature reads them; no databases are instantiated for enumeration.

When something genuinely has no counterpart, do not silently skip it:
**say so explicitly in the reply** ("not applicable to codex because …") and
record it in the parity table in `docs/multi-account.md`. Known cases so far:

- Claude's first-start wizard (`hasCompletedOnboarding` seed in `.claude.json`)
  has no Codex counterpart; Codex has no wizard to skip.
- Claude's status line ↔ Codex's `config.toml` (model, effort, features,
  per-project trust) are the "settings to copy" of each tool.
- The Codex desktop app reads the same dir as the CLI and cannot be given a
  dir per launch, so it always runs as the default account.
- Codex titles set with `/rename` live in the account's own catalog, not in
  the rollout; a move carries them over by appending to the destination's
  `session_index.jsonl` (the legacy index Codex still reads) — ccr never
  writes Codex's sqlite catalog. Claude titles are inside the transcript.
- Running state: Claude registers each process in `<dir>/sessions/<pid>.json`
  (`pidDomain` = platform:host, `kind`, `status`, `jobId`), so ccr can show
  `@host` for a conversation open on another PC that shares the dir, `bg` for
  a background session, and close one with `claude stop <jobId>`. Codex has
  no registry, no background kind and no `stop`: its running flag is a
  process scan, `@host` and `bg` never apply, and Ctrl+X ends the process.
- Token usage (Ctrl+K / Ctrl+J): both tools record per-turn token counts in
  their transcripts (claude: `message.usage` per assistant line, deduplicated
  by message id; codex: `token_count` events, `last_token_usage`). Codex also
  records the rate-limit meter it saw (`rate_limits.primary/secondary`);
  Claude Code does not write its meter anywhere, so the details page says so
  for claude rows.
- Model / effort at resume (Shift+Enter page): claude `--model` / `--effort`,
  codex `-m` / `-c model_reasoning_effort=`. Codex lists its models and the
  levels each supports in `models_cache.json`; Claude keeps no model list on
  disk, so its list is the `claude --model` aliases + `.claude.json`
  `additionalModelOptionsCache` + the settings.json model. Codex app threads
  (ccr.py) take no model from outside: ccr warns and hands them to the app.
- Installing (Ctrl+I / Ctrl+T, -Install): both vendors publish a standalone
  installer (claude.ai/install.ps1|sh, chatgpt.com/codex/install.ps1|sh), and
  ccr fetches it from that URL at install time and runs it as published -
  never a copy in the repo. Updates: claude native -> `claude update`; codex
  has no update command, so its installer again (it updates in place); npm /
  winget / brew -> their documented upgrade; anything else is left alone.
  Codex's installer keeps its package under CODEX_HOME (packages/standalone),
  so ccr always runs it with the default account's dir, never another
  account's. Claude's installer makes the channel it installs the auto-update
  channel, and settings.json is synced across PCs here, so ccr passes
  `stable` when settings.json has chosen it.
- MAP (Ctrl+P, Ctrl+N -> MAP, -Panel; docs/panel.md): the same rules text,
  rounds and consensus for both tools, and since v0.70 every agent like its
  CLI in auto mode, the web included: claude `-p --permission-mode
  auto|acceptEdits|default --permission-prompts none --allowedTools
  WebSearch,WebFetch`, a reviewer also `--disallowedTools
  Edit,Write,MultiEdit,NotebookEdit`; codex `exec -s read-only|workspace-write
  -c web_search=live` (unless config.toml sets web_search) with its plugins,
  apps and rules. Session ids: claude takes the uuid ccr chooses; codex's comes
  from `thread.started`, and its name goes to session_index.jsonl. Codex's
  shell on Windows needs a PATH without WindowsApps; claude has no counterpart
  (not a restricted-token sandbox). Going on (-Panel -Continue; Ctrl+R on the
  question page, `r` at the Python prompt) resumes both tools' sessions.

## PowerShell ↔ Python parity (hard rule)

`Resume-CcSessions.ps1` (Windows, pwsh) and `ccr.py` (macOS/Linux, Python +
fzf) are the same tool. **Every behavior change, fix and version bump made to
one must be made to the other in the same change**, with the same flags,
keys, config file (`ccr.json`), wording and docs. When the picker UI cannot
express something the same way (fzf vs. the hand-drawn console picker), pick
the nearest fzf equivalent and note it in the reply and in the README's
macOS section; never leave a feature out silently.

Flags accept both spellings on both sides: the PowerShell one (`-Update`,
`-Root work`, `-WhatIf`, case-insensitive) and the GNU one (`--update`,
`--root work`, `--dry-run`); a new flag gets both in the same change.
Keys are the same on both sides wherever the terminal allows it, and when
fzf cannot take a key the PowerShell picker adopts fzf's choice (the account
page is Ctrl+A everywhere because fzf cannot tell Ctrl-M from Enter).
Remaining differences, because Space types into fzf's filter: Space cycling
the account per row ↔ `Tab` mark + `Ctrl-O` (one target account for the
marked rows); `+`/`X` on the account page ↔ rows picked with Enter;
`S` ↔ `Ctrl-S`; the Shift+Enter model/effort page ↔ `Ctrl-E` with fzf
menus in sequence (model, then effort, per tool) — fzf has no shift-enter,
and alt-enter is Windows Terminal's fullscreen key, so the PowerShell picker
takes Ctrl+E as well. The panel page (fields changed with arrows, `+` `-`
`M`) ↔ an fzf list with `▶ start` / agents / `+ add agent` / `rounds` rows,
Enter on an agent opening its settings; the question page (Shift+Enter or
Ctrl+J new line, Ctrl+O editor) ↔ one line, a multi-line paste, or `e` / an
empty line for $EDITOR. In ccr.py a row cannot change account and get a model
in one step (Ctrl-O and Ctrl-E each launch); on Windows Space + Shift+Enter
combine. The mapping table lives in `docs/multi-account.md`.
The panel is Ctrl+P on both sides (named MAP, multi-agent panel, in the UI;
-MAP / --map alias -Panel / --panel): fzf binds ctrl-p to "up" by default,
and `--expect` takes it over (the arrows still move). Its agents' tabs are
Windows Terminal tabs or tmux windows on the PowerShell side, iTerm2 tabs /
Terminal.app windows / tmux windows on the Python side (open_tab); ccr.py has
no tab backend on Windows. The viewer's last prompt is a key on Windows and a
line (Enter / Ctrl-C) in ccr.py.
The install page is Ctrl+I on Windows as asked, plus Ctrl+T on both sides:
fzf cannot take ctrl-i, the byte Tab sends too, so it would steal marking.

Known asymmetries (2026-09-17): `ccr.py` has Codex desktop-app support
(`codex app` rows, deeplinks, `--terminal`) and a `+ new folder` entry on
Ctrl-N that the PowerShell script does not have yet. The PowerShell picker
rotates a cut title or path through its column on the highlighted row
(marquee); fzf cannot animate a list row, so `ccr.py` relies on its preview
pane, which shows the full title and folder of the highlighted row.

## Working rules

- Edit `Resume-CcSessions.ps1` in this repo, parse-check, commit, push; the
  installed copies refresh with `ccr -Update` (main) / `ccrtest -Update` (test
  branch). Bump `$script:CcrVersion` on every behavior change (the loaded
  function self-heals only when the version string differs).
- Auto-update never runs for a copy that sits in a git checkout (a `.git`
  next to the script) and never downgrades. Reason: on 2026-09-17 a test run
  of `ccr.py` from this repo, without `--dry-run`, auto-"updated" the
  working copy to the pushed (older) version and silently discarded
  uncommitted edits. When testing from the checkout anyway, set
  `CCR_AUTO_UPDATE=0` and prefer `--dry-run` / `-WhatIf`.
- The vendors' installers call `exit` and `Set-StrictMode`: run through
  `irm | iex` in ccr's own session they would end or change the user's
  shell. ccr runs them in a child `powershell -NoProfile -ExecutionPolicy
  Bypass -Command` (Windows) or `sh -c` (elsewhere), then appends the new
  registry PATH entries to its own session so the tool works at once.
  The child also gets System32 first on its PATH and powershell.exe by full
  path: codex's installer calls a bare `tar`, and Git for Windows' GNU tar
  (first on the PATH when ccr runs from Git Bash) reads the C: of an archive
  path as a remote host - "Cannot connect to C: resolve failed" (2026-09-29).
- `ForEach-Object <Member>` (the member-name form) goes through ShouldProcess:
  under `-WhatIf` it prints "What if: ... property ..." and returns nothing.
  Always write `ForEach-Object { $_.Member }` - a launch check and the
  account cycling silently broke under `ccr -WhatIf` before 2026-09-29.
- Windows Terminal splits its whole command line on `;`, inside a quoted
  `-Command` argument too, so every `;` in the command a tab runs has to
  reach `wt.exe` as `\;`. The account env prefix carries one, and without
  the escape wt took the rest for a subcommand and failed to start a program
  named " claude --resume <id>" (0x80070002), i.e. the second and later
  selections never opened while multi-account was on (fixed 2026-09-21,
  v0.62). Verified the same day: `\;` arrives at the shell as a plain `;`,
  and backslashes, single quotes and [brackets] in that argument are left
  alone. Tab titles are stripped of `;` for the same reason. Windows only:
  ccr.py hands tabs a shell string through osascript / tmux.
- Nothing is started under an account whose dir does not exist on this PC
  (codex refuses a missing CODEX_HOME, claude would start from an empty
  dir): a new conversation or a move there stops with the error and the
  command that fixes it - `ccr -AddAccount <label> -Tool <tool>`, or the
  tool's own login for the default account. An account added on another PC
  is the usual case: ccr.json syncs, the codex dirs do not (2026-09-29).
- Account dirs are created next to the tool's default dir (never a fixed
  `~\.claude-<label>`), and reused without a new login when they already hold one.
- Headless agents (the panel), verified with claude 2.1.285 and codex
  0.159.0 on 2026-09-30:
  - `claude -p` keeps nothing between turns but the conversation: pass the
    model, effort and permission mode on every turn. `--restricted` (the
    reviewers' flag up to v0.69) ignores settings.json and does not load the
    project's CLAUDE.md; since v0.70 agents run like the CLI and a reviewer
    only loses the edit tools. `plan` mode is not read-only. A model without auto mode (haiku) given `--permission-mode
    auto` silently runs in `default`: only the stream-json init event
    (`--output-format stream-json --verbose`) says so, together with the
    tools the agent got. `--no-session-persistence` keeps a probe out of the
    picker. A session killed in the middle of a command resumes normally.
  - `codex exec`: `-s` goes before `resume`; resume only by uuid; never pass
    `--thread-source` (ccr lists `thread_source: user`). The sandbox covers
    commands, not plugins or apps (connectors): up to v0.69 a read-only agent
    got `--disable plugins --disable apps --ignore-rules`; v0.70 runs codex as
    configured. `exec` takes no `--search`: `-c web_search=live` asks for live
    search (a probe without it searched too, in codex's default mode), and
    `web_search` in config.toml wins. A thread killed in the middle of a
    command resumes normally. `--ephemeral` keeps a probe out of the picker.
  - ccr never ends a MAP turn on its own (v0.70): past `askAfterMinutes`
    (ccr.json `panel`, default 20, 0 = never) it rings and asks, Enter waits,
    S ends that agent's process tree. The old 20-minute kill cut a codex turn
    of a real MAP in the middle of its research.
  - A MAP's state is a `.json` next to its transcript, which -Continue reads;
    a transcript from before v0.70 is parsed back from its Markdown
    (ConvertFrom-CcrPanelTranscript / parse_panel_transcript must agree).
    Tests set CCR_STATE as well as CCR_CONFIG: transcripts go next to the
    state file, and a test MAP would otherwise land in the user's own list.
  - `codex delete <id>` asks for a confirmation since codex 0.159 and, with
    its output captured, refuses ("cannot confirm session deletion without an
    interactive terminal; rerun with --force and a session UUID"). ccr's Del
    fell back to removing the rollout alone, leaving the thread in codex's
    catalog: ccr now passes `--force` after its own confirmation, then the
    plain command (stdin empty) for an older codex (v0.67).
  - codex's Windows sandbox (`[windows] sandbox = "unelevated"`, a restricted
    token) cannot start programs under WindowsApps: from a Store `pwsh`,
    whose folder is on PATH, every command failed with `CreateProcessAsUserW
    failed: 5` - reads too. The panel drops those PATH entries for codex
    agents; codex then finds PowerShell 7 under Program Files even off PATH.
  - `Start-Process`: `$null = $p.Handle` right after the start, or `ExitCode`
    stays empty; it does not quote `-ArgumentList`, so every argument goes
    through ConvertTo-CcrWinArg (MSVCRT rules). Prompts go on stdin as
    UTF-8 without BOM; `Kill($true)` ends the whole tree.
  - `$null | ForEach-Object { }` runs the block once: filter with
    `Where-Object { $_ }` before turning a list that may be absent into
    strings.
- Usage limits (v0.72, the account table above the picker): claude
  `-p "/usage"` is a local command (0 turns, $0) and prints text lines,
  parsed by ConvertFrom-CcrClaudeLimits / parse_claude_limits; codex
  `app-server` answers `account/rateLimits/read` in JSON. A codex window is
  named by its length, never its slot: on prolite the week is `primary`
  (Ctrl+J's meter had them swapped until v0.72). From Git Bash `/usage`
  turns into `C:/Program Files/Git/usage` (MSYS path conversion) and reaches
  the model as a prompt - it cost a real turn once: `MSYS_NO_PATHCONV=1`.
  Tests set `CCR_LIMITS=0` wherever the picker runs, or it asks the real
  tools.
- `& exe @(F)`, where F returns `, $list.ToArray()`, hands the native
  program ONE argument, the whole list joined with spaces: `@()` holds the
  list as a single element. wt then opened one default-profile tab and no MAP
  viewer (v0.69-v0.70); every test passed because they checked the list, not
  the call. Assign, then splat: `$argv = F; & exe @argv`. The same wrap hit
  `@(Get-CcrAccountTable)` in v0.72 (one "System.String[]" line): a function
  whose caller collects with `@()` returns its lines unwrapped. And wt returns before
  its tabs exist and says nothing when it drops a request: Open-CcrPanelTabs /
  open_panel_tabs wait for the viewers' processes. The harness gives the real
  function a stand-in wt.exe that records its arguments (scratch, csc-built).
- The MAP tabs never run an agent: ccr owns every process, and a tab only
  follows the run's live record (agent-<i>.jsonl plus each turn's output,
  both append-only) and at the end offers `--resume` / `resume`. A tab that
  was the agent's interactive CLI during the run would be a second process on
  the same conversation. The viewer's rendering is one function per side
  (Format-CcrPanelEvent / format_panel_event), and both viewers must print the
  same text for the same record. Windows Terminal focuses each tab it opens;
  there is no background option (tmux: `new-window -d`).
- `.claude.json`'s `additionalModelOptionsCache` can hold disabled notices
  (`{"value": "cc-update-required-1", "label": "Opus 5.5 (disabled)",
  "disabled": true}` in an account last used by an older claude): the model
  lists skip `disabled` entries.
- No absolute machine paths, no personal data in the repo or docs.
