#!/usr/bin/env python3
"""
GrantSignal Thursday Reminder Pipeline
---------------------------------------
Every Thursday, checks grants from the most recent digest for upcoming deadlines
(closing within 7 days). If any are found, sends a reminder email to all
premium subscribers via Resend.

Run: python scripts/reminders.py
Env vars required (or set as GitHub Actions secrets):
  BEEHIIV_API_KEY, BEEHIIV_PUB_ID, RESEND_API_KEY, FROM_EMAIL
Optional:
  DRY_RUN=true    — fetch and parse, but skip sending emails
  GITHUB_TOKEN    — used to load archive from repo (auto-set in Actions)
"""

import os
import sys
import json
import re
import datetime
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID  = os.environ.get("BEEHIIV_PUB_ID",  "")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",   "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "digest@grantsignal.news")
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

# GITHUB_TOKEN is auto-injected in GitHub Actions
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GITHUB_API_BASE = "https://api.github.com/repos/Agent17D/grantsignal-site"

# Brand colours
NAVY = "#0f3460"
TEAL = "#00897b"

# ---------------------------------------------------------------------------
# Step 1: Load most recent digest entry from archive/issues.json
# ---------------------------------------------------------------------------

def load_latest_issue() -> dict | None:
    """
    Fetch archive/issues.json from GitHub and return the most recent issue entry.
    Returns None on failure.
    """
    url = f"{GITHUB_API_BASE}/contents/archive/issues.json"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            print("[archive] archive/issues.json not found — nothing to remind about")
            return None
        resp.raise_for_status()
        data = resp.json()
        import base64
        raw = base64.b64decode(data["content"]).decode("utf-8")
        issues = json.loads(raw)
    except Exception as exc:
        print(f"[archive] ERROR loading issues.json: {exc}")
        return None

    if not issues:
        print("[archive] issues.json is empty")
        return None

    # Issues are stored with most recent last (appended) or first — sort by date
    def parse_date(entry):
        try:
            return datetime.date.fromisoformat(entry.get("date", "1970-01-01"))
        except Exception:
            return datetime.date(1970, 1, 1)

    latest = max(issues, key=parse_date)
    print(f"[archive] Most recent issue: {latest.get('date')} — {latest.get('slug', 'unknown')}")
    return latest


# ---------------------------------------------------------------------------
# Step 2: Load archive HTML for the latest issue
# ---------------------------------------------------------------------------

def load_archive_html(issue: dict) -> str | None:
    """
    Fetch the archive HTML for a given issue entry.
    Returns HTML string or None on failure.
    """
    slug = issue.get("slug", "")
    if not slug:
        print("[archive] Issue entry has no slug")
        return None

    # Construct path: archive/{slug}.html
    archive_path = f"archive/{slug}.html"
    url = f"{GITHUB_API_BASE}/contents/{archive_path}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            print(f"[archive] {archive_path} not found")
            return None
        resp.raise_for_status()
        data = resp.json()
        import base64
        html = base64.b64decode(data["content"]).decode("utf-8")
        print(f"[archive] Loaded {archive_path} ({len(html):,} bytes)")
        return html
    except Exception as exc:
        print(f"[archive] ERROR loading {archive_path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Step 3: Parse grant close dates from archive HTML
# ---------------------------------------------------------------------------

# Supported month abbreviations
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Regex patterns to match close date strings in HTML
# Matches: "Closes May 1, 2026" or "Closes Apr 1, 2026"
CLOSE_DATE_PATTERN = re.compile(
    r'Closes\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})',
    re.IGNORECASE
)

# Also try to match the grant title and Grants.gov link in the surrounding context
GRANT_CARD_PATTERN = re.compile(
    r'<[^>]*class="[^"]*grant[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE
)


def parse_close_date(text: str) -> datetime.date | None:
    """Parse a date string like 'May 1, 2026' into a date object."""
    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$', text.strip())
    if not m:
        return None
    month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
    month = MONTH_MAP.get(month_str.lower())
    if not month:
        return None
    try:
        return datetime.date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def extract_grants_with_deadlines(html: str) -> list[dict]:
    """
    Parse archive HTML to extract grants with their close dates.
    Returns list of dicts: {title, close_date, url}
    """
    grants = []

    # Find all grant title + close date combinations
    # Look for anchor tags with grant titles, then nearby close date text
    # Strategy: split by grant-card divs or similar markers

    # Extract grant cards (divs with class containing 'grant-card' or 'grant')
    # Try to find title, link, and close date for each grant block
    title_pattern = re.compile(r'<h3[^>]*class="[^"]*grant-title[^"]*"[^>]*>.*?<a[^>]+href="([^"]*)"[^>]*>([^<]+)</a>', re.DOTALL | re.IGNORECASE)
    # Also try simpler link patterns
    link_pattern = re.compile(r'href="(https?://[^"]*grants\.gov[^"]*)"', re.IGNORECASE)

    # Find all close date occurrences with surrounding context
    for match in CLOSE_DATE_PATTERN.finditer(html):
        month_str = match.group(1)
        day_str = match.group(2)
        year_str = match.group(3)

        close_date = parse_close_date(f"{month_str} {day_str}, {year_str}")
        if not close_date:
            continue

        # Get surrounding context (300 chars before the match) to find title and URL
        start = max(0, match.start() - 1500)
        context = html[start:match.end() + 200]

        # Try to find grant title in context
        title = "Unknown Grant"
        title_m = re.search(r'class="grant-title"[^>]*>.*?<a[^>]+>([^<]+)</a>', context, re.DOTALL | re.IGNORECASE)
        if title_m:
            title = title_m.group(1).strip()
        else:
            # Fallback: find any <a> tag text near this date
            any_link = re.search(r'<a[^>]+>([^<]{10,120})</a>', context, re.DOTALL | re.IGNORECASE)
            if any_link:
                title = re.sub(r'\s+', ' ', any_link.group(1)).strip()

        # Try to find Grants.gov URL in context
        url = "https://www.grants.gov"
        url_m = re.search(r'href="(https?://[^"]*grants\.gov[^"]*)"', context, re.IGNORECASE)
        if url_m:
            url = url_m.group(1)

        grants.append({
            "title": title,
            "close_date": close_date,
            "url": url,
        })

    print(f"[parse] Found {len(grants)} grants with close dates in archive HTML")
    return grants


# ---------------------------------------------------------------------------
# Step 4: Filter for grants closing within 7 days
# ---------------------------------------------------------------------------

def filter_urgent_grants(grants: list[dict], today: datetime.date | None = None) -> list[dict]:
    """Return grants closing within 7 days from today."""
    if today is None:
        today = datetime.date.today()

    urgent = []
    for g in grants:
        close_date = g.get("close_date")
        if not isinstance(close_date, datetime.date):
            continue
        days_until = (close_date - today).days
        if 0 <= days_until <= 7:
            urgent.append({**g, "days_until": days_until})

    urgent.sort(key=lambda x: x["close_date"])
    return urgent


# ---------------------------------------------------------------------------
# Step 5: Build reminder email HTML
# ---------------------------------------------------------------------------

def _escape(s: str) -> str:
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_reminder_html(urgent_grants: list[dict]) -> str:
    """Build the reminder email HTML for urgent grants."""
    n = len(urgent_grants)
    today = datetime.date.today()
    week_str = today.strftime("%B %d, %Y")

    # Build grant cards
    cards_html = ""
    for g in urgent_grants:
        title = _escape(g.get("title", "Unknown Grant"))
        url = _escape(g.get("url", "https://www.grants.gov"))
        close_date = g.get("close_date")
        days = g.get("days_until", 0)

        if close_date:
            close_str = close_date.strftime("%B %d, %Y")
        else:
            close_str = "Unknown"

        if days == 0:
            urgency_label = "Closes TODAY"
            urgency_color = "#c62828"
        elif days == 1:
            urgency_label = "Closes TOMORROW"
            urgency_color = "#c62828"
        elif days <= 3:
            urgency_label = f"Closes in {days} days"
            urgency_color = "#e65100"
        else:
            urgency_label = f"Closes in {days} days"
            urgency_color = "#f57c00"

        cards_html += f"""
      <div style="background:#f4f7fb;border:1px solid #dde4ed;border-left:4px solid {urgency_color};
                  border-radius:8px;padding:18px 20px;margin-bottom:16px;">
        <div style="font-size:13px;font-weight:700;color:{urgency_color};margin-bottom:6px;">
          ⚡ {_escape(urgency_label)} — {_escape(close_str)}
        </div>
        <div style="font-size:16px;font-weight:700;color:#0f3460;margin-bottom:8px;line-height:1.4;">
          <a href="{url}" style="color:#0f3460;text-decoration:none;">{title}</a>
        </div>
        <a href="{url}" target="_blank"
           style="display:inline-block;background:#00897b;color:#fff;font-size:13px;
                  font-weight:700;padding:8px 18px;border-radius:6px;text-decoration:none;">
          View on Grants.gov →
        </a>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GrantSignal Deadline Alert</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;
             border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(15,52,96,0.10);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f3460 0%,#163d6e 100%);padding:28px 32px;text-align:center;">
            <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-0.5px;margin-bottom:4px;">
              ⚡ GrantSignal
            </div>
            <div style="font-size:13px;color:#90b8d8;">Deadline Alert — {_escape(week_str)}</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:28px 32px;">
            <h2 style="font-size:20px;font-weight:800;color:#0f3460;margin:0 0 8px;line-height:1.3;">
              {n} Grant{"s" if n != 1 else ""} Closing This Week
            </h2>
            <p style="font-size:15px;color:#5a6a7a;margin:0 0 24px;line-height:1.6;">
              These grants from this week's digest are closing within 7 days. Don't miss them.
            </p>

            {cards_html}

            <p style="font-size:13px;color:#8a9ab0;margin:24px 0 0;line-height:1.6;">
              Review this week's full digest for more opportunities and fit scores.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f4f7fb;padding:20px 32px;border-top:1px solid #dde4ed;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:11px;color:#8a9ab0;line-height:1.7;">
                  📡 <strong>GrantSignal</strong> — Federal Grant Discovery for Nonprofits &amp; Schools<br>
                  1401 Westbank Expressway, Suite 109, Westwego, LA 70094<br>
                  You're receiving this because you're a GrantSignal Premium subscriber.<br>
                  <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;">Unsubscribe</a> &nbsp;·&nbsp;
                  <a href="https://grantsignal.news" style="color:#00897b;">grantsignal.news</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 6: Fetch premium subscribers from Beehiiv
# ---------------------------------------------------------------------------

def fetch_premium_subscribers() -> list[str]:
    """Fetch all active premium subscriber emails from Beehiiv."""
    print("[beehiiv] Fetching premium subscribers…")
    emails = []
    page = 1
    per_page = 100

    while True:
        url = (
            f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}"
            f"/subscriptions"
        )
        params = {
            "status":  "active",
            "tier":    "premium",
            "page":    page,
            "limit":   per_page,
        }
        headers = {
            "Authorization": f"Bearer {BEEHIIV_API_KEY}",
            "Content-Type":  "application/json",
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"[beehiiv] ERROR fetching premium subscribers (page {page}): {exc}")
            break

        subs = data.get("data", [])
        if not subs:
            break

        for sub in subs:
            email = sub.get("email")
            if email:
                emails.append(email)

        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    print(f"[beehiiv] Found {len(emails)} premium subscribers.")
    return emails


# ---------------------------------------------------------------------------
# Step 7: Send reminder emails via Resend
# ---------------------------------------------------------------------------

def send_reminder_emails(subscribers: list[str], subject: str, html_body: str) -> None:
    """Send reminder emails to each premium subscriber via Resend."""
    if not subscribers:
        print("[resend] No recipients — skipping.")
        return

    if DRY_RUN:
        print(f"[dry_run] Would send reminder to {len(subscribers)} premium subscribers.")
        print(f"[dry_run] Subject: {subject}")
        return

    print(f"[resend] Sending reminder to {len(subscribers)} premium subscribers…")
    success = 0
    errors = 0

    for i, email in enumerate(subscribers, start=1):
        payload = {
            "from":    FROM_EMAIL,
            "to":      [email],
            "subject": subject,
            "html":    html_body,
        }
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            success += 1
        except requests.RequestException as exc:
            errors += 1
            print(f"[resend] WARN failed to send to {email}: {exc}")

        if i % 10 == 0:
            print(f"[resend] Progress: {i}/{len(subscribers)} sent…")

    print(f"[resend] Reminder complete — {success} sent, {errors} errors.")




# ---------------------------------------------------------------------------
# SAM.gov Expiration Reminder Functions
# ---------------------------------------------------------------------------

def load_subscriber_preferences() -> list[dict]:
    """
    Load all preference files from data/preferences/ in GitHub.
    Returns a list of preference dicts (each with at least 'email' and optionally 'sam_expiry').
    """
    url = f"{GITHUB_API_BASE}/contents/data/preferences"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            print("[prefs] data/preferences/ not found — skipping SAM.gov reminders")
            return []
        resp.raise_for_status()
        files = resp.json()
    except Exception as exc:
        print(f"[prefs] ERROR listing preferences directory: {exc}")
        return []

    prefs_list = []
    import base64
    for f in files:
        if not f.get("name", "").endswith(".json"):
            continue
        try:
            file_resp = requests.get(f["url"], headers=headers, timeout=15)
            file_resp.raise_for_status()
            file_data = file_resp.json()
            raw = base64.b64decode(file_data["content"]).decode("utf-8")
            prefs = json.loads(raw)
            prefs_list.append(prefs)
        except Exception as exc:
            print(f"[prefs] WARN could not load {f.get('name')}: {exc}")

    print(f"[prefs] Loaded {len(prefs_list)} preference files.")
    return prefs_list


def build_sam_reminder_html(email: str, days_until: int, expiry_month: str, expiry_year: int) -> str:
    """Build SAM.gov expiration reminder email HTML."""
    today = datetime.date.today()
    week_str = today.strftime("%B %d, %Y")

    if days_until <= 0:
        header_label = "SAM.gov Registration Expired"
        intro = (
            f"Your SAM.gov entity registration may have expired as of "
            f"{expiry_month} {expiry_year}."
        )
        urgency_color = "#c62828"
        urgency_icon = "🚨"
    elif days_until <= 30:
        header_label = "SAM.gov Registration Expiring Soon"
        intro = (
            f"Your SAM.gov entity registration expires on "
            f"{expiry_month} {expiry_year} — in {days_until} day{'s' if days_until != 1 else ''}."
        )
        urgency_color = "#e65100"
        urgency_icon = "⚠️"
    else:
        header_label = "SAM.gov Registration Expiring in 60 Days"
        intro = (
            f"Your SAM.gov entity registration expires on "
            f"{expiry_month} {expiry_year} — in {days_until} days."
        )
        urgency_color = "#f57c00"
        urgency_icon = "⚠️"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GrantSignal SAM.gov Reminder</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;
             border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(15,52,96,0.10);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f3460 0%,#163d6e 100%);padding:28px 32px;text-align:center;">
            <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-0.5px;margin-bottom:4px;">
              {urgency_icon} GrantSignal
            </div>
            <div style="font-size:13px;color:#90b8d8;">{header_label} — {week_str}</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:28px 32px;">
            <h2 style="font-size:20px;font-weight:800;color:#0f3460;margin:0 0 12px;line-height:1.3;">
              {intro}
            </h2>
            <p style="font-size:15px;color:#5a6a7a;margin:0 0 16px;line-height:1.6;">
              An expired SAM.gov registration will disqualify you from applying for federal grants.
            </p>
            <p style="font-size:15px;color:#5a6a7a;margin:0 0 24px;line-height:1.6;">
              Renewing takes 10–15 minutes at
              <a href="https://sam.gov/content/entity-registration" style="color:#1565c0;">sam.gov/content/entity-registration</a>.
            </p>

            <!-- CTA Button -->
            <div style="text-align:center;margin:24px 0;">
              <a href="https://sam.gov/content/entity-registration" target="_blank"
                 style="display:inline-block;background:#00897b;color:#fff;font-size:15px;
                        font-weight:700;padding:14px 32px;border-radius:8px;text-decoration:none;">
                Renew at SAM.gov →
              </a>
            </div>

            <p style="font-size:13px;color:#8a9ab0;margin:24px 0 0;line-height:1.6;">
              This reminder was sent because you entered your SAM.gov expiration date during GrantSignal onboarding.
              Premium subscribers receive automatic reminders 60 and 30 days before expiration.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f4f7fb;padding:20px 32px;border-top:1px solid #dde4ed;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:11px;color:#8a9ab0;line-height:1.7;">
                  📡 <strong>GrantSignal</strong> — Federal Grant Discovery for Nonprofits &amp; Schools<br>
                  1401 Westbank Expressway, Suite 109, Westwego, LA 70094<br>
                  You're receiving this because you're a GrantSignal subscriber with a SAM.gov expiration date on file.<br>
                  <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;">Unsubscribe</a> &nbsp;·&nbsp;
                  <a href="https://grantsignal.news" style="color:#00897b;">grantsignal.news</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def check_sam_expiry_reminders() -> None:
    """
    Check all subscribers with SAM.gov expiration dates and send reminders as needed.
    Called at the end of the main reminders flow.
    """
    import calendar

    print("\n[sam] Checking SAM.gov expiration reminders…")
    prefs_list = load_subscriber_preferences()

    if not prefs_list:
        print("[sam] No SAM.gov reminders needed this week.")
        return

    today = datetime.date.today()
    reminders_sent = 0

    for prefs in prefs_list:
        sam_expiry = prefs.get("sam_expiry", "") or ""
        email = prefs.get("email", "") or ""

        if not sam_expiry or not email:
            continue

        # Parse "YYYY-MM" format from month input
        try:
            parts = sam_expiry.strip().split("-")
            if len(parts) != 2:
                continue
            expiry_year = int(parts[0])
            expiry_month_num = int(parts[1])
            # Use last day of the month as expiry date
            last_day = calendar.monthrange(expiry_year, expiry_month_num)[1]
            expiry_date = datetime.date(expiry_year, expiry_month_num, last_day)
        except (ValueError, IndexError) as exc:
            print(f"[sam] WARN could not parse sam_expiry '{sam_expiry}' for {email}: {exc}")
            continue

        days_until = (expiry_date - today).days
        expiry_month_name = expiry_date.strftime("%B")

        # Determine if this subscriber needs a reminder
        if days_until <= 60 and days_until > 30:
            reminder_type = "60-day"
            subject = f"⚠️ Your SAM.gov registration expires in {days_until} days"
        elif days_until <= 30 and days_until > 0:
            reminder_type = "30-day"
            subject = f"⚠️ Your SAM.gov registration expires in {days_until} days"
        elif days_until <= 0:
            reminder_type = "expired"
            subject = "🚨 Your SAM.gov registration may have expired"
        else:
            # More than 60 days away — no reminder needed yet
            continue

        print(f"[sam] {reminder_type} reminder → {email} (expires {expiry_date}, {days_until}d)")

        html_body = build_sam_reminder_html(email, days_until, expiry_month_name, expiry_year)

        if DRY_RUN:
            print(f"[dry_run] Would send SAM.gov {reminder_type} reminder to {email}")
            print(f"[dry_run] Subject: {subject}")
            reminders_sent += 1
            continue

        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                json={
                    "from":    FROM_EMAIL,
                    "to":      [email],
                    "subject": subject,
                    "html":    html_body,
                },
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            reminders_sent += 1
            print(f"[sam] ✓ Sent {reminder_type} reminder to {email}")
        except requests.RequestException as exc:
            print(f"[sam] WARN failed to send SAM.gov reminder to {email}: {exc}")

    if reminders_sent == 0:
        print("[sam] No SAM.gov reminders needed this week.")
    else:
        print(f"[sam] SAM.gov reminders complete — {reminders_sent} sent.")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  GrantSignal Thursday Reminder Pipeline")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if DRY_RUN:
        print("  *** DRY RUN — emails will NOT be sent ***")
    print("=" * 60)

    # Validate required env vars
    missing = [v for v in ("BEEHIIV_API_KEY", "BEEHIIV_PUB_ID",
                            "RESEND_API_KEY", "FROM_EMAIL")
               if not os.environ.get(v)]
    if missing and not DRY_RUN:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # ── 1. Load most recent digest issue ────────────────────────────────────
    latest_issue = load_latest_issue()
    if not latest_issue:
        print("No digest issues found. Exiting.")
        sys.exit(0)

    # ── 2. Load archive HTML ─────────────────────────────────────────────────
    html = load_archive_html(latest_issue)
    if not html:
        print("Could not load archive HTML. Exiting.")
        sys.exit(0)

    # ── 3. Parse grants with close dates ─────────────────────────────────────
    grants = extract_grants_with_deadlines(html)
    if not grants:
        print("No grants with close dates found in archive HTML. Exiting.")
        sys.exit(0)

    # ── 4. Filter for urgent (closing within 7 days) ─────────────────────────
    today = datetime.date.today()
    urgent = filter_urgent_grants(grants, today)

    if not urgent:
        print(f"No urgent grants (closing within 7 days of {today}) — skipping reminder.")
        sys.exit(0)

    print(f"[urgent] {len(urgent)} grant(s) closing within 7 days:")
    for g in urgent:
        print(f"  • {g['close_date']} ({g['days_until']}d) — {g['title'][:60]}")

    # ── 5. Build reminder email ───────────────────────────────────────────────
    n = len(urgent)
    subject = f"⚡ GrantSignal | {n} Grant{'s' if n != 1 else ''} Closing This Week"
    reminder_html = build_reminder_html(urgent)

    # ── 6. Fetch premium subscribers ─────────────────────────────────────────
    subscribers = fetch_premium_subscribers()

    # ── 7. Send emails ────────────────────────────────────────────────────────
    send_reminder_emails(subscribers, subject, reminder_html)

    # ── SAM.gov expiry reminders ─────────────────────────────────────────────
    check_sam_expiry_reminders()

    print("=" * 60)
    print("  Reminder pipeline complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
