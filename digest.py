#!/usr/bin/env python3
"""Morning Digest — pulls news from RSS feeds, summarizes via Gemini, emails a polished digest."""

import os
import argparse
import calendar
import json
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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


# ── Gemini summarization ──────────────────────────────────────────────────────

def system_prompt():
    sections = [
        "**Money Talk** — finance & markets",
        "**Frontier Watch** — biggest releases & research from frontier AI labs "
        "(OpenAI, Anthropic, DeepMind, Meta AI, xAI, Mistral)",
        "**Benchmark Beat** — new AI benchmark results, eval releases, and leaderboard moves",
        "**World Lore** — geopolitics & global affairs",
        "**Tech Tea** — technology & innovation",
        "**Data Dive** — AI research, ML engineering & data science",
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

    print(f"[1/4] fetching RSS feeds ({args.audience}) …")
    articles = fetch_articles(feeds)
    total = sum(len(v) for v in articles.values())
    print(f"       pulled {total} articles across {len(articles)} categories")

    print("[2/4] summarizing …")
    prompt = build_prompt(articles)
    raw_digest = call_llm(prompt)

    print("[3/4] building HTML email …")
    html = digest_to_html(raw_digest)

    print(f"[4/4] sending email to {len(recipients)} recipient(s) …")
    send_email(html, recipients)


if __name__ == "__main__":
    main()
