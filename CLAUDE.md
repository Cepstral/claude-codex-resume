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
takes Ctrl+E as well. In ccr.py a row cannot change account and get a model
in one step (Ctrl-O and Ctrl-E each launch); on Windows Space + Shift+Enter
combine. The mapping table lives in `docs/multi-account.md`.
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
- No absolute machine paths, no personal data in the repo or docs.
