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
- Codex titles set with `/rename` live in the account's own catalog and do not
  travel with a moved rollout; Claude titles are inside the transcript and do.

## PowerShell ↔ Python parity (hard rule)

`Resume-CcSessions.ps1` (Windows, pwsh) and `ccr.py` (macOS/Linux, Python +
fzf) are the same tool. **Every behavior change, fix and version bump made to
one must be made to the other in the same change**, with the same flags,
keys, config file (`ccr.json`), wording and docs. When the picker UI cannot
express something the same way (fzf vs. the hand-drawn console picker), pick
the nearest fzf equivalent and note it in the reply and in the README's
macOS section; never leave a feature out silently.

Key mapping between the two pickers (fzf cannot tell Ctrl-M from Enter, and
Space types into its filter): Ctrl+M/Ctrl+A account page ↔ `Ctrl-A`; Space
cycling the account per row ↔ `Tab` mark + `Ctrl-O` (one target account for
the marked rows); `+`/`X` on the account page ↔ rows picked with Enter;
`S` ↔ `Ctrl-S`. The mapping table lives in `docs/multi-account.md`.

Known asymmetries (2026-09-16): `ccr.py` has Codex desktop-app support
(`codex app` rows, deeplinks, `--terminal`) and a `+ new folder` entry on
Ctrl-N that the PowerShell script does not have yet.

## Working rules

- Edit `Resume-CcSessions.ps1` in this repo, parse-check, commit, push; the
  installed copies refresh with `ccr -Update` (main) / `ccrtest -Update` (test
  branch). Bump `$script:CcrVersion` on every behavior change (the loaded
  function self-heals only when the version string differs).
- Account dirs are created next to the tool's default dir (never a fixed
  `~\.claude-<label>`), and reused without a new login when they already hold one.
- No absolute machine paths, no personal data in the repo or docs.
