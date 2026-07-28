#!/usr/bin/env python3
"""Morning Digest — pulls news from RSS feeds, summarizes via Gemini, emails a polished digest."""

import os
import argparse
import calendar
import json
import re
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

import time

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]  # fallback summarizer
# Kimi K3 (Moonshot) is the primary summarizer when a key is present.
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k3")
# Grok/X search for Benchmark Beat. Optional — absent key just skips the section's X items.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.5")
# Dedup state for X citations. CI runners are ephemeral, so "already cited" lives in a gist.
# CI maps the GH_GIST_TOKEN secret onto GITHUB_TOKEN (see digest.yml).
GH_GIST_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SEEN_GIST_ID = os.environ.get("SEEN_GIST_ID", "")
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
# Comma-separated, like RECIPIENT_EMAIL.
PERSONAL_EMAILS = [
    e.strip()
    for e in os.environ.get("PERSONAL_EMAIL", "ianisaiahdon@gmail.com").split(",")
    if e.strip()
]

FEEDS = {
    "Finance": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://finance.yahoo.com/news/rssindex",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.theguardian.com/uk/business/rss",
        "https://api.axios.com/feed/",
    ],
    "Geopolitics": [
        "https://feeds.npr.org/1004/rss.xml",
        "https://foreignpolicy.com/feed/",
        "https://rss.dw.com/rdf/rss-en-world",
    ],
    "Tech": [
        "https://hnrss.org/frontpage",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://www.wired.com/feed/rss",
    ],
    "AI / Data Update": [
        "https://www.interconnects.ai/feed",
        "https://sebastianraschka.substack.com/feed",
        "https://simonwillison.net/atom/everything/",
    ],
    # Orgs that produce benchmark/eval numbers, not just commentary.
    # (Epoch's Gradient Updates substack — epoch.ai itself has no feed.)
    "Benchmark Beat": [
        "https://epochai.substack.com/feed",
        "https://arcprize.org/feed.xml",
        "https://metr.org/feed.xml",
        "https://arena.ai/blog/rss/",
    ],
}

# Frontier-lab feeds for the "Frontier Watch" section (all editions).
# OpenAI, DeepMind and Mistral expose native RSS; the rest are pulled from the
# Olshansk/rss-feeds community mirror (those labs publish no native feed).
# Mercor and Micro1 have no RSS source available — skipped in v1.
FRONTIER_LAB_FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_research.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_meta_ai.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_xainews.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_blogsurgeai.xml",
    "https://mistral.ai/rss.xml",
]

MAX_ARTICLES_PER_FEED = 3
MAX_ARTICLE_AGE_HOURS = 36
# Eval orgs publish ~weekly; a daily-sized window would leave the section empty most days.
CATEGORY_MAX_AGE_HOURS = {"Benchmark Beat": 72}

# X/Grok search settings — matches the Benchmark Beat window so the section is internally consistent.
MAX_X_POSTS = 5
X_SEARCH_AGE_DAYS = 3
SEEN_GIST_FILE = "seen_x_citations.json"
SEEN_HISTORY_LIMIT = 500  # ~3 months of posts at 5/day; keeps the gist small and the read fast
X_POST_RE = re.compile(r"(?:x|twitter)\.com/[^/\s]+/status/(\d+)", re.I)


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_articles(feeds):
    """Return {category: [{'title': ..., 'link': ..., 'summary': ...}, ...]}, freshest first."""
    articles = {}
    for category, urls in feeds.items():
        max_age = CATEGORY_MAX_AGE_HOURS.get(category, MAX_ARTICLE_AGE_HOURS)
        cutoff = time.time() - max_age * 3600
        items = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                if not feed.entries:
                    # Some sites fake-200 an HTML shell on rss paths; don't treat as quiet day.
                    print(f"[warn] no entries from {url} (bozo={getattr(feed, 'bozo', '?')})")
                    continue
                dated = []
                for entry in feed.entries:
                    ts = entry.get("published_parsed") or entry.get("updated_parsed")
                    ts = calendar.timegm(ts) if ts else 0
                    if 0 < ts < cutoff:
                        continue  # stale; undated (ts=0) kept but ranked last
                    dated.append((ts, entry))
                dated.sort(key=lambda pair: pair[0], reverse=True)
                for _, entry in dated[:MAX_ARTICLES_PER_FEED]:
                    items.append({
                        "title": entry.get("title", ""),
                        "link": entry.get("link", ""),
                        "summary": entry.get("summary", "")[:300],
                    })
            except Exception as e:
                print(f"[warn] failed to fetch {url}: {e}")
        articles[category] = items
    return articles


# ── X / Grok benchmark chatter ────────────────────────────────────────────────
#
# RSS only reaches orgs that blog. Benchmark scores usually surface on X first, so
# Grok's server-side x_search tool fills that gap for Benchmark Beat.
# Note: xAI retired the old `search_parameters` Live Search API on 2026-01-12 —
# this uses the current agent-tools shape (POST /v1/responses with tools=[x_search]).

def _gist_headers():
    return {"Authorization": f"Bearer {GH_GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def load_seen_citations():
    """Post IDs cited in earlier digests. Returns None when persistence is unavailable —
    distinct from [] (persistence works, nothing seen yet), because None means we cannot
    promise no repeats and should say so out loud."""
    if not (SEEN_GIST_ID and GH_GIST_TOKEN):
        print("[warn] SEEN_GIST_ID/GITHUB_TOKEN unset — X dedup OFF, posts may repeat")
        return None
    try:
        r = requests.get(f"https://api.github.com/gists/{SEEN_GIST_ID}",
                         headers=_gist_headers(), timeout=20)
        r.raise_for_status()
        raw = r.json().get("files", {}).get(SEEN_GIST_FILE, {}).get("content") or "[]"
        ids = json.loads(raw)
        if not isinstance(ids, list):
            raise ValueError("gist payload is not a list")
        return [str(i) for i in ids]
    except Exception as e:
        print(f"[warn] couldn't read dedup gist ({e}) — X dedup OFF this run")
        return None


def save_seen_citations(previous, new_ids):
    """Append newly-cited IDs, oldest trimmed first. Called only after a successful send so
    a crash mid-run doesn't burn posts that nobody ever received."""
    if previous is None or not new_ids:
        return
    merged = previous + [i for i in new_ids if i not in set(previous)]
    merged = merged[-SEEN_HISTORY_LIMIT:]
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{SEEN_GIST_ID}",
            headers=_gist_headers(),
            json={"files": {SEEN_GIST_FILE: {"content": json.dumps(merged, indent=0)}}},
            timeout=20,
        )
        r.raise_for_status()
        print(f"       dedup store now holds {len(merged)} post id(s)")
    except Exception as e:
        print(f"[warn] couldn't update dedup gist: {e} — these posts may repeat tomorrow")


def _x_post_id(url):
    m = X_POST_RE.search(url or "")
    return m.group(1) if m else None


def _responses_output_text(data):
    """Pull assistant text out of a /v1/responses payload without assuming exact nesting."""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output") or []:
        for block in (item or {}).get("content") or []:
            if (block or {}).get("type") == "output_text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks)


def _extract_json_array(text):
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (text or "").strip(), flags=re.M)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, list) else []
    except ValueError:
        return []


def fetch_x_benchmark_posts(seen_ids):
    """Recent X posts about AI benchmark results, excluding anything cited before.

    Returns RSS-shaped dicts so build_prompt needs no special case. Never raises: the
    digest must still go out if xAI is down.
    """
    if not XAI_API_KEY:
        print("[warn] XAI_API_KEY not set — skipping X benchmark search")
        return []

    today = dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=X_SEARCH_AGE_DAYS)
    ask = (
        f"Search X for the most notable posts from the last {X_SEARCH_AGE_DAYS} days about AI "
        "benchmark and eval results — new scores, leaderboard movements, eval releases, or "
        "credible critiques of a benchmark. Prefer posts from labs, benchmark maintainers and "
        "researchers over hype accounts, and prefer posts citing concrete numbers.\n\n"
        f"Return AT MOST {MAX_X_POSTS} posts as a JSON array and nothing else. Each element: "
        '{"url": "<full x.com post URL>", "handle": "<author handle without @>", '
        '"claim": "<one sentence, max 30 words, stating what the post actually reports>"}\n'
        "Only include posts you actually found via search — never construct or guess a URL. "
        "If nothing noteworthy was posted, return []."
    )
    payload = {
        "model": XAI_MODEL,
        "input": [{"role": "user", "content": ask}],
        "tools": [{
            "type": "x_search",
            "from_date": since.isoformat(),
            "to_date": today.isoformat(),
        }],
    }
    try:
        resp = requests.post(
            f"{XAI_BASE_URL}/responses",
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            json=payload,
            timeout=(30, 180),
        )
        if resp.status_code != 200:
            print(f"[warn] x_search HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
    except Exception as e:
        print(f"[warn] x_search call failed: {e}")
        return []

    # Cross-check model-reported URLs against the API's own citation list, so a hallucinated
    # link can't reach the digest. If citations are absent, fall back to URL-shape validation.
    cited = {pid for pid in (_x_post_id(u) for u in data.get("citations") or []) if pid}
    items, new_ids = [], []
    for entry in _extract_json_array(_responses_output_text(data)):
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        pid = _x_post_id(url)
        if not pid or pid in seen_ids or pid in new_ids:
            continue
        if cited and pid not in cited:
            print(f"[warn] dropping uncited X post {pid} (not in API citations)")
            continue
        handle = (entry.get("handle") or "").lstrip("@").strip() or "unknown"
        claim = " ".join((entry.get("claim") or "").split())[:280]
        if not claim:
            continue
        items.append({
            "title": f"[X] @{handle}: {claim[:120]}",
            "link": url,
            "summary": f"X post by @{handle} — {claim}",
            "post_id": pid,
        })
        new_ids.append(pid)
        if len(items) >= MAX_X_POSTS:
            break

    skipped = len(cited - set(new_ids)) if cited else 0
    print(f"       x_search: {len(items)} new post(s)"
          + (f", {skipped} already-seen/filtered" if skipped else ""))
    return items


# ── Gemini summarization ──────────────────────────────────────────────────────

def system_prompt():
    sections = [
        "**Frontier Watch** — biggest releases & research from frontier AI labs "
        "(OpenAI, Anthropic, DeepMind, Meta AI, xAI, Mistral)",
        "**Benchmark Beat** — new AI benchmark results, eval releases, and leaderboard moves. "
        "Some items are X posts, prefixed '[X] @handle'. Treat those as claims attributable to "
        "that account rather than established fact: name the account when you use one, and never "
        "present a post's numbers as confirmed the way you would a published result.",
        "**World Lore** — geopolitics & global affairs",
        "**Tech Tea** — technology & innovation",
        "**Data Dive** — AI research, ML engineering & data science",
        "**Money Talk** — finance & markets. Close this section with a 'TICKERS TO WATCH' list: "
        "3-5 stocks or ETFs that appear in today's provided articles, one line each — ticker "
        "symbol, company/fund name, and why it's in the news right now. Open the list with the "
        "exact line 'Not investment advice — just what's hot in today's headlines.' Only name "
        "tickers whose news is in the provided articles; never recommend from memory.",
    ]
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sections))
    return (
        "You are a witty investment and technology expert who actually reads the news. "
        f"Given today's articles, write a morning digest with EXACTLY these {len(sections)} sections:\n\n"
        f"{numbered}\n\n"
        "Context on the mid-2026 AI benchmark landscape: frontier model releases are judged "
        "primarily on agentic evals — SWE-Bench Pro, Terminal-Bench 2.1, GDPval-AA, MCP Atlas, "
        "Agents' Last Exam, JobBench, BrowseComp, OSWorld-Verified, Toolathlon, Humanity's Last "
        "Exam (with tools), ARC-AGI-3 — plus aggregate trackers (Artificial Analysis Intelligence "
        "Index, Epoch ECI, METR time horizons). When an article cites benchmark scores, include "
        "the exact numbers and who they beat. Never invent, round, or extrapolate a score that "
        "is not in the article text.\n\n"
        "For each section write 2-3 short paragraphs. "
        "Be insightful but conversational — like a group chat, not a boardroom. "
        "If a category has no fresh articles, write one line saying it's a quiet day there. "
        "Use plain text (no markdown), just section headers in ALL CAPS followed by a blank line."
    )


def build_prompt(articles):
    parts = []
    for category, items in articles.items():
        parts.append(f"=== {category.upper()} ===")
        if not items:
            parts.append(f"(no fresh articles in the last {MAX_ARTICLE_AGE_HOURS} hours)")
        for a in items:
            parts.append(f"- {a['title']}\n  {a['summary']}\n  {a['link']}")
        parts.append("")
    return system_prompt() + "\n\nHere are today's articles:\n\n" + "\n".join(parts)


def call_kimi(prompt):
    url = f"{KIMI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {KIMI_API_KEY}"}
    # K3 fixes temperature/top_p server-side — Moonshot docs say omit sampling params.
    # Streamed because K3's always-on reasoning outlasts proxy buffering timeouts (CF 524).
    payload = {
        "model": KIMI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    for attempt in range(4):
        resp = requests.post(url, json=payload, headers=headers, timeout=(30, 300), stream=True)
        if resp.status_code in (429, 503, 524):
            wait = 2 ** attempt * 5
            print(f"       {resp.status_code} from {KIMI_MODEL}, retrying in {wait}s …")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            print(f"       error {resp.status_code} on {KIMI_MODEL}: {resp.text[:200]}")
        resp.raise_for_status()
        chunks = []
        for line in resp.iter_lines():
            if not line.startswith(b"data: "):
                continue
            data = line[len(b"data: "):]
            if data == b"[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (ValueError, KeyError, IndexError):
                continue  # usage/keepalive chunks
            chunks.append(delta.get("content") or "")
        text = "".join(chunks).strip()
        if text:
            return text
        print(f"       empty stream from {KIMI_MODEL}, retrying …")
    raise RuntimeError(f"{KIMI_MODEL} exhausted retries")


def call_llm(prompt):
    """Kimi K3 is primary; Gemini is the emergency fallback so the cron never sends nothing."""
    if KIMI_API_KEY:
        try:
            return call_kimi(prompt)
        except Exception as e:
            print(f"[warn] Kimi failed ({e}); falling back to Gemini")
    else:
        print("[warn] KIMI_API_KEY not set; using Gemini")
    return call_gemini(prompt)


def call_gemini(prompt):
    models = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        for attempt in range(5):
            resp = requests.post(url, json=payload, timeout=90)
            if resp.status_code in (429, 503):
                wait = 2 ** attempt * 5
                reason = "rate-limited" if resp.status_code == 429 else "unavailable"
                detail = ""
                try:
                    detail = resp.json().get("error", {}).get("message", "")
                except Exception:
                    detail = resp.text[:200]
                print(f"       {reason} on {model} ({resp.status_code}), retrying in {wait}s …")
                print(f"       detail: {detail}")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                detail = ""
                try:
                    detail = resp.json().get("error", {}).get("message", "")
                except Exception:
                    detail = resp.text[:200]
                print(f"       error {resp.status_code} on {model}: {detail}")
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"       {model} exhausted retries, trying next model …")

    raise RuntimeError("All Gemini models failed after retries")


# ── HTML email formatting ─────────────────────────────────────────────────────

FEEDBACK_EMAIL = "ian@afterquery.com"


def feedback_block(today):
    # Email clients strip real forms; prefilled mailto links are the portable "quick form".
    def mailto(rating):
        subject = quote(f"Digest feedback ({today}): {rating}")
        return f"mailto:{FEEDBACK_EMAIL}?subject={subject}"

    link_style = (
        "display:inline-block;padding:8px 14px;margin:0 4px;border-radius:8px;"
        "background:#334155;color:#e2e8f0;text-decoration:none;font-size:14px;"
    )
    return f"""
    <tr><td style="padding:28px 32px 4px;">
        <div style="background:#0f172a;border-radius:10px;padding:18px 20px;text-align:center;">
            <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#94a3b8;margin-bottom:12px;">
                HOW WAS TODAY'S DIGEST?
            </div>
            <a href="{mailto('Great')}" style="{link_style}">&#128293; Great</a>
            <a href="{mailto('Meh')}" style="{link_style}">&#128528; Meh</a>
            <a href="{mailto('Not useful')}" style="{link_style}">&#128078; Not useful</a>
            <div style="font-size:12px;color:#64748b;margin-top:12px;">
                One tap opens a pre-filled email &mdash; add a line if you like, or just hit reply.
            </div>
        </div>
    </td></tr>"""

SECTION_COLORS = {
    "MONEY TALK": "#10b981",
    "FRONTIER WATCH": "#06b6d4",
    "BENCHMARK BEAT": "#14b8a6",
    "WORLD LORE": "#6366f1",
    "TECH TEA": "#f59e0b",
    "DATA DIVE": "#ec4899",
}


def digest_to_html(raw_text):
    today = dt.date.today().strftime("%A, %B %-d, %Y")

    sections_html = ""
    current_section = None
    current_body = []

    def flush():
        nonlocal sections_html, current_section, current_body
        if current_section:
            color = SECTION_COLORS.get(current_section, "#64748b")
            body = "<br>".join(p for p in current_body if p)
            sections_html += f"""
            <tr><td style="padding:28px 32px 0;">
                <div style="
                    font-size:13px;font-weight:700;letter-spacing:2px;
                    color:{color};border-bottom:2px solid {color};
                    padding-bottom:6px;margin-bottom:14px;
                ">{current_section}</div>
                <div style="font-size:15px;line-height:1.7;color:#d1d5db;">
                    {body}
                </div>
            </td></tr>"""
        current_section = None
        current_body = []

    for line in raw_text.split("\n"):
        stripped = line.strip()
        upper = stripped.upper()
        if upper in SECTION_COLORS:
            flush()
            current_section = upper
        elif current_section:
            current_body.append(stripped)
        # skip lines before the first section

    flush()

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;">
<tr><td align="center" style="padding:24px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#1e293b;border-radius:12px;overflow:hidden;">
    <!-- header -->
    <tr><td style="
        background:linear-gradient(135deg,#1e293b 0%,#334155 100%);
        padding:32px;text-align:center;
    ">
        <div style="font-size:28px;font-weight:800;color:#f8fafc;letter-spacing:-0.5px;">
            Morning Digest
        </div>
        <div style="font-size:13px;color:#94a3b8;margin-top:6px;">
            {today}
        </div>
    </td></tr>

    {sections_html}

    {feedback_block(today)}

    <!-- footer -->
    <tr><td style="padding:28px 32px;text-align:center;">
        <div style="font-size:12px;color:#475569;">
            Brewed with Gemini &amp; too much coffee.
        </div>
    </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(html_body, recipients):
    today = dt.date.today().strftime("%b %-d")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        for recipient in recipients:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Your Morning Digest — {today}"
            msg["From"] = GMAIL_ADDRESS
            msg["Reply-To"] = FEEDBACK_EMAIL
            msg["To"] = recipient
            msg.attach(MIMEText("Your email client doesn't support HTML.", "plain"))
            msg.attach(MIMEText(html_body, "html"))
            server.sendmail(GMAIL_ADDRESS, [recipient], msg.as_string())
            print(f"[ok] digest sent to {recipient}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audience", choices=["broadcast", "personal"], default="broadcast")
    parser.add_argument(
        "--include-personal-in-broadcast",
        action="store_true",
        help="Send the broadcast copy to PERSONAL_EMAIL too. Off by default — the owner gets the personal copy only.",
    )
    args = parser.parse_args()
    is_personal = args.audience == "personal"

    # The former personal edition is the only edition now: Frontier Watch for everyone.
    # --audience only routes recipients (broadcast list vs PERSONAL_EMAIL).
    feeds = {}
    for category, urls in FEEDS.items():
        feeds[category] = urls
        if category == "Finance":
            feeds["Frontier Watch"] = FRONTIER_LAB_FEEDS

    if is_personal:
        recipients = PERSONAL_EMAILS
    else:
        recipients = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]
        if not args.include_personal_in_broadcast:
            personal = {p.lower() for p in PERSONAL_EMAILS}
            recipients = [r for r in recipients if r.lower() not in personal]

    print(f"[1/5] fetching RSS feeds ({args.audience}) …")
    articles = fetch_articles(feeds)
    total = sum(len(v) for v in articles.values())
    print(f"       pulled {total} articles across {len(articles)} categories")

    print("[2/5] searching X for benchmark chatter …")
    seen = load_seen_citations()
    x_items = fetch_x_benchmark_posts(set(seen or []))
    if x_items:
        articles.setdefault("Benchmark Beat", []).extend(x_items)

    print("[3/5] summarizing …")
    prompt = build_prompt(articles)
    raw_digest = call_llm(prompt)

    print("[4/5] building HTML email …")
    html = digest_to_html(raw_digest)

    print(f"[5/5] sending email to {len(recipients)} recipient(s) …")
    send_email(html, recipients)

    # Only after a successful send — a crash earlier shouldn't burn unseen posts.
    save_seen_citations(seen, [i["post_id"] for i in x_items])


if __name__ == "__main__":
    main()
