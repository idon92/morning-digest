#!/usr/bin/env python3
"""Morning Digest — pulls news from RSS feeds, summarizes via Gemini, emails a polished digest."""

import argparse
import os
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
    "Creator Economy": [
        "https://digiday.com/feed/",
        "https://www.socialmediatoday.com/feed/",
    ],
}

MAX_ARTICLES_PER_FEED = 3


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_articles():
    """Return {category: [{'title': ..., 'link': ..., 'summary': ...}, ...]}."""
    articles = {}
    for category, urls in FEEDS.items():
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

SYSTEM_PROMPT = (
    "You are a sharp, witty friend who actually reads the news. "
    "Given today's articles, write a morning digest with EXACTLY these five sections:\n\n"
    "1. **Money Talk** — finance & markets\n"
    "2. **World Lore** — geopolitics & global affairs\n"
    "3. **Tech Tea** — technology & innovation\n"
    "4. **Creator Szn** — creator economy & social platforms\n"
    "5. **Speed Round** — 5-7 punchy one-liners covering the most interesting bits across all categories\n\n"
    "For each section (except Speed Round) write 2-3 short paragraphs. "
    "Be insightful but conversational — like a group chat, not a boardroom. "
    "Use plain text (no markdown), just section headers in ALL CAPS followed by a blank line."
)


def build_prompt(articles):
    parts = []
    for category, items in articles.items():
        parts.append(f"=== {category.upper()} ===")
        for a in items:
            parts.append(f"- {a['title']}\n  {a['summary']}\n  {a['link']}")
        parts.append("")
    return SYSTEM_PROMPT + "\n\nHere are today's articles:\n\n" + "\n".join(parts)


def call_gemini(prompt):
    models = ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.5-flash"]
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        for attempt in range(3):
            resp = requests.post(url, json=payload, timeout=90)
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"       rate-limited on {model}, retrying in {wait}s …")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"       {model} exhausted retries, trying next model …")

    raise RuntimeError("All Gemini models failed after retries")


# ── HTML email formatting ─────────────────────────────────────────────────────

SECTION_COLORS = {
    "MONEY TALK": "#10b981",
    "WORLD LORE": "#6366f1",
    "TECH TEA": "#f59e0b",
    "CREATOR SZN": "#ec4899",
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

def send_email(html_body):
    recipients = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]
    today = dt.date.today().strftime("%b %-d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your Morning Digest — {today}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("Your email client doesn't support HTML.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    print(f"[ok] digest sent to {', '.join(recipients)}")


# ── Shareable link via GitHub Gist ─────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def upload_gist(html_body):
    """Upload HTML to a public GitHub Gist and return the viewable URL."""
    today = dt.date.today().strftime("%Y-%m-%d")
    filename = f"morning-digest-{today}.html"
    resp = requests.post(
        "https://api.github.com/gists",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "description": f"Morning Digest — {today}",
            "public": True,
            "files": {filename: {"content": html_body}},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_url = data["files"][filename]["raw_url"]
    preview_url = f"https://htmlpreview.github.io/?{raw_url}"
    short_url = shorten_url(preview_url)
    return data["html_url"], preview_url, short_url


def shorten_url(url):
    """Shorten a URL via is.gd (free, no auth)."""
    try:
        resp = requests.get(
            "https://is.gd/create.php",
            params={"format": "simple", "url": url},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("[1/4] fetching RSS feeds …")
    articles = fetch_articles()
    total = sum(len(v) for v in articles.values())
    print(f"       pulled {total} articles across {len(articles)} categories")

    print("[2/4] sending to Gemini …")
    prompt = build_prompt(articles)
    raw_digest = call_gemini(prompt)

    print("[3/4] building HTML email …")
    html = digest_to_html(raw_digest)

    # Always save locally
    local_path = os.path.join(os.path.dirname(__file__), "digest.html")
    with open(local_path, "w") as f:
        f.write(html)

    # Upload shareable link
    if GITHUB_TOKEN:
        print("[4/5] uploading shareable link …")
        try:
            gist_url, preview_url, short_url = upload_gist(html)
            print(f"       gist:    {gist_url}")
            print(f"       preview: {preview_url}")
            if short_url:
                print(f"       share:   {short_url}")
        except Exception as e:
            print(f"[warn] gist upload failed: {e}")
    else:
        print("[skip] no GITHUB_TOKEN set, skipping shareable link")

    # Send email
    print(f"[{'5' if GITHUB_TOKEN else '4'}/{'5' if GITHUB_TOKEN else '4'}] sending email …")
    try:
        send_email(html)
    except Exception as e:
        print(f"[warn] email failed: {e}")
        print(f"[ok] saved fallback to {local_path}")


def send_last():
    """Send the most recently generated digest.html to all recipients."""
    local_path = os.path.join(os.path.dirname(__file__), "digest.html")
    if not os.path.exists(local_path):
        print("[error] no digest.html found — run `python3 digest.py` first to generate one")
        return
    with open(local_path) as f:
        html = f.read()
    print(f"[1/1] sending last digest to recipients …")
    send_email(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Morning Digest")
    parser.add_argument("--send", action="store_true",
                        help="Email the last generated digest.html to all recipients")
    args = parser.parse_args()

    if args.send:
        send_last()
    else:
        main()
