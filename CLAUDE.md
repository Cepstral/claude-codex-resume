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

## Working rules

- Edit `Resume-CcSessions.ps1` in this repo, parse-check, commit, push; the
  installed copies refresh with `ccr -Update` (main) / `ccrtest -Update` (test
  branch). Bump `$script:CcrVersion` on every behavior change (the loaded
  function self-heals only when the version string differs).
- Account dirs are created next to the tool's default dir (never a fixed
  `~\.claude-<label>`), and reused without a new login when they already hold one.
- No absolute machine paths, no personal data in the repo or docs.
