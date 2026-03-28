#!/usr/bin/env python3
"""
GrantSignal Weekly Digest Pipeline
-----------------------------------
Fetches posted grants from Grants.gov, scores them for nonprofit/school
relevance, builds FREE (top 3) and PAID (top 50) digest emails, sends them
via Resend to segmented Beehiiv subscribers, and publishes an archive post
on Beehiiv.

Run: python digest.py
Env vars required (or set as GitHub Actions secrets):
  BEEHIIV_API_KEY, BEEHIIV_PUB_ID, RESEND_API_KEY, FROM_EMAIL
"""

import os
import sys
import json
import math
import datetime
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID  = os.environ.get("BEEHIIV_PUB_ID",  "")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",  "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "digest@grantsignal.news")

GRANTS_GOV_ENDPOINT = "https://api.grants.gov/v1/api/search2"

# Maximum grants to include in the paid digest
MAX_PAID_GRANTS = 50

# Minimum fit score (out of 5) to include a grant at all
MIN_SCORE = 1

# Days before close date that triggers the urgency flag
URGENCY_DAYS = 14

# Brand colours
NAVY  = "#0f3460"
BLUE  = "#1565c0"
TEAL  = "#00897b"

# ---------------------------------------------------------------------------
# Eligibility filter keywords
# Grants.gov uses coded eligibility strings; we also scan the synopsis/title.
# ---------------------------------------------------------------------------

ELIGIBLE_CODES = {
    "nonprofits",          # Nonprofits Having a 501(c)(3) Status…
    "private",             # Private institutions of higher education
    "public",              # Public and State controlled institutions
    "independent",         # Independent school districts
    "special",             # Special district governments
    "small",               # Small businesses (excluded in scoring but not filtered)
    "unrestricted",        # Unrestricted (open to all)
    "other",               # Other (see text field for clarification)
}

# Keywords that positively indicate nonprofit/school eligibility in free-text
NONPROFIT_KEYWORDS = [
    "nonprofit", "non-profit", "501(c)(3)", "501c3",
    "community organization", "community-based",
    "school", "school district", "education", "educational institution",
    "university", "college", "higher education",
    "faith-based", "faith based",
    "public library", "library system",
]

# ---------------------------------------------------------------------------
# Scoring keywords — grouped by category with weights
# ---------------------------------------------------------------------------

SCORING_CATEGORIES = {
    "education":             ["education", "school", "learning", "literacy", "stem",
                              "tutoring", "after-school", "after school", "student",
                              "teacher", "curriculum", "early childhood"],
    "health":                ["health", "mental health", "substance abuse", "opioid",
                              "nutrition", "wellness", "public health", "clinic",
                              "behavioral health", "maternal", "infant", "senior health"],
    "arts":                  ["arts", "culture", "music", "theater", "theatre",
                              "humanities", "creative", "heritage", "museum", "library"],
    "environment":           ["environment", "climate", "conservation", "sustainability",
                              "clean energy", "renewable", "watershed", "wildlife",
                              "green infrastructure", "resilience"],
    "community_development": ["community development", "economic development",
                              "workforce", "job training", "small business",
                              "revitalization", "neighborhood", "rural development",
                              "urban development", "capacity building"],
    "youth":                 ["youth", "children", "juvenile", "teen", "adolescent",
                              "child welfare", "foster", "mentoring", "mentorship"],
    "housing":               ["housing", "affordable housing", "homelessness", "shelter",
                              "transitional housing", "rental assistance", "homeownership"],
    "social_services":       ["social service", "food bank", "food security", "hunger",
                              "disability", "veteran", "refugee", "immigrant",
                              "domestic violence", "human trafficking", "poverty",
                              "low-income", "low income", "underserved"],
}


# ---------------------------------------------------------------------------
# Step 1: Fetch grants from Grants.gov
# ---------------------------------------------------------------------------

def fetch_grants(max_records: int = 500) -> list[dict]:
    """
    POST to the Grants.gov search2 API and return a flat list of opportunity dicts.
    Paginates automatically until max_records is reached or results are exhausted.
    """
    print(f"[grants.gov] Fetching posted grants (max {max_records})…")
    all_hits = []
    page_size = 100  # Grants.gov allows up to 100 per page
    offset = 0

    while len(all_hits) < max_records:
        payload = {
            "oppStatuses": "posted",
            "rows": page_size,
            "startRecord": offset,
            # Request all fields we need
            "fields": (
                "id,oppNumber,title,agencyName,openDate,closeDate,"
                "fundingCategory,eligibilities,synopsis"
            ),
        }

        try:
            resp = requests.post(GRANTS_GOV_ENDPOINT, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"[grants.gov] ERROR fetching page offset={offset}: {exc}")
            break
        except json.JSONDecodeError as exc:
            print(f"[grants.gov] ERROR decoding JSON at offset={offset}: {exc}")
            break

        hits = data.get("data", {}).get("oppHits", [])
        if not hits:
            print(f"[grants.gov] No more results at offset={offset}.")
            break

        all_hits.extend(hits)
        print(f"[grants.gov] Retrieved {len(all_hits)} grants so far…")

        total = data.get("data", {}).get("hitCount", 0)
        if len(all_hits) >= total:
            break
        offset += page_size

    print(f"[grants.gov] Total fetched: {len(all_hits)} grants.")
    return all_hits[:max_records]


# ---------------------------------------------------------------------------
# Step 2: Filter for nonprofit / school eligibility
# ---------------------------------------------------------------------------

def is_eligible(grant: dict) -> bool:
    """
    Return True if the grant appears eligible for nonprofits or schools.
    We check the structured eligibilities field AND scan title/synopsis text.
    """
    # Check structured eligibilities list
    eligibilities = grant.get("eligibilities", []) or []
    for elig in eligibilities:
        label = (elig.get("label", "") or "").lower()
        code  = (elig.get("code",  "") or "").lower()
        for kw in ELIGIBLE_CODES:
            if kw in label or kw in code:
                return True
        # "unrestricted" means open to all — definitely include
        if "unrestricted" in label or "unrestricted" in code:
            return True

    # Fall back to free-text scan
    text = " ".join([
        grant.get("title",    "") or "",
        grant.get("synopsis", "") or "",
    ]).lower()

    for kw in NONPROFIT_KEYWORDS:
        if kw in text:
            return True

    return False


# ---------------------------------------------------------------------------
# Step 3: Score each grant 1–5 stars
# ---------------------------------------------------------------------------

def score_grant(grant: dict) -> float:
    """
    Score a grant 1–5 based on keyword matches across relevance categories.
    Higher score = more relevant to typical GrantSignal audience.

    Scoring logic:
    - Every matching category contributes +1 (up to the number of categories = 8)
    - Raw score is normalised to 1–5 scale
    - Bonus +0.5 if the word "nonprofit" or "community" appears in title/synopsis
    """
    text = " ".join([
        grant.get("title",    "") or "",
        grant.get("synopsis", "") or "",
        grant.get("agencyName", "") or "",
    ]).lower()

    matched_categories = 0
    for _category, keywords in SCORING_CATEGORIES.items():
        if any(kw in text for kw in keywords):
            matched_categories += 1

    # Map matched_categories (0–8) → raw 0–4, then +1 for 1-based scale
    raw = matched_categories / len(SCORING_CATEGORIES)  # 0.0–1.0
    score = 1 + raw * 4  # 1.0–5.0

    # Bonus for explicit nonprofit / community language in the title
    title_lower = (grant.get("title", "") or "").lower()
    if any(kw in title_lower for kw in ["nonprofit", "non-profit", "community", "501(c)"]):
        score = min(5.0, score + 0.5)

    return round(score, 1)


# ---------------------------------------------------------------------------
# Step 4: Build digest lists
# ---------------------------------------------------------------------------

def build_digests(grants: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Returns (free_digest, paid_digest).
    free_digest  = top 3 grants by score
    paid_digest  = top MAX_PAID_GRANTS grants by score
    """
    # Filter then score
    eligible = [g for g in grants if is_eligible(g)]
    print(f"[digest] {len(eligible)} grants passed eligibility filter.")

    for grant in eligible:
        grant["_score"] = score_grant(grant)

    # Sort descending by score
    eligible.sort(key=lambda g: g["_score"], reverse=True)

    # Apply minimum score threshold
    eligible = [g for g in eligible if g["_score"] >= MIN_SCORE]
    print(f"[digest] {len(eligible)} grants meet minimum score threshold ({MIN_SCORE}).")

    paid_digest = eligible[:MAX_PAID_GRANTS]
    free_digest = eligible[:3]

    return free_digest, paid_digest


# ---------------------------------------------------------------------------
# Helpers: formatting utilities
# ---------------------------------------------------------------------------

def stars(score: float) -> str:
    """Convert numeric score to emoji star string."""
    full  = int(score)
    half  = 1 if (score - full) >= 0.5 else 0
    empty = 5 - full - half
    return "⭐" * full + ("✨" if half else "") + "☆" * empty


def urgency_flag(close_date_str: str) -> str:
    """Return ⚡ if grant closes within URGENCY_DAYS days, else ''."""
    if not close_date_str:
        return ""
    try:
        # Grants.gov closeDate format: "MM/DD/YYYY"
        close_dt = datetime.datetime.strptime(close_date_str, "%m/%d/%Y").date()
        days_left = (close_dt - datetime.date.today()).days
        if 0 <= days_left <= URGENCY_DAYS:
            return f"⚡ Closes in {days_left}d"
    except ValueError:
        pass
    return ""


def grants_gov_url(opp_number: str) -> str:
    return f"https://www.grants.gov/search-results-detail/{opp_number}"


# ---------------------------------------------------------------------------
# Step 5a: Build FREE HTML email
# ---------------------------------------------------------------------------

FREE_UPGRADE_CTA = """
<div style="background:#e8f5e9;border-left:4px solid {teal};padding:16px 20px;
            margin:24px 0;border-radius:4px;">
  <p style="margin:0;color:#1b5e20;font-size:15px;">
    <strong>👋 Want the full list?</strong> This week we matched
    <strong>{total}</strong> federal grants for nonprofits and schools.
    Free subscribers see the top 3. Upgrade to see all {total} with full
    details, agency contacts, and priority scores.
  </p>
  <p style="margin:12px 0 0;">
    <a href="https://grantsignal.news/upgrade"
       style="background:{blue};color:#fff;padding:10px 20px;
              border-radius:4px;text-decoration:none;font-weight:bold;
              display:inline-block;">
      Upgrade to Full Access →
    </a>
  </p>
</div>
""".format(teal=TEAL, blue=BLUE, total="{total}")  # {total} filled later


def build_free_html(grants: list[dict], total_matched: int) -> str:
    week_str = datetime.date.today().strftime("%B %d, %Y")
    grant_cards = ""

    for grant in grants:
        score     = grant.get("_score", 0)
        title     = grant.get("title",      "Untitled") or "Untitled"
        agency    = grant.get("agencyName", "Unknown Agency") or "Unknown Agency"
        close_dt  = grant.get("closeDate",  "") or ""
        opp_num   = grant.get("oppNumber",  "") or ""
        synopsis  = (grant.get("synopsis",  "") or "")[:300]
        urgency   = urgency_flag(close_dt)
        url       = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"

        urgency_html = (
            f'<span style="color:#e65100;font-weight:bold;margin-left:8px;">'
            f'{urgency}</span>'
        ) if urgency else ""

        grant_cards += f"""
        <div style="border:1px solid #e0e0e0;border-radius:6px;padding:16px 20px;
                    margin:0 0 16px;background:#fff;">
          <div style="font-size:13px;color:{TEAL};font-weight:600;
                      text-transform:uppercase;letter-spacing:.5px;
                      margin-bottom:4px;">{agency}</div>
          <h3 style="margin:0 0 8px;font-size:17px;color:{NAVY};">
            <a href="{url}" style="color:{NAVY};text-decoration:none;">{title}</a>
          </h3>
          <div style="margin-bottom:8px;">
            <span style="font-size:18px;">{stars(score)}</span>
            <span style="color:#555;font-size:13px;margin-left:6px;">
              Fit Score: {score}/5
            </span>
            {urgency_html}
          </div>
          <p style="color:#444;font-size:14px;line-height:1.6;margin:0 0 10px;">
            {synopsis}{"…" if len(synopsis) == 300 else ""}
          </p>
          <div style="font-size:13px;color:#666;">
            Closes: <strong>{close_dt or "See listing"}</strong>
            &nbsp;·&nbsp;
            <a href="{url}" style="color:{BLUE};">View on Grants.gov →</a>
          </div>
        </div>"""

    upgrade_block = FREE_UPGRADE_CTA.format(total=total_matched)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GrantSignal | Top 3 Federal Grants This Week</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Helvetica Neue',
             Arial,sans-serif;color:#222;">
  <div style="max-width:620px;margin:0 auto;background:#fff;
              border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);">

    <!-- Header -->
    <div style="background:{NAVY};padding:28px 32px;">
      <div style="color:{TEAL};font-size:13px;font-weight:600;
                  text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
        📡 GrantSignal
      </div>
      <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">
        Top 3 Federal Grants This Week
      </h1>
      <div style="color:#90caf9;font-size:13px;margin-top:8px;">
        Week of {week_str}
      </div>
    </div>

    <!-- Body -->
    <div style="padding:28px 32px;">
      <p style="color:#555;font-size:15px;line-height:1.6;margin:0 0 24px;">
        Here are this week's top-scoring federal grant opportunities for nonprofits
        and schools — curated and scored by GrantSignal.
      </p>

      {grant_cards}

      {upgrade_block}
    </div>

    <!-- Footer -->
    <div style="background:#f5f5f5;padding:20px 32px;font-size:12px;
                color:#888;border-top:1px solid #e0e0e0;">
      <p style="margin:0 0 6px;">
        You're receiving this because you subscribed to GrantSignal.
      </p>
      <p style="margin:0;">
        <a href="{{{{unsubscribe_url}}}}" style="color:{TEAL};">Unsubscribe</a>
        &nbsp;·&nbsp;
        <a href="https://grantsignal.news" style="color:{TEAL};">grantsignal.news</a>
      </p>
    </div>

  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 5b: Build PAID HTML email
# ---------------------------------------------------------------------------

def build_paid_html(grants: list[dict]) -> str:
    week_str = datetime.date.today().strftime("%B %d, %Y")
    count    = len(grants)
    grant_cards = ""

    for i, grant in enumerate(grants, start=1):
        score    = grant.get("_score", 0)
        title    = grant.get("title",      "Untitled") or "Untitled"
        agency   = grant.get("agencyName", "Unknown Agency") or "Unknown Agency"
        close_dt = grant.get("closeDate",  "") or ""
        opp_num  = grant.get("oppNumber",  "") or ""
        synopsis = (grant.get("synopsis",  "") or "")[:400]
        urgency  = urgency_flag(close_dt)
        url      = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"

        border_color = TEAL if score >= 4.0 else ("#1565c0" if score >= 3.0 else "#bdbdbd")

        urgency_html = (
            f'<div style="display:inline-block;background:#fff3e0;color:#e65100;'
            f'font-size:12px;font-weight:bold;padding:3px 8px;border-radius:3px;'
            f'margin-left:8px;">{urgency}</div>'
        ) if urgency else ""

        grant_cards += f"""
        <div style="border-left:4px solid {border_color};padding:16px 20px;
                    margin:0 0 20px;background:#fafafa;border-radius:0 6px 6px 0;">
          <div style="display:flex;align-items:center;flex-wrap:wrap;
                      margin-bottom:6px;gap:6px;">
            <span style="font-size:11px;background:{NAVY};color:#fff;
                         padding:2px 8px;border-radius:3px;font-weight:600;">
              #{i}
            </span>
            <span style="font-size:13px;color:{TEAL};font-weight:600;">{agency}</span>
            {urgency_html}
          </div>
          <h3 style="margin:0 0 8px;font-size:16px;color:{NAVY};">
            <a href="{url}" style="color:{NAVY};text-decoration:none;">{title}</a>
          </h3>
          <div style="margin-bottom:10px;">
            <span style="font-size:17px;">{stars(score)}</span>
            <span style="color:#555;font-size:13px;margin-left:6px;">
              Fit Score: <strong>{score}</strong> / 5
            </span>
          </div>
          <p style="color:#444;font-size:14px;line-height:1.6;margin:0 0 10px;">
            {synopsis}{"…" if len(synopsis) == 400 else ""}
          </p>
          <div style="font-size:13px;color:#666;">
            Closes: <strong>{close_dt or "See listing"}</strong>
            &nbsp;·&nbsp;
            <a href="{url}" style="color:{BLUE};">View full listing →</a>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GrantSignal | {count} Federal Grant Matches This Week</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Helvetica Neue',
             Arial,sans-serif;color:#222;">
  <div style="max-width:640px;margin:0 auto;background:#fff;
              border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);">

    <!-- Header -->
    <div style="background:{NAVY};padding:28px 32px;">
      <div style="color:{TEAL};font-size:13px;font-weight:600;
                  text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
        📡 GrantSignal — Full Member Digest
      </div>
      <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">
        {count} Federal Grant Matches This Week
      </h1>
      <div style="color:#90caf9;font-size:13px;margin-top:8px;">
        Week of {week_str} &nbsp;·&nbsp; Sorted by Fit Score (highest first)
      </div>
    </div>

    <!-- Legend -->
    <div style="background:#e8eaf6;padding:12px 32px;font-size:13px;color:#444;
                border-bottom:1px solid #ddd;">
      ⭐⭐⭐⭐⭐ = Perfect fit &nbsp;·&nbsp;
      ⚡ = Closes within 14 days &nbsp;·&nbsp;
      Ranked by relevance to nonprofits &amp; schools
    </div>

    <!-- Body -->
    <div style="padding:28px 32px;">
      {grant_cards}
    </div>

    <!-- Footer -->
    <div style="background:#f5f5f5;padding:20px 32px;font-size:12px;
                color:#888;border-top:1px solid #e0e0e0;">
      <p style="margin:0 0 6px;">
        You're a GrantSignal full member — you receive every matched grant.
      </p>
      <p style="margin:0;">
        <a href="{{{{unsubscribe_url}}}}" style="color:{TEAL};">Unsubscribe</a>
        &nbsp;·&nbsp;
        <a href="https://grantsignal.news" style="color:{TEAL};">grantsignal.news</a>
        &nbsp;·&nbsp;
        <a href="https://grantsignal.news/archive" style="color:{TEAL};">Archive</a>
      </p>
    </div>

  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 6: Fetch Beehiiv subscribers by tier
# ---------------------------------------------------------------------------

def fetch_subscribers(tier: str) -> list[str]:
    """
    Fetch all active subscriber emails for a given tier ('free' or 'premium').
    Handles pagination via cursor.
    Returns a list of email addresses.
    """
    print(f"[beehiiv] Fetching {tier} subscribers…")
    emails = []
    page = 1
    per_page = 100

    while True:
        url = (
            f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}"
            f"/subscriptions"
        )
        params = {
            "status":   "active",
            "tier":     tier,
            "page":     page,
            "limit":    per_page,
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
            print(f"[beehiiv] ERROR fetching {tier} subscribers (page {page}): {exc}")
            break

        subs = data.get("data", [])
        if not subs:
            break

        for sub in subs:
            email = sub.get("email")
            if email:
                emails.append(email)

        # Check if there are more pages
        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    print(f"[beehiiv] Found {len(emails)} {tier} subscribers.")
    return emails


# ---------------------------------------------------------------------------
# Step 7: Send emails via Resend
# ---------------------------------------------------------------------------

def send_email_batch(
    to_emails: list[str],
    subject: str,
    html_body: str,
    label: str = "batch",
) -> None:
    """
    Send individual emails to each recipient via the Resend API.
    Resend's free tier requires one recipient per API call.
    Logs progress every 10 sends.
    """
    if not to_emails:
        print(f"[resend] No recipients for {label} — skipping.")
        return

    print(f"[resend] Sending {label} digest to {len(to_emails)} subscribers…")
    success = 0
    errors  = 0

    for i, email in enumerate(to_emails, start=1):
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
            print(f"[resend] Progress: {i}/{len(to_emails)} sent…")

    print(f"[resend] {label} complete — {success} sent, {errors} errors.")


# ---------------------------------------------------------------------------
# Step 8: Publish Beehiiv archive post
# ---------------------------------------------------------------------------

def publish_beehiiv_post(html_body: str) -> None:
    """
    Create a published web post on Beehiiv with the full digest as archive.
    """
    week_str  = datetime.date.today().strftime("%B %d, %Y")
    post_title = f"GrantSignal Weekly Digest — Week of {week_str}"

    url = f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/posts"
    payload = {
        "title":    post_title,
        "subtitle": "Federal grant opportunities for nonprofits and schools.",
        "body":     html_body,   # Beehiiv accepts HTML in the body field
        "status":   "confirmed", # Published immediately
        "channel":  "web",       # Web post (archive), not email blast
        "content_tags": ["grants", "nonprofits", "education"],
    }
    headers = {
        "Authorization": f"Bearer {BEEHIIV_API_KEY}",
        "Content-Type":  "application/json",
    }

    print(f"[beehiiv] Publishing archive post: '{post_title}'…")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        post_data = resp.json().get("data", {})
        post_id  = post_data.get("id", "?")
        post_url = post_data.get("web_url", "")
        print(f"[beehiiv] Archive post created — id={post_id}  url={post_url}")
    except requests.RequestException as exc:
        print(f"[beehiiv] ERROR creating archive post: {exc}")
        if hasattr(exc, "response") and exc.response is not None:
            print(f"[beehiiv] Response body: {exc.response.text[:500]}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  GrantSignal Weekly Digest Pipeline")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Validate required env vars
    missing = [v for v in ("BEEHIIV_API_KEY", "BEEHIIV_PUB_ID",
                            "RESEND_API_KEY", "FROM_EMAIL")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # ── 1. Fetch grants ──────────────────────────────────────────────────────
    grants = fetch_grants(max_records=500)
    if not grants:
        print("No grants fetched. Exiting.")
        sys.exit(1)

    # ── 2–4. Filter, score, build digests ───────────────────────────────────
    free_digest, paid_digest = build_digests(grants)
    total_matched = len(paid_digest)

    if not paid_digest:
        print("No eligible, scored grants found. Exiting.")
        sys.exit(0)

    print(f"[digest] Free digest: {len(free_digest)} grants | "
          f"Paid digest: {len(paid_digest)} grants")

    # ── 5. Build email HTML ──────────────────────────────────────────────────
    free_html = build_free_html(free_digest, total_matched=total_matched)
    paid_html = build_paid_html(paid_digest)

    # ── 6. Fetch subscribers ─────────────────────────────────────────────────
    free_subscribers = fetch_subscribers("free")
    paid_subscribers = fetch_subscribers("premium")

    # ── 7. Send emails ───────────────────────────────────────────────────────
    week_str = datetime.date.today().strftime("%B %d, %Y")

    send_email_batch(
        to_emails=free_subscribers,
        subject="📡 GrantSignal | Top 3 Federal Grants This Week",
        html_body=free_html,
        label="FREE",
    )

    send_email_batch(
        to_emails=paid_subscribers,
        subject=f"📡 GrantSignal | {total_matched} Federal Grant Matches This Week",
        html_body=paid_html,
        label="PAID",
    )

    # ── 8. Publish Beehiiv archive post ──────────────────────────────────────
    publish_beehiiv_post(paid_html)

    print("=" * 60)
    print("  Pipeline complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
