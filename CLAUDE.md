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
- Archived conversations (v0.75): `codex archive` moves a rollout from
  `sessions/YYYY/MM/DD` to a flat `archived_sessions/`, and codex refuses to
  resume it there ("session <id> is archived. Run `codex unarchive <id>`");
  `codex delete --force` works on it. ccr lists both folders, tags the row
  `(archived)` (filter word `archived`) and unarchives one before opening or
  moving it (Restore-CcrCodexArchived / unarchive_codex). Claude Code has no
  archive: not applicable. With `sessions` linked into OneDrive,
  `archived_sessions` stays local, so an archived conversation leaves the
  other PCs.
- Codex paginated history (v0.79): one thread can span several rollout files.
  The desktop app writes `history_mode: paginated`; editing a message starts
  a new page `rollout-<ts>-<id>_<page id>.jsonl` (its session_meta carries
  `history_base`, the previous page and the byte offset it continues from)
  and the thread goes on there, while the head page stops. Before v0.79 ccr
  matched only `-<id>.jsonl` and dropped the pages: an app conversation used
  this morning read "19h" and its Ctrl+K tokens were missing. ccr groups the
  files by thread id (Get-CcrCodexPages / codex_pages), takes meta and title
  from the head page (Get-CcrCodexHead / codex_head), last activity = the
  newest of the pages' last lines, and usage, move, unarchive and the delete
  fallback cover every page (`Parts` / `parts`). Claude has no counterpart:
  an edit or `/rewind` branches inside the same transcript.
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
Account per row (v0.85): in multi-account mode ccr.py's Tab cycles the
account a row opens under like the PowerShell picker - none, its own (green
dot), the other accounts of its tool (their legend number) - through a fzf
`reload` of a small helper (TabMarks / TAB_HELPER) with `--track` keeping
the cursor, fzf without --multi (the marked rows are those with a target).
Verified with fzf 0.74 driven over --listen. fzf older than 0.39 keeps Tab
mark + `Ctrl-O` (one target account for the marked rows).
Since v0.86 ccr.py keeps the marks itself in every mode (one account: Tab
is on/off), so fzf always prints the highlighted row and Ctrl-F / Ctrl-X /
Ctrl-J act on it, as on Windows; the typed filter survives a redraw
(--print-query). And the PowerShell picker marks with Tab too - it was
Space - so Space types into the filter on both sides, where every word
must match (PowerShell: a substring per word; fzf: fuzzy).
Remaining differences: `+`/`X` on the account page ↔ rows picked with Enter;
`S` ↔ `Ctrl-S`; the Shift+Enter model/effort page ↔ `Ctrl-E` with fzf
menus in sequence (model, then effort, per tool) — fzf has no shift-enter,
and alt-enter is Windows Terminal's fullscreen key, so the PowerShell picker
takes Ctrl+E as well. The panel page (fields changed with arrows, `+` `-`
`M`) ↔ an fzf list with `▶ start` / agents / `+ add agent` / `rounds` rows,
Enter on an agent opening its settings; the question page (Shift+Enter or
Ctrl+J new line, Ctrl+O editor) ↔ one line, a multi-line paste, or `e` / an
empty line for $EDITOR. A row's account and a model combine on both sides
(the account cycling, then Shift+Enter / Ctrl-E). The mapping table lives in
`docs/multi-account.md`.
The panel is Ctrl+P on both sides (named MAP, multi-agent panel, in the UI;
-MAP / --map alias -Panel / --panel): fzf binds ctrl-p to "up" by default,
and `--expect` takes it over (the arrows still move). Its agents' tabs are
Windows Terminal tabs or tmux windows on the PowerShell side, iTerm2 tabs /
Terminal.app windows / tmux windows on the Python side (open_tab); ccr.py has
no tab backend on Windows. The viewer's last prompt is a key on Windows and a
line (Enter / Ctrl-C) in ccr.py.
The key line above the list names only the essentials plus Ctrl+G, the guide
(Get-CcrPickerGuide / picker_guide): a full page with every key, the signs
on a row and the table on top (v0.76, after a colleague found the old
15-key line unreadable). A new key goes into both guides, not into the line.
fzf's own ctrl-g (abort) is taken over by `--expect`. F1 opens it too
(v0.77): on a Mac, Grammarly's AI Chat takes Ctrl+G system-wide, so the
terminal never sees it, even when focused. Windows Terminal binds no F1.
The install page is Ctrl+I on Windows as asked, plus Ctrl+T on both sides:
fzf cannot take ctrl-i, the byte Tab sends too, so it would steal marking.
Delete is Del or Ctrl+D on both sides (v0.84), Ctrl+D for keyboards
without Del, and takes the marked rows (one list to confirm, the rows still
open kept and named) or else the highlighted one (the detailed page).
Terminals that tell Ctrl+Backspace from Backspace send it as ^H, and fzf
reads ^H as `ctrl-h` (verified
with fzf 0.74 in a pty: 0x08 -> ctrl-h, 0x7f -> bspace, ESC[3~ -> del), so
ccr.py expects ctrl-h, except where `stty erase` is ^H and Backspace itself
sends it (delete_keys). v0.83 had Ctrl+Backspace as the key: Terminal.app
sends 0x7f for it, the byte of Backspace (the user's check, 2026-10-15), so
fzf cannot tell them apart. Ctrl+D (0x04, fzf's ctrl-d, normally
delete-char/eof) reaches fzf from every terminal and the PowerShell picker
as D+Ctrl; Ctrl+Backspace stays as an extra where the terminal sends ^H.
The PowerShell picker takes D, Backspace or H with Ctrl; pwsh on macOS gets
^H as one of them (pty).

Known asymmetries (2026-09-17): `ccr.py` has Codex desktop-app support
(`codex app` rows, deeplinks, `--terminal`) and a `+ new folder` entry on
Ctrl-N that the PowerShell script does not have yet. The PowerShell picker
rotates a cut title or path through its column on the highlighted row
(marquee); fzf cannot animate a list row, so `ccr.py` relies on its preview
pane, which shows the full title and folder of the highlighted row.

Audit of 2026-10-20 (v0.84 -> v0.86): aligned Tab, the filter's words, the
highlighted row of Ctrl-F/X/J, Esc stepping back through Ctrl-N / Ctrl-P
(ccr.py quit), Enter on an empty list (ccr.py quit), the filter kept across
redraws, the dry-run delete pause, c / x / p on the tool step, `--version`,
`--filter` and `-DryRun` in PowerShell, `-Usage` through the self-heal, the
delete page's account line, the guide's filter words. Accepted, because fzf
or the platform differ: ccr.py does not put the cursor back on its row after a
redraw; `+ add agent` on the MAP page copies the highlighted agent in
PowerShell, the last one in ccr.py (the highlighted row is "+ add agent");
the many-row delete list is capped to the screen only in PowerShell; Insert
also adds an account on the PowerShell account page; ccr.py says
"computer" where PowerShell says "PC"; the install page's confirm is a key
in PowerShell, a y/N line in ccr.py; `-WhatIf` lists no `unarchive:` lines.

UI pass of v0.87 (both sides): the account column is blank for the default
account (the table on top names it) and "Multi-account mode active." is
gone; the key line says `Tab mark/account` in multi-account mode; the
guide keeps the keys and drops the terminal notes (Ctrl+Backspace, ^H,
Grammarly), which live in the README. ccr.py: the title column has a fixed
width so the folders line up (PowerShell did already); the hidden filter
words that showed dimmed after the folder are gone, since fzf matches only
what it shows and run / bg / app / (cleared) / (archived) are on the row
already - only `run` on a bg row and `elsewhere @host` stay; Esc clears a
typed filter first, then leaves, as in PowerShell (fzf 0.45+ `transform`,
esc_bind; verified with fzf 0.74 in a pty). Consequence in ccr.py: the
default account's label is no longer on the row, so typing it no longer
narrows to it (the PowerShell filter still matches it); `-Root` does.

v0.87 verified on Windows on 2026-10-21 (pwsh 7.6.7, the real picker in a console with keys injected and the screen read back): all four checks Jacopo listed pass with one and with two accounts, nothing to fix.

Local accounts (v0.88, README "Local accounts"): an account whose
conversations only reach the Ollama server of this computer, for work under a
confidentiality agreement. What was found on 2026-10-21 (claude 2.1.306,
codex 0.160, Ollama 0.40.7), and why it is built this way:
- Separate ccr.json keys (`claudeLocalRoots` / `codexLocalRoots`), never an
  object value inside `claudeRoots`: v0.87 turns an object into a garbage
  relative path, and its account page's "log in" would create that dir and run
  a cloud login there. And Setup-Pc.ps1 / Sync-CodexSessions.ps1 (outside the
  repo) read `codexRoots`: a local codex account there would get its sessions
  linked into OneDrive. Old saves keep unknown keys.
- No `ollama launch`: `ollama launch codex` writes its profile and model
  catalog to `~/.codex` whatever CODEX_HOME says (cmd/launch/codex.go,
  codexConfigPath), i.e. into the default account. ccr starts the tools
  itself, with the env `ollama launch claude` sets, and the account's dir
  pins Ollama, so a tool started there by hand (or by an old ccr) stays local.
- Claude precedence, tested with a bogus base URL: settings.json `env` beats
  the process env; a project's `.claude/settings.json` and
  `settings.local.json` beat the user settings (they redirected it); `--settings
  <file>` beats both. So every local start passes `--settings`.
- Network, watched per process tree (Get-NetTCPConnection): claude's UI with
  only `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` still fetched
  downloads.claude.ai (version check): `DISABLE_AUTOUPDATER=1` stops it.
  Codex's UI fetched raw.githubusercontent.com/openai/codex/main/announcement_tip.toml
  (`[tui] show_tooltips = false`) and opened ab.chatgpt.com: its built-in
  statsig metrics exporter (`ab.chatgpt.com/otlp/v1/metrics`; `[otel]
  metrics_exporter = "none"`, `[analytics] enabled = false`); `[features]
  plugins = false` stops a git fetch of the plugin marketplace. After that,
  127.0.0.1:11434 only, headless and in both UIs. The model requests in
  codex's logs_2.sqlite all went to 127.0.0.1. (pwsh itself phones its own
  telemetry at start; not ccr's, and no content.)
- WebFetch / WebSearch (v0.89, on Luca's request; he approves commands by
  hand): WebFetch fetches the page from this PC and the local model reads it,
  but before that claude sends the site's name to
  `https://api.anthropic.com/api/web/domain_info?domain=` (a blocklist
  check) unless settings.json has `skipWebFetchPreflight: true` - found in the
  2.1.306 binary, verified through a logging proxy: with it, example.com was
  the only external target. WebSearch is a server tool
  (`web_search_20250305`) run by the model provider: through Ollama it returns
  nothing and opens no connection, so it stays denied. The PowerShell claude
  starts for commands phoned dc.services.visualstudio.com (PowerShell's own
  telemetry, version and OS only). v0.89 set POWERSHELL_TELEMETRY_OPTOUT=1 /
  POWERSHELL_UPDATECHECK=Off; v0.91 dropped them on Luca's call (overkill: no
  conversation content), and the README tells how to turn it off by hand.
- Auto mode on a local model: the classifier runs on the same local model
  (its error names it); qwen3.8:27b took over the 60 s claude allows on
  GRINDELWALD ("temporarily unavailable (timed out)") and the command was
  blocked - fails closed, never asks a server. lfm2.5 answered in time.
  Subagents and /compact ran on the local model too.
- Ollama's cloud: off when `~/.ollama/server.json` has `disable_ollama_cloud`
  or OLLAMA_NO_CLOUD=1; the running server then answers a cloud model with 403
  "ollama cloud is disabled", locally - ccr checks the settings first (nothing
  sent while they allow the cloud), then that answer (catches a setting made
  after Ollama started). Without it `/model x:cloud` inside a session would go
  through local Ollama to ollama.com.
- Ollama's default context depends on the VRAM it finds: on 0 VRAM it is 4096
  and truncates Claude's / Codex's prompts silently (both reported 2051 input
  tokens). On GRINDELWALD an auto-update had started the server before its GPU
  libraries were in place; a restart fixed it.
- Long silences (v0.90): Ollama streams nothing while it reads a whole
  conversation again (after the 5-minute keep-alive unloads the model, or a
  cache miss); on GRINDELWALD qwen3.8:27b reads 150-230 tokens/s, so 50k
  tokens take over 5 minutes, and claude aborted one at 5m23s (Ollama logged
  500). Tested on 2026-10-21 against a fake server that held 400 s
  (scratchpad, not in the repo): claude 2.1.306 closed at 360 s when no
  headers came (body idle timeout) and at 300 s when headers came first
  (byte-level watchdog), then re-sent; codex 0.160 waited without headers but
  closed at 300 s after them ("idle timeout waiting for SSE", reconnect 1/5).
  With `API_FORCE_IDLE_TIMEOUT=0`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000`,
  `API_TIMEOUT_MS=1800000` in settings.json env, and
  `stream_idle_timeout_ms = 1800000` in codex's provider table, all four cases
  waited the 400 s. 30 minutes is the byte-level watchdog's cap (docs:
  network-config, "Streaming idle watchdogs"). ccr adds the keys where missing
  before every start and keeps hand-set values, so it never fights an edit.
- Testing ccr.py on Windows: a claude transcript path over 260 characters (a
  long scratch cwd) is invisible to Python's directory listing, not to pwsh.
  Use a short working folder for py tests there.

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
- Claude Code's Windows installer (`claude.exe install`) puts claude.exe in
  `~\.local\bin` and only prints a note when that folder is not on the PATH;
  codex's installer adds its own folder. So since v0.74 ccr's install adds
  `~\.local\bin` to the User PATH (Add-CcrUserPath / add_user_path: the
  registry value keeps its kind, REG_EXPAND_SZ and its %VARS%, and Windows is
  told the environment changed - never [Environment]::SetEnvironmentVariable
  on PATH, which writes REG_SZ). Found on a new PC where `codex` ran and
  `claude` was "not recognized".
- Codex names (v0.73): codex 0.159 names every thread by itself and keeps
  the name (that one or a /rename) only in its local catalog and
  session_index.jsonl, never in the rollout. With `sessions` linked into
  OneDrive the rollouts travel and the names do not: ccr carries them in
  `<link target>.names.jsonl` beside the linked folder (newest wins; a newer
  one is also appended to the local session_index.jsonl, only with a time -
  never a line with an empty updated_at in codex's own file). Times compare
  through ConvertTo-CcrTsKey / ts_key (fractions of 6 or 7 digits). The
  fallback title skips codex's own user messages: `<...>` blocks and
  `# AGENTS.md instructions for <dir>`; the IDE extension's `# Context from
  my IDE setup` block yields the request after `## My request for Codex:`
  (Get-CcrCodexPrompt / codex_prompt).
- Ctrl+F (v0.80) finds the window of a running conversation, both tools
  alike (claude's pid from its registry, codex's from the process scan).
  Windows (Show-CcrSessionWindow): a hidden child PowerShell does the work,
  because a pwsh host attached to another console breaks (BufferCell errors,
  2026-10-15). AttachConsole(pid) -> GetConsoleWindow is Windows Terminal's
  pseudo window, owned by the WT window (GA_ROOTOWNER), or a conhost window;
  the tab: SetConsoleTitle to a unique marker, UI Automation finds the
  TabItem whose name holds it and selects it, the title goes back;
  AttachThreadInput lets the background helper SetForegroundWindow. Verified
  on a test window with two tabs. ccr.py (find_window): `ps -o tty=` ->
  tmux pane (select-window/pane, then a client's tty) -> the iTerm2 session or
  Terminal.app tab with that tty (AppleScript). Verified on a Mac in
  Terminal.app on 2026-10-15 (v0.81): macOS `pgrep` leaves out its own
  ancestors, and Terminal.app is the parent of every tab's `login`, so
  `pgrep -x Terminal` from inside it found nothing ("neither iTerm2 nor
  Terminal.app is running"); `pgrep -a -x` includes them. Terminal.app
  sending Apple events to itself asks for no Automation rights.
  The blink must leave the tab as it was (v0.82): Terminal's AppleScript
  reads the background of a profile that follows the system appearance
  (Basic) as its dark variant (5866/65535 grey) even in light mode, and
  writing that back pinned every found tab to dark grey. Setting the tab's
  profile again by name (`settings set "<name>"`, not `current settings of
  tab`, which is a reference to the tab itself) drops the tab's own colors,
  keeps the window's size and resets a font changed with Cmd-+, so the font
  is put back; a tab whose color differed from its profile's gets the RGB
  back. iTerm2 keeps writing back the color it read: not verified, no
  iTerm2 on the test Mac.
- A codex row's age (last activity, the sort key) is the time on the
  rollout's last line, not the file's mtime (v0.78; Get-CcrCodexLastActivity
  / codex_last; the file time is only the fallback). Verified with codex
  0.160 on 2026-10-15: turns appended from the desktop app, the IDE extension
  and subagents leave the mtime at the conversation's start (a rollout with
  lines of 15:00Z kept an mtime of 07:48Z), while `codex resume` in a
  terminal moves it. A colleague's app conversation of the same morning read
  "22h". The Ctrl+K window filter uses the same time for codex rows.
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
