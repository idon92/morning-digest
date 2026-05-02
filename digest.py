#!/usr/bin/env python3
"""Morning Digest — pulls news from RSS feeds, summarizes via Gemini, emails a polished digest."""

import os
import argparse
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

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
PERSONAL_EMAIL = os.environ.get("PERSONAL_EMAIL", "ianisaiahdon@gmail.com")

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
        "https://nathanlambertai.substack.com/feed",
        "https://sebastianraschka.substack.com/feed",
        "https://www.deeplearning.ai/the-batch/rss/",
    ],
    "Catalyst Calendar": [
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    ],
}

# Frontier-lab feeds for the personal-only "Frontier Watch" section.
# OpenAI and DeepMind expose native RSS; the rest are pulled from the
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
]

MAX_ARTICLES_PER_FEED = 3


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_articles(feeds):
    """Return {category: [{'title': ..., 'link': ..., 'summary': ...}, ...]}."""
    articles = {}
    for category, urls in feeds.items():
        items = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
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

def system_prompt(include_frontier):
    sections = ["**Money Talk** — finance & markets"]
    if include_frontier:
        sections.append(
            "**Frontier Watch** — biggest releases & research from frontier AI labs "
            "(OpenAI, Anthropic, DeepMind, Meta AI, xAI)"
        )
    sections.extend([
        "**World Lore** — geopolitics & global affairs",
        "**Tech Tea** — technology & innovation",
        "**Data Dive** — AI research, ML engineering & data science",
        "**Catalyst Calendar** — upcoming economic data releases, Fed activity, "
        "and notable report publishings that could move markets",
        "**Speed Round** — 5-7 punchy one-liners covering the most interesting bits across all categories",
    ])
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sections))
    return (
        "You are a witty investment and technology expert who actually reads the news. "
        f"Given today's articles, write a morning digest with EXACTLY these {len(sections)} sections:\n\n"
        f"{numbered}\n\n"
        "For each section (except Speed Round) write 2-3 short paragraphs. "
        "Be insightful but conversational — like a group chat, not a boardroom. "
        "Use plain text (no markdown), just section headers in ALL CAPS followed by a blank line."
    )


def build_prompt(articles, include_frontier):
    parts = []
    for category, items in articles.items():
        parts.append(f"=== {category.upper()} ===")
        for a in items:
            parts.append(f"- {a['title']}\n  {a['summary']}\n  {a['link']}")
        parts.append("")
    return system_prompt(include_frontier) + "\n\nHere are today's articles:\n\n" + "\n".join(parts)


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
    "WORLD LORE": "#6366f1",
    "TECH TEA": "#f59e0b",
    "DATA DIVE": "#ec4899",
    "CATALYST CALENDAR": "#ef4444",
    "SPEED ROUND": "#8b5cf6",
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
    args = parser.parse_args()
    is_personal = args.audience == "personal"

    # Personal digest gets a Frontier Watch section inserted right after Finance.
    feeds = {}
    for category, urls in FEEDS.items():
        feeds[category] = urls
        if is_personal and category == "Finance":
            feeds["Frontier Watch"] = FRONTIER_LAB_FEEDS

    recipients = (
        [PERSONAL_EMAIL]
        if is_personal
        else [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]
    )

    print(f"[1/4] fetching RSS feeds ({args.audience}) …")
    articles = fetch_articles(feeds)
    total = sum(len(v) for v in articles.values())
    print(f"       pulled {total} articles across {len(articles)} categories")

    print("[2/4] sending to Gemini …")
    prompt = build_prompt(articles, include_frontier=is_personal)
    raw_digest = call_gemini(prompt)

    print("[3/4] building HTML email …")
    html = digest_to_html(raw_digest)

    print(f"[4/4] sending email to {len(recipients)} recipient(s) …")
    send_email(html, recipients)


if __name__ == "__main__":
    main()
