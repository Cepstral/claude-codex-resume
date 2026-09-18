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
`S` ↔ `Ctrl-S`. The mapping table lives in `docs/multi-account.md`.

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
- Account dirs are created next to the tool's default dir (never a fixed
  `~\.claude-<label>`), and reused without a new login when they already hold one.
- No absolute machine paths, no personal data in the repo or docs.
