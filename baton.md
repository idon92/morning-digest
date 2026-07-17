# Morning Digest — baton

## Scope
Daily 7 AM PT email digest. Pulls RSS → Gemini summarizes → HTML email via Gmail SMTP. Runs on GitHub Actions cron + manual `workflow_dispatch`.

## Stack & conventions
- Single-file `digest.py` (Python 3.12 in CI, 3.9 locally). Deps: `feedparser`, `requests`, `python-dotenv`. No tests, no module structure — keep it that way.
- HTML is inline-styled string interpolation (email-client compat). Section headers detected by ALL-CAPS line lookup against `SECTION_COLORS`.
- Gemini call retries on 429/503 across `gemini-2.5-flash` → `gemini-2.5-flash-lite`.
- Comments are sparse and explain *why*, not *what*.

## Current state
**Shipped:** audience-split digest with frontier/benchmark upgrade (2026-07-17). CI runs a `[broadcast, personal]` matrix with `fail-fast: false`.
- `--audience {broadcast,personal}` CLI flag in `main()`.
- Personal audience injects a **Frontier Watch** section right after Finance (OpenAI/Anthropic/DeepMind/Meta AI/xAI/Mistral — Anthropic + Meta + xAI via the Olshansk/rss-feeds mirror; those labs publish no native RSS; Moonshot/DeepSeek/Qwen have none either, fake-200 SPA shells on rss paths).
- **Benchmark Beat** section (both audiences): Epoch Gradient Updates, ARC Prize, METR, LMArena blog feeds; 72h window (`CATEGORY_MAX_AGE_HOURS`) since eval orgs post ~weekly.
- Recency filter in `fetch_articles`: entries older than `MAX_ARTICLE_AGE_HOURS` (36h) dropped, newest-first, undated entries ranked last; empty/bozo feeds warn (fake-200 guard).
- Summarizer: **Kimi K3 primary** (`call_kimi` → api.moonshot.ai/v1, model `kimi-k3`, OpenAI-compatible; K3 fixes temperature/top_p server-side — send no sampling params), Gemini fallback chain unchanged (`call_llm` wraps both). `KIMI_API_KEY` empty → straight to Gemini, so the cron can't break while the key is pending.
- `PERSONAL_EMAIL` is now comma-separated (like `RECIPIENT_EMAIL`): currently ian@ + diane@afterquery.com. Broadcast filters out all personal addresses unless `--include-personal-in-broadcast`.
- Prompt carries a mid-2026 benchmark vocabulary block (SWE-Bench Pro, Terminal-Bench 2.1, GDPval-AA, MCP Atlas, Agents' Last Exam, HLE w/tools, ARC-AGI-3, ECI, METR horizons) + "never invent scores" rule.

## Key files
- `digest.py` — single source of truth.
- `.github/workflows/digest.yml` — cron `0 14 * * *` (7 AM PT during DST; ~1 hr early in PST), matrix on audience.
- `.env` — local secrets (gitignored). Has `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`, `PERSONAL_EMAIL`, `KIMI_API_KEY` (empty until user supplies), and a `GITHUB_TOKEN` that is an **owner (idon92) token** — see gotchas.

## Gotchas
- `python` isn't on PATH locally; use `python3`.
- The `--audience` matrix in CI does **not** set a `PERSONAL_EMAIL` env. The script's default (`ianisaiahdon@gmail.com`) covers it; if recipient ever needs to differ, add a secret + workflow env entry.
- DST shift means winter runs go out at ~6 AM PT, not 7. Acknowledged trade-off (see `a1772bb`).
- Homebrew is installed at `/opt/homebrew/bin/brew` but not on the agent's PATH — call brew/gh by full path.
- `gh` keyring now only has `iidon92` (no admin on the repo); `idon92` is logged out. To manage secrets, prefix with the owner token from `.env`: `GH_TOKEN=$(grep '^GITHUB_TOKEN=' .env | cut -d= -f2) gh secret set …`.
- Kimi K3 API: sampling params (temperature/top_p) are fixed server-side — omit them or the request may error. Low-tier Moonshot keys have concurrency 1 / 3 RPM; the CI matrix runs both audiences concurrently, so the 429-retry path may engage (harmless).
- The Batch RSS (`deeplearning.ai/the-batch/rss/`) is dead (308 → 404) — removed 2026-07-17.

## Open questions / next up
- **`KIMI_API_KEY` not yet set** (locally or as GH secret) — user has the key; until it lands, digest silently uses Gemini fallback. Also confirm whether the key is Moonshot first-party or OpenRouter (OpenRouter needs `KIMI_BASE_URL=https://openrouter.ai/api/v1`, `KIMI_MODEL=moonshotai/kimi-k3`).
- Phase 2 of the frontier plan: "Benchmark Board" from structured data (Artificial Analysis API v2 + snapshot diff via the unused `GH_GIST_TOKEN`; SWE-bench leaderboards.json; Epoch ECI; METR YAML). Verified endpoints saved in agent memory (`verified-ai-benchmark-data-sources`).
- Phase 3: open-weights release radar via HF API `author=moonshotai|deepseek-ai|Qwen&sort=lastModified` (K3 weights drop promised 2026-07-27).

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
- 2026-05-04: Broadcast now filters out `PERSONAL_EMAIL` by default; added `--include-personal-in-broadcast` override.
- 2026-06-23: Switched sender to `ian.news.aq@gmail.com`; new broadcast list (jack, amanda, siddharth/ian/jasper @afterquery). Updated `.env` + 3 GH secrets. Committed/pushed the `--include-personal-in-broadcast` change. Note: active `gh` account must be `idon92` (owner) to manage secrets — `iidon92` lacks admin. Verified via `workflow_dispatch` 28059430573 (both jobs green).
- 2026-06-23: Personal recipient now `ian@afterquery.com` (replaces `ianisaiahdon@gmail.com`). Added `PERSONAL_EMAIL` GH secret + workflow env entry. Side effect: `ian@afterquery.com` filtered out of broadcast (gets personal-only superset); effective broadcast = jack/amanda/siddharth/jasper. `ianisaiahdon@gmail.com` no longer receives anything.
- 2026-07-17: `PERSONAL_EMAIL` now comma-separated; added diane@afterquery.com (GH secret updated via .env owner token — keyring `idon92` login is gone). Frontier upgrade shipped: recency filter (36h, newest-first, fake-200 guard), Benchmark Beat section both audiences (Epoch/ARC Prize/METR/LMArena, 72h window), Interconnects URL fixed, Willison added, dead The Batch feed removed, Mistral RSS added to Frontier Watch, prompt got 2026 benchmark canon + no-invented-scores rule. Summarizer: Kimi K3 primary / Gemini fallback; `KIMI_API_KEY` secret + workflow env added but **key value still pending from user**. Fetch pipeline smoke-tested live (K3 API details verified against platform.kimi.ai docs same day).
