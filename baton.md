# Morning Digest — baton

## Scope
Daily 7 AM PT email digest. Pulls RSS → Gemini summarizes → HTML email via Gmail SMTP. Runs on GitHub Actions cron + manual `workflow_dispatch`.

## Stack & conventions
- Single-file `digest.py` (Python 3.12 in CI, 3.9 locally). Deps: `feedparser`, `requests`, `python-dotenv`. No tests, no module structure — keep it that way.
- HTML is inline-styled string interpolation (email-client compat). Section headers detected by ALL-CAPS line lookup against `SECTION_COLORS`.
- Gemini call retries on 429/503 across `gemini-2.5-flash` → `gemini-2.5-flash-lite`.
- Comments are sparse and explain *why*, not *what*.

## Current state
**Shipped (HEAD = `ad7dedc`):** audience-split digest. CI runs a `[broadcast, personal]` matrix with `fail-fast: false`.
- `--audience {broadcast,personal}` CLI flag in `main()`.
- Personal audience injects a **Frontier Watch** section right after Finance (OpenAI/Anthropic/DeepMind/Meta AI/xAI feeds — Anthropic + Meta + xAI come from the Olshansk/rss-feeds mirror since those labs don't publish native RSS). Mercor/Micro1 skipped — no RSS available.
- Personal sends to `PERSONAL_EMAIL` (defaults to `ianisaiahdon@gmail.com`); broadcast keeps the existing `RECIPIENT_EMAIL` list.
- `system_prompt(include_frontier)` builds a 6- or 7-section prompt dynamically.
- Verified locally on 2026-05-01 and end-to-end via `workflow_dispatch` on 2026-05-02 (run 25265045555 — both matrix jobs green, ~40s each).

## Key files
- `digest.py` — single source of truth.
- `.github/workflows/digest.yml` — cron `0 14 * * *` (7 AM PT during DST; ~1 hr early in PST), matrix on audience.
- `.env` — local secrets (gitignored). Has `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`. **No `PERSONAL_EMAIL`** — relies on the script's default.

## Gotchas
- `python` isn't on PATH locally; use `python3`.
- The `--audience` matrix in CI does **not** set a `PERSONAL_EMAIL` env. The script's default (`ianisaiahdon@gmail.com`) covers it; if recipient ever needs to differ, add a secret + workflow env entry.
- DST shift means winter runs go out at ~6 AM PT, not 7. Acknowledged trade-off (see `a1772bb`).
- Homebrew is installed at `/opt/homebrew/bin/brew` but not on the agent's PATH — call brew/gh by full path. `gh` 2.92.0 is installed and authenticated as `idon92` (keyring); agent-side `git push` works.

## Open questions / next up
- Decide whether to add a `PERSONAL_EMAIL` GitHub secret (cleaner) vs. relying on the script default (works, slightly opaque).

## Session log
- 2026-05-01: Completed half-done audience split (`main()` argparse + branching, dynamic `system_prompt`, `send_email(recipients)` signature, FRONTIER WATCH color). Sent personal copy locally to confirm. Refactor still uncommitted.
- 2026-05-02: Committed the split as `ad7dedc`; push deferred to user (no GitHub creds available in agent session).
- 2026-05-02: Retried `git push origin main` from agent — same keychain-TTY failure. User to push from their own shell, or install `gh` for future agent pushes.
- 2026-05-02: Tried `NONINTERACTIVE=1` Homebrew install — failed at sudo (no TTY). Confirms agent cannot self-bootstrap auth tooling on this machine.
- 2026-05-02: Handed user the Homebrew install one-liner to run in their own terminal. No code changes.
- 2026-05-02: User installed Homebrew. Agent installed `gh` via `/opt/homebrew/bin/brew install gh`. Auth still pending — user to run `gh auth login` interactively, then agent retries push.
- 2026-05-02: User authenticated `gh` as `idon92`; agent pushed `ad7dedc` to `origin/main`. Audience split is now live; first production run is tomorrow's 7 AM PT cron.
- 2026-05-02: Triggered `workflow_dispatch` (run 25265045555) — both broadcast and personal jobs succeeded. Audience split is fully verified in production.
- 2026-05-02: Committed and pushed `baton.md` itself (`47956a6`) so the handoff doc is preserved in git.
