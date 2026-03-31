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
import random
import datetime
import requests
import hashlib
import base64

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUB_ID  = os.environ.get("BEEHIIV_PUB_ID",  "")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",  "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "digest@grantsignal.news")
# GITHUB_TOKEN is used to load subscriber preferences from the repo.
# Set via GitHub Actions secret or GITHUB_TOKEN env var (auto-set in Actions).
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")

GRANTS_GOV_ENDPOINT = "https://api.grants.gov/v1/api/search2"

# Maximum grants to include in the paid digest
MAX_PAID_GRANTS = 50

# Minimum fit score (out of 5) to include a grant at all
MIN_SCORE = 2

# Days before close date that triggers the urgency flag
URGENCY_DAYS = 14

# Brand colours
NAVY  = "#0f3460"
BLUE  = "#1565c0"
TEAL  = "#00897b"

# Archive directory (relative to repo root, where GitHub Actions runs)
_ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "archive")

# ---------------------------------------------------------------------------
# Exclusion filters — grants almost never relevant to small nonprofits
# ---------------------------------------------------------------------------

# Title patterns that indicate research/academic grants to exclude
EXCLUDE_TITLE_PATTERNS = [
    "R25", "R01", "R03", "R21", "R34", "K08", "K23", "F31", "F32", "T32",
    "Clinical Trial", "Dissertation", "Fellowship", "Postdoctoral",
    "Career Development Award",
]

# Agency name patterns that indicate research agencies
RESEARCH_AGENCY_PATTERNS = [
    "NIH",
    "National Cancer Institute",
    "National Heart",
    "National Institute of",
    "AHRQ",
    "National Science Foundation",
    "NSF",
    "National Endowment for the Humanities",
]

# Keywords that make a research-agency grant worth KEEPING (community focus)
COMMUNITY_RELEVANCE_KEYWORDS = [
    "community", "nonprofit", "school", "education",
    "prevention", "outreach", "public health",
]

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
# Grant writing tips — rotated randomly in paid digest
# ---------------------------------------------------------------------------

GRANT_WRITING_TIPS = [
    "Always read the full NOFO before starting your application. Eligibility requirements are often buried in Section C.",
    "Federal grants require a DUNS/UEI number and SAM.gov registration. Make sure yours is active — it can take 2-3 weeks to process.",
    "Deadline means RECEIVED by deadline, not postmarked. Submit at least 48 hours early to avoid technical issues.",
    "Match requirements are common in federal grants — know your organization's in-kind and cash match capacity before applying.",
    "Start with smaller grants ($25K-$100K) to build a track record. Federal agencies favor organizations with prior federal award experience.",
]


# ---------------------------------------------------------------------------
# Step 1: Fetch grants from Grants.gov
# ---------------------------------------------------------------------------

def fetch_grants(max_records: int = 500) -> list[dict]:
    """
    POST to the Grants.gov search2 API and return a flat list of opportunity dicts.
    Each hit contains fields at the TOP LEVEL of the oppHits entry:
      id, oppNumber, title, agencyName, openDate, closeDate,
      fundingCategory, eligibilities, synopsis, awardCeiling
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
            # Request all fields we need (all returned at top level of each oppHit)
            "fields": (
                "id,oppNumber,title,agencyName,openDate,closeDate,"
                "fundingCategory,eligibilities,synopsis,awardCeiling"
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

def _is_research_agency(agency: str) -> bool:
    """Return True if the agency name matches known research/medical agency patterns."""
    agency_upper = agency.upper()
    for pattern in RESEARCH_AGENCY_PATTERNS:
        if pattern.upper() in agency_upper:
            return True
    return False


def _has_community_relevance(title: str, synopsis: str) -> bool:
    """Return True if title or synopsis contains community-relevant keywords."""
    text = (title + " " + synopsis).lower()
    return any(kw.lower() in text for kw in COMMUNITY_RELEVANCE_KEYWORDS)


def is_eligible(grant: dict) -> bool:
    """
    Return True if the grant appears eligible for nonprofits or schools.

    Field mapping (all at top level of Grants.gov oppHits entries):
      - title:      grant.get("title")
      - agencyName: grant.get("agency")
      - synopsis:   grant.get("synopsis")
      - closeDate:  grant.get("closeDate")
      - openDate:   grant.get("openDate")
      - oppNumber:  grant.get("number")
      - id:         grant.get("id")

    We check structured eligibilities AND scan title/synopsis free-text.
    We also apply exclusion rules for research/academic grants.
    """
    # --- Field access (all top-level per Grants.gov API) ---
    title    = grant.get("title",      "") or ""
    synopsis = grant.get("synopsis",   "") or ""
    agency   = grant.get("agency", "") or ""

    # --- EXCLUSION: title pattern filter ---
    title_lower = title.lower()
    for pattern in EXCLUDE_TITLE_PATTERNS:
        # Match whole-word for short codes (R25, R01, etc.) to avoid false positives
        if len(pattern) <= 3:
            # Check as standalone word or at end of title segment like "(R25)"
            import re
            if re.search(r'\b' + re.escape(pattern) + r'\b', title, re.IGNORECASE):
                return False
        else:
            if pattern.lower() in title_lower:
                return False

    # --- EXCLUSION: research agency filter (unless community-focused) ---
    if _is_research_agency(agency):
        if not _has_community_relevance(title, synopsis):
            return False

    # --- INCLUSION: Check structured eligibilities list ---
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

    # --- INCLUSION: Fall back — default to include ---
    # The exclusion filters above already removed clearly irrelevant grants
    # (NIH research, clinical trials, etc.). If a grant survived to here,
    # include it and let the scorer rank its relevance.
    # Bonus: boost score if nonprofit keywords are present (handled in scorer).
    return True


# ---------------------------------------------------------------------------
# Step 3: Score each grant 1–5 stars
# ---------------------------------------------------------------------------

def score_grant(grant: dict) -> float:
    """
    Score a grant on a 0–5 scale based on keyword matches and relevance signals.

    Scoring logic:
    - Base score: 2.0 (all eligible grants start here)
    - +up to 3.0 for keyword category matches (normalised across 8 categories)
    - +1.0 if "nonprofit" or "community organization" appears anywhere
    - +0.5 if award ceiling is under $500K (more accessible)
    - +1.0 if closing within 60 days (time-sensitive)
    - Cap at 5.0
    - Minimum to include: MIN_SCORE (2.0)
    """
    title    = grant.get("title",        "") or ""
    synopsis = grant.get("synopsis",     "") or ""
    agency   = grant.get("agency",   "") or ""
    close_dt = grant.get("closeDate",    "") or ""
    award    = grant.get("awardCeiling", None)

    text = (title + " " + synopsis + " " + agency).lower()

    # Base score
    score = 2.0

    # Keyword category matching (up to +3)
    matched_categories = 0
    for _category, keywords in SCORING_CATEGORIES.items():
        if any(kw in text for kw in keywords):
            matched_categories += 1

    raw = matched_categories / len(SCORING_CATEGORIES)  # 0.0–1.0
    score += raw * 3.0  # up to +3

    # Bonus: explicit nonprofit / community organization language
    if "nonprofit" in text or "community organization" in text:
        score += 1.0

    # Bonus: award amount under $500K (more accessible for small nonprofits)
    try:
        if award is not None and float(str(award).replace(",", "")) < 500_000:
            score += 0.5
    except (ValueError, TypeError):
        pass

    # Bonus: closing within 60 days (time-sensitive = more relevant to show)
    if close_dt:
        try:
            close_date = datetime.datetime.strptime(close_dt, "%m/%d/%Y").date()
            days_left = (close_date - datetime.date.today()).days
            if 0 <= days_left <= 60:
                score += 1.0
        except ValueError:
            pass

    return round(min(5.0, score), 1)


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

def stars_html(score: float) -> tuple[str, str, str]:
    """
    Returns (stars_html_str, match_label, label_color) based on score.
    Uses ★ (filled, gold #f4a800) and ☆ (empty, light gray).
    """
    filled = '<span style="color:#f4a800;">&#9733;</span>'  # ★
    empty  = '<span style="color:#cccccc;">&#9734;</span>'  # ☆

    if score >= 4.5:
        stars_str = filled * 5
        label     = "Excellent Match"
        color     = TEAL
    elif score >= 3.5:
        stars_str = filled * 4 + empty
        label     = "Strong Match"
        color     = BLUE
    elif score >= 2.5:
        stars_str = filled * 3 + empty * 2
        label     = "Good Match"
        color     = "#5a6a7a"
    else:
        stars_str = filled * 2 + empty * 3
        label     = "Possible Match"
        color     = "#9e9e9e"

    return stars_str, label, color


def is_urgent(close_date_str: str) -> bool:
    """Return True if grant closes within URGENCY_DAYS days."""
    if not close_date_str:
        return False
    try:
        close_dt = datetime.datetime.strptime(close_date_str, "%m/%d/%Y").date()
        days_left = (close_dt - datetime.date.today()).days
        return 0 <= days_left <= URGENCY_DAYS
    except ValueError:
        return False


def urgency_flag(close_date_str: str) -> str:
    """Return urgency string if grant closes within URGENCY_DAYS days, else ''."""
    if not close_date_str:
        return ""
    try:
        close_dt = datetime.datetime.strptime(close_date_str, "%m/%d/%Y").date()
        days_left = (close_dt - datetime.date.today()).days
        if 0 <= days_left <= URGENCY_DAYS:
            return f"⚡ Closes in {days_left}d"
    except ValueError:
        pass
    return ""


def grants_gov_url(opp_number: str, grant: dict = None) -> str:
    if grant and grant.get("source") == "federal_register":
        return grant.get("url", "https://www.federalregister.gov")
    return f"https://www.grants.gov/search-results-detail/{opp_number}"


# ---------------------------------------------------------------------------
# Feature helpers — match explanation, badges, grant of the week
# ---------------------------------------------------------------------------

def get_match_explanation(grant: dict) -> str:
    """
    Return a one-line match explanation based on the top scoring category
    for the grant's title/synopsis.
    """
    title    = (grant.get("title",    "") or "").lower()
    synopsis = (grant.get("synopsis", "") or "").lower()
    agency   = (grant.get("agency",   "") or "").lower()
    text     = title + " " + synopsis + " " + agency

    category_messages = {
        "education":             "Strong match: education &amp; youth development focus",
        "health":                "Strong match: health &amp; human services mission alignment",
        "community_development": "Strong match: community development &amp; social services",
        "environment":           "Match: environmental conservation focus",
        "arts":                  "Match: arts, culture &amp; humanities programming",
        "housing":               "Match: housing &amp; homelessness services",
        "youth":                 "Match: youth-serving organization alignment",
        "social_services":       "Match: social services mission",
    }

    best_cat   = None
    best_count = 0
    for category, keywords in SCORING_CATEGORIES.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > best_count:
            best_count = count
            best_cat   = category

    if best_cat and best_count > 0 and best_cat in category_messages:
        return category_messages[best_cat]
    return "Match: open to nonprofit organizations"


def get_grant_badges(grant: dict) -> str:
    """
    Return HTML badge(s) for Features 3 (Quick Win) and 4 (New vs Reopened).
    Both badges are placed next to the grant title in the PAID digest.
    """
    badges = ""

    # ── Feature 4: New vs Reopened ───────────────────────────────────────────
    open_date_str = grant.get("openDate", "") or ""
    if open_date_str:
        try:
            open_date = datetime.datetime.strptime(open_date_str, "%m/%d/%Y").date()
            days_since_open = (datetime.date.today() - open_date).days
            if days_since_open <= 14:
                badges += (
                    '<span style="background:#dbeafe;color:#1e40af;font-size:11px;'
                    'font-weight:bold;padding:2px 8px;border-radius:12px;'
                    'margin-left:8px;display:inline-block;">'
                    '&#x1F195; New</span>'
                )
            else:
                badges += (
                    '<span style="background:#f3f4f6;color:#6b7280;font-size:11px;'
                    'font-weight:bold;padding:2px 8px;border-radius:12px;'
                    'margin-left:8px;display:inline-block;">'
                    '&#x1F504; Reopened</span>'
                )
        except ValueError:
            pass

    # ── Feature 3: Quick Win ─────────────────────────────────────────────────
    score       = grant.get("_score", 0) or 0
    award       = grant.get("awardCeiling", None)
    title       = (grant.get("title", "") or "").lower()
    close_str   = grant.get("closeDate", "") or ""

    exclude_terms = ["research", "clinical", "phase", "innovation"]
    has_complex_term = any(t in title for t in exclude_terms)

    award_ok = True
    if award is not None:
        try:
            award_ok = float(str(award).replace(",", "")) <= 100_000
        except (ValueError, TypeError):
            award_ok = True  # treat unparseable as unspecified → include

    close_ok = False
    if close_str:
        try:
            close_date = datetime.datetime.strptime(close_str, "%m/%d/%Y").date()
            close_ok = (close_date - datetime.date.today()).days > 14
        except ValueError:
            close_ok = False
    else:
        close_ok = True  # no close date = rolling/open-ended → include

    if score >= 3.5 and award_ok and close_ok and not has_complex_term:
        badges += (
            '<span style="background:#dcfce7;color:#166534;font-size:11px;'
            'font-weight:bold;padding:2px 8px;border-radius:12px;'
            'margin-left:8px;display:inline-block;">'
            '&#x26A1; Quick Win</span>'
        )

    return badges


def build_grant_of_week(top_grant: dict) -> str:
    """
    Build the HTML block for the "Grant of the Week" section (Feature 1).
    The top_grant is the highest-scored grant from the full paid digest list.
    """
    title        = top_grant.get("title",      "Untitled") or "Untitled"
    agency       = top_grant.get("agency", "Unknown Agency") or "Unknown Agency"
    score        = top_grant.get("_score", 0) or 0
    _raw_close   = top_grant.get("closeDate",  "") or ""
    opp_num      = top_grant.get("number",     "") or ""
    url          = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"

    # Format close date
    import re as _re2
    if _re2.match(r"\d{2}/\d{2}/\d{4}", _raw_close):
        import datetime as _dt2
        close_display = _dt2.datetime.strptime(_raw_close, "%m/%d/%Y").strftime("%b %-d, %Y")
        rolling = False
    elif _raw_close:
        close_display = _raw_close
        rolling = ("grant" in _raw_close.lower() or "see" in _raw_close.lower())
    else:
        close_display = "See Grants.gov for deadline"
        rolling = True

    # Why this stands out
    text = (title + " " + (top_grant.get("synopsis", "") or "") + " " + agency).lower()
    # Determine top scoring category for the blurb
    best_cat_label = "this opportunity"
    best_count = 0
    MIN_KEYWORD_HITS = 2  # require at least 2 keyword matches to claim a category
    cat_labels = {
        "education":             "education &amp; youth development",
        "health":                "health &amp; human services",
        "community_development": "community development",
        "environment":           "environmental conservation",
        "arts":                  "arts &amp; culture",
        "housing":               "housing &amp; homelessness",
        "youth":                 "youth-serving programs",
        "social_services":       "social services",
    }
    for category, keywords in SCORING_CATEGORIES.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > best_count and count >= MIN_KEYWORD_HITS:
            best_count = count
            best_cat_label = cat_labels.get(category, "this opportunity")

    if score >= 4.5:
        why_blurb = f"Top-tier match for {best_cat_label} nonprofits. Highly recommended."
    elif score >= 3.5:
        why_blurb = f"Strong fit for nonprofits focused on {best_cat_label}. Worth a close look."
    else:
        why_blurb = f"Relevant funding for {best_cat_label} — worth reviewing if it fits your mission."

    # Eligibility checklist
    agency_lower = agency.lower()
    community_agencies = [
        "community", "cdbg", "cdfi", "neighborhood", "rural",
        "urban", "americorps", "cncs", "ojjdp", "acf", "samhsa",
    ]
    is_community = any(kw in agency_lower for kw in community_agencies)

    if rolling:
        deadline_bullet = "&#x2713; Applications accepted on a rolling basis"
    else:
        deadline_bullet = "&#x2713; Single deadline &mdash; apply early"

    if is_community:
        experience_bullet = "&#x2713; No prior federal award experience required"
    else:
        experience_bullet = "&#x2713; Prior federal experience helpful but not required"

    eligibility_html = f"""
          <ul style="list-style:none;padding:0;margin:10px 0;font-size:13px;color:#334155;">
            <li style="margin-bottom:4px;">&#x2713; Open to 501(c)(3) nonprofit organizations</li>
            <li style="margin-bottom:4px;">{experience_bullet}</li>
            <li style="margin-bottom:4px;">{deadline_bullet}</li>
          </ul>"""

    # Common mistake warning
    hhs_keywords    = ["hhs", "health and human services", "samhsa", "hrsa", "acf", "cms"]
    edu_keywords    = ["department of education", "ed.gov", "doe", "education dept"]
    is_hhs = any(kw in agency_lower for kw in hhs_keywords)
    is_edu = any(kw in agency_lower for kw in edu_keywords)

    if is_hhs:
        common_mistake = "Missing required UEI/SAM.gov registration before applying"
    elif is_edu:
        common_mistake = "Not demonstrating measurable student outcome metrics in the narrative"
    else:
        common_mistake = "Submitting without a fully completed SF-424 form"

    return f"""
    <div style="border-left:4px solid #00897b;background:#f0f7ff;
                padding:16px 20px;border-radius:8px;margin-bottom:24px;">
      <div style="font-size:11px;font-weight:bold;color:#00897b;
                  letter-spacing:0.08em;margin-bottom:6px;">
        &#x1F3C6; GRANT OF THE WEEK
      </div>
      <div style="font-size:16px;font-weight:bold;color:#0f3460;
                  line-height:1.4;margin-bottom:4px;">{title}</div>
      <div style="font-size:12px;color:#1565c0;margin-bottom:4px;">&#x1F3DB; {agency}</div>
      <div style="font-size:12px;color:#444;margin-bottom:8px;">&#x1F4C5; Closes {close_display}</div>
      <div style="font-size:13px;color:#334155;margin-bottom:12px;">{why_blurb}</div>
      <a href="{url}"
         style="display:inline-block;background:#00897b;color:#ffffff;
                font-size:13px;font-weight:bold;padding:8px 18px;
                border-radius:6px;text-decoration:none;">
        View Full Opportunity &rarr;
      </a>
    </div>"""


# ---------------------------------------------------------------------------
# Step 5a: Build FREE HTML email
# ---------------------------------------------------------------------------

def build_free_html(
    grants: list[dict],
    total_matched: int,
    urgency_count: int = 0,
    all_paid_grants: list[dict] | None = None,
) -> str:
    week_str = datetime.date.today().strftime("%B %d, %Y")

    # ── Feature 1: Grant of the Week ─────────────────────────────────────────
    # Pick the highest-scored grant from the full paid list (may differ from top-3)
    gotw_html = ""
    source_list = all_paid_grants if all_paid_grants else grants
    if source_list:
        def gotw_score(g):
            import re as _re
            base = float(g.get("_score", 0) or g.get("score", 0) or 0)
            close = g.get("closeDate", "") or ""
            has_date = bool(_re.match(r"\d{2}/\d{2}/\d{4}", close))
            title = (g.get("title", "") or "").lower()
            niche_keywords = ["repatriation", "tribal", "nagpra", "sbir", "sttr",
                              "dissertation", "fellowship", "research training", "postdoctoral",
                              "information collection", "comment request", "paperwork reduction",
                              "subaward", "proposed subaward", "notice of subaward",
                              "cooperative agreement modification", "amendment"]
            niche_penalty = -2.0 if any(kw in title for kw in niche_keywords) else 0.0
            return base + (1.5 if has_date else -1.0) + niche_penalty
        # Also hard-exclude known non-applicable grant types from GOTW
        GOTW_EXCLUDE = ["nagpra", "repatriation", "subaward", "sbir", "sttr",
                        "information collection", "fellowship", "dissertation"]
        filtered_source = [g for g in source_list
                          if not any(kw in (g.get("title","") or "").lower()
                                    for kw in GOTW_EXCLUDE)]
        if not filtered_source:
            filtered_source = source_list  # fallback if all excluded
        top_grant = max(filtered_source, key=gotw_score)
        gotw_html = build_grant_of_week(top_grant)

    grant_cards = ""

    for grant in grants:
        score    = grant.get("_score", 0)
        title    = grant.get("title",      "Untitled") or "Untitled"
        agency   = grant.get("agency", "Unknown Agency") or "Unknown Agency"
        _raw_close = grant.get("closeDate", "") or ""
        import re as _re
        if _re.match(r"\d{2}/\d{2}/\d{4}", _raw_close):
            import datetime as _dt
            _cd = _dt.datetime.strptime(_raw_close, "%m/%d/%Y")
            close_dt = _cd.strftime("%b %-d, %Y")
        else:
            close_dt = "See Grants.gov for deadline" if not _raw_close else _raw_close
        opp_num  = grant.get("number",  "") or ""
        synopsis_raw = grant.get("synopsis", "") or ""
        funding_cat = grant.get("fundingCategory", "") or grant.get("cfdaList", "") or ""
        # If no synopsis, show funding category instead of repeating title
        if not synopsis_raw or synopsis_raw == (grant.get("title","") or ""):
            synopsis = ""  # Don't show raw CFDA codes
        else:
            synopsis = synopsis_raw[:120]
        urgent   = is_urgent(_raw_close)
        is_fr    = grant.get("source") == "federal_register"
        url      = grants_gov_url(opp_num, grant=grant) if (opp_num or is_fr) else "https://www.grants.gov"

        border_color = "#e53935" if urgent else "#00897b"
        stars_str, match_label, match_color = stars_html(score)

        if urgent:
            close_html = (
                f'<span style="color:#e53935;font-weight:bold;">'
                f'&#9889; Closes {close_dt} &mdash; URGENT</span>'
            )
        else:
            close_html = f'&#128197; Closes {close_dt or "See Grants.gov for deadline"}'

        synopsis_display = (synopsis + ("..." if len(synopsis_raw) > 120 else "")) if synopsis else ""

        match_explanation = get_match_explanation(grant)

        fr_badge_html = (
            '<span style="display:inline-block;background:#e5e7eb;color:#4b5563;'
            'font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;'
            'margin-top:4px;">&#128203; Federal Register</span>'
        ) if is_fr else ""

        fr_early_alert_html = (
            '<div style="font-size:11px;color:#6b7280;font-style:italic;margin-bottom:6px;">'
            '&#9889; Early alert &mdash; may not yet appear on Grants.gov</div>'
        ) if is_fr else ""

        view_btn_text = "View on Federal Register &rarr;" if is_fr else "View on Grants.gov &rarr;"

        grant_cards += f"""
        <div style="background:#ffffff;border-radius:10px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);
                    margin:0 0 16px 0;padding:20px 24px;
                    border-left:4px solid {border_color};">
          <div style="font-size:15px;font-weight:bold;color:#0f3460;
                      margin-bottom:6px;line-height:1.4;">{title}</div>
          <div style="font-size:12px;color:#1565c0;margin-bottom:4px;">
            &#127963; {agency}
          </div>
          {fr_badge_html}
          <div style="margin-bottom:4px;font-size:14px;{'margin-top:6px;' if is_fr else ''}">
            {stars_str}
            <span style="font-size:12px;color:{match_color};
                         margin-left:6px;font-weight:600;">{match_label}</span>
          </div>
          <div style="font-size:12px;color:#6b7280;font-style:italic;margin-bottom:8px;">
            {match_explanation}
          </div>
          <div style="font-size:13px;color:#444;margin-bottom:8px;">
            {close_html}
          </div>
                {fr_early_alert_html}
          <div>
            <a href="{url}"
               style="display:inline-block;background:#00897b;color:#ffffff;
                      font-size:13px;font-weight:bold;padding:8px 18px;
                      border-radius:6px;text-decoration:none;">
              {view_btn_text}
            </a>
          </div>
        </div>"""

    upgrade_cta = f"""
    <div style="background:#0f3460;border-radius:10px;padding:24px 28px;margin:8px 0 0 0;">
      <div style="color:#ffffff;font-size:15px;font-weight:600;margin-bottom:8px;">
        You're seeing 3 of {total_matched} grants matched this week
      </div>
      <div style="color:#90a8c0;font-size:13px;line-height:1.5;margin-bottom:18px;">
        Upgrade to Basic to see all {total_matched} opportunities &mdash;
        {f"including {urgency_count} closing soon" if urgency_count > 0 else "apply before deadlines close"}
      </div>
      <a href="https://dereks-newsletter-be7bae.beehiiv.com/upgrade"
         style="display:inline-block;background:#00897b;color:#ffffff;
                font-size:15px;font-weight:bold;padding:12px 28px;
                border-radius:8px;text-decoration:none;">
        Upgrade for $29/month &rarr;
      </a>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>&#128225; GrantSignal | Your Top 3 Federal Grant Matches This Week</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             color:#222222;">
  <!-- Preheader text (controls Gmail preview snippet) -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    Your top 3 federal grant matches this week — curated for nonprofits and schools.
  </div>

  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">

    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#0f3460;border-radius:10px 10px 0 0;">
      <tr>
        <td style="padding:22px 28px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;
                      line-height:1.2;">&#128225; GrantSignal</div>
          <div style="color:#4fc3f7;font-size:12px;margin-top:4px;">
            Federal Grant Intelligence
          </div>
        </td>
        <td align="right" style="padding:22px 28px;vertical-align:top;">
          <div style="color:#90a8c0;font-size:12px;white-space:nowrap;">
            {week_str}
          </div>
        </td>
      </tr>
    </table>

    <!-- Intro -->
    <div style="background:#ffffff;padding:18px 28px;
                border-bottom:1px solid #e8eef4;">
      <p style="margin:0;font-size:14px;color:#5a6a7a;line-height:1.6;">
        Good morning &mdash; here are your top 3 federal grant matches this week,
        selected for nonprofits and schools like yours.
      </p>
    </div>

    <!-- Grant of the Week + Grant Cards -->
    <div style="background:#f4f7fb;padding:20px 16px;">
      {grant_cards}
      {gotw_html}
      {upgrade_cta}
    </div>

    <div style="background:#e8eef4;padding:16px 28px;
                border-radius:0 0 10px 10px;text-align:center;">
      <div style="font-size:13px;color:#5a6a7a;font-weight:600;">
        &#128225; GrantSignal &middot; grantsignal.news
      </div>
      <div style="font-size:12px;color:#8a9ab0;margin-top:6px;line-height:1.5;">
        You're receiving this because you subscribed to GrantSignal's free tier.
      </div>
      <div style="font-size:12px;margin-top:10px;">
        <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;text-decoration:underline;">
          Unsubscribe
        </a>
      </div>
    </div>

  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Step 5b: Build PAID HTML email
# ---------------------------------------------------------------------------

def build_paid_html(grants: list[dict]) -> str:
    week_str  = datetime.date.today().strftime("%B %d, %Y")
    count     = len(grants)
    tip       = random.choice(GRANT_WRITING_TIPS)

    grant_cards = ""

    for grant in grants:
        score    = grant.get("_score", 0)
        title    = grant.get("title",      "Untitled") or "Untitled"
        agency   = grant.get("agency", "Unknown Agency") or "Unknown Agency"
        _raw_close = grant.get("closeDate", "") or ""
        import re as _re
        if _re.match(r"\d{2}/\d{2}/\d{4}", _raw_close):
            import datetime as _dt
            _cd = _dt.datetime.strptime(_raw_close, "%m/%d/%Y")
            close_dt = _cd.strftime("%b %-d, %Y")
        else:
            close_dt = "See Grants.gov for deadline" if not _raw_close else _raw_close
        opp_num  = grant.get("number",  "") or ""
        synopsis_raw = grant.get("synopsis", "") or ""
        funding_cat = grant.get("fundingCategory", "") or grant.get("cfdaList", "") or ""
        # If no synopsis, show funding category instead of repeating title
        if not synopsis_raw or synopsis_raw == (grant.get("title","") or ""):
            synopsis = ""  # Don't show raw CFDA codes
        else:
            synopsis = synopsis_raw[:120]
        urgent   = is_urgent(_raw_close)
        is_fr    = grant.get("source") == "federal_register"
        url      = grants_gov_url(opp_num, grant=grant) if (opp_num or is_fr) else "https://www.grants.gov"

        border_color = "#e53935" if urgent else "#00897b"
        stars_str, match_label, match_color = stars_html(score)

        if urgent:
            close_html = (
                f'<span style="color:#e53935;font-weight:bold;">'
                f'&#9889; Closes {close_dt} &mdash; URGENT</span>'
            )
        else:
            close_html = f'&#128197; Closes {close_dt or "See Grants.gov for deadline"}'

        synopsis_display = (synopsis + ("..." if len(synopsis_raw) > 120 else "")) if synopsis else ""

        # Features 3 & 4: Quick Win badge + New/Reopened label
        grant_badges = get_grant_badges(grant)

        fr_badge_html = (
            '<span style="display:inline-block;background:#e5e7eb;color:#4b5563;'
            'font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;'
            'margin-top:4px;">&#128203; Federal Register</span>'
        ) if is_fr else ""

        fr_early_alert_html = (
            '<div style="font-size:11px;color:#6b7280;font-style:italic;margin-bottom:6px;">'
            '&#9889; Early alert &mdash; may not yet appear on Grants.gov</div>'
        ) if is_fr else ""

        view_btn_text = "View on Federal Register &rarr;" if is_fr else "View on Grants.gov &rarr;"

        grant_cards += f"""
        <div style="background:#ffffff;border-radius:10px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);
                    margin:0 0 16px 0;padding:20px 24px;
                    border-left:4px solid {border_color};">
          <div style="font-size:15px;font-weight:bold;color:#0f3460;
                      margin-bottom:6px;line-height:1.4;">
            {title}{grant_badges}
          </div>
          <div style="font-size:12px;color:#1565c0;margin-bottom:4px;">
            &#127963; {agency}
          </div>
          {fr_badge_html}
          <div style="margin-bottom:8px;font-size:14px;{'margin-top:6px;' if is_fr else ''}">
            {stars_str}
            <span style="font-size:12px;color:{match_color};
                         margin-left:6px;font-weight:600;">{match_label}</span>
          </div>
          <div style="font-size:13px;color:#444;margin-bottom:8px;">
            {close_html}
          </div>
                {fr_early_alert_html}
          <div>
            <a href="{url}"
               style="display:inline-block;background:#00897b;color:#ffffff;
                      font-size:13px;font-weight:bold;padding:8px 18px;
                      border-radius:6px;text-decoration:none;">
              {view_btn_text}
            </a>
          </div>
        </div>"""

    tip_section = f"""
    <div style="background:#e3f2fd;border-radius:10px;padding:20px 24px;margin:8px 0 0 0;">
      <div style="font-size:15px;font-weight:bold;color:#0f3460;margin-bottom:10px;">
        &#128161; This Week's Tip
      </div>
      <div style="font-size:14px;color:#1565c0;line-height:1.6;">{tip}</div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>&#128225; GrantSignal | Full Weekly Digest &mdash; {count} Grants Matched</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             color:#222222;">
  <!-- Preheader text (controls Gmail preview snippet) -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    Your top 3 federal grant matches this week — curated for nonprofits and schools.
  </div>

  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">

    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#0f3460;border-radius:10px 10px 0 0;">
      <tr>
        <td style="padding:22px 28px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;
                      line-height:1.2;">&#128225; GrantSignal</div>
          <div style="color:#4fc3f7;font-size:12px;margin-top:4px;">
            Full Weekly Digest &mdash; {count} Grants Matched
          </div>
        </td>
        <td align="right" style="padding:22px 28px;vertical-align:top;">
          <div style="color:#90a8c0;font-size:12px;white-space:nowrap;">
            {week_str}
          </div>
        </td>
      </tr>
    </table>

    <!-- Intro -->
    <div style="background:#ffffff;padding:18px 28px;
                border-bottom:1px solid #e8eef4;">
      <p style="margin:0;font-size:14px;color:#5a6a7a;line-height:1.6;">
        Your full weekly digest of federal grant matches, sorted by relevance score.
        {count} opportunities matched this week for nonprofits and schools.
      </p>
    </div>

    <!-- Grant Cards -->
    <div style="background:#f4f7fb;padding:20px 16px;">
      {grant_cards}
      {tip_section}
    </div>

    <div style="background:#e8eef4;padding:16px 28px;
                border-radius:0 0 10px 10px;text-align:center;">
      <div style="font-size:13px;color:#5a6a7a;font-weight:600;">
        &#128225; GrantSignal &middot; grantsignal.news
      </div>
      <div style="font-size:12px;color:#8a9ab0;margin-top:6px;line-height:1.5;">
        Full digest &mdash; Basic/Premium subscriber
      </div>
      <div style="font-size:12px;margin-top:10px;">
        <a href="{{{{unsubscribe_url}}}}" style="color:#00897b;text-decoration:underline;">
          Unsubscribe
        </a>
        &nbsp;&middot;&nbsp;
        <a href="https://grantsignal.news/archive"
           style="color:#00897b;text-decoration:underline;">Archive</a>
      </div>
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

def _render_grant_card(grant: dict, rank: int) -> str:
    """Render a single grant as an HTML card for the archive page."""
    score     = grant.get("_score", 0)
    title     = grant.get("title",      "Untitled") or "Untitled"
    agency    = grant.get("agency", "Unknown Agency") or "Unknown Agency"
    close_dt  = grant.get("closeDate",  "") or ""
    opp_num   = grant.get("number",  "") or ""
    synopsis  = (grant.get("synopsis",  "") or "")[:500]
    urgency   = urgency_flag(close_dt)
    url       = grants_gov_url(opp_num) if opp_num else "https://www.grants.gov"

    full_stars = int(score)
    half_star  = 1 if (score - full_stars) >= 0.5 else 0
    empty_stars = 5 - full_stars - half_star
    stars_display = "⭐" * full_stars + ("✨" if half_star else "") + "☆" * empty_stars

    css_class = "grant-card"
    if score >= 4.0:
        css_class += " high-fit"
    if urgency:
        css_class += " urgent"

    urgency_html = (
        f'<span class="grant-urgency">⚡ {urgency}</span>'
    ) if urgency else ""

    def escape(s):
        return (str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    return f"""
    <div class="{css_class}">
      <div class="grant-rank">#{rank}</div>
      <div class="grant-agency">{escape(agency)}</div>
      <h3 class="grant-title">
        <a href="{escape(url)}" target="_blank" rel="noopener">{escape(title)}</a>
      </h3>
      <div class="grant-scores">
        <span class="grant-stars">{stars_display}</span>
        <span class="grant-fit-label">Fit Score: {score}/5</span>
        {urgency_html}
      </div>
      <p class="grant-synopsis">{escape(synopsis)}{"…" if len(synopsis) == 500 else ""}</p>
      <div class="grant-footer">
        <span class="grant-close-date">Closes: <strong>{escape(close_dt) or "See Grants.gov for deadline"}</strong></span>
        <a href="{escape(url)}" target="_blank" rel="noopener" class="grant-link">View on Grants.gov →</a>
      </div>
    </div>"""


def save_archive_entry(grants: list[dict], week_date: datetime.date) -> list[str]:
    """
    Build a standalone archive HTML page for this week's digest and update
    archive/issues.json.

    Args:
        grants:     The paid_digest list (scored + sorted grants).
        week_date:  The date representing this digest week (usually today).

    Returns:
        List of file paths that were written (relative to repo root),
        suitable for `git add`.
    """
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)

    slug       = week_date.strftime("%Y-%m-%d")
    week_label = week_date.strftime("Week of %b %-d, %Y")
    issue_date = week_date.strftime("%B %-d, %Y")
    grant_count = len(grants)
    top_grants  = [g.get("title", "") for g in grants[:5] if g.get("title")]

    # ── Build grant cards HTML ────────────────────────────────────────────────
    if grants:
        cards_html = "\n".join(_render_grant_card(g, i+1) for i, g in enumerate(grants))
    else:
        cards_html = '<p style="color:var(--text-muted);text-align:center;padding:32px;">No grants matched this week.</p>'

    # ── Read the template ─────────────────────────────────────────────────────
    template_path = os.path.join(_ARCHIVE_DIR, "digest-template.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            page_html = f.read()
    else:
        # Fallback minimal structure if template missing
        page_html = "<html><body>{{GRANT_CARDS}}</body></html>"

    # ── Substitute template placeholders ─────────────────────────────────────
    page_title   = f"GrantSignal — {week_label}"
    og_title     = f"GrantSignal Digest — {week_label}"
    og_desc      = f"{grant_count} federal grant opportunities for nonprofits and schools matched this week."
    meta_desc    = og_desc

    replacements = {
        "{{PAGE_TITLE}}":    page_title,
        "{{OG_TITLE}}":      og_title,
        "{{OG_DESCRIPTION}}": og_desc,
        "{{META_DESCRIPTION}}": meta_desc,
        "{{SLUG}}":          slug,
        "{{WEEK_LABEL}}":    week_label,
        "{{DIGEST_TITLE}}":  week_label,
        "{{ISSUE_DATE}}":    issue_date,
        "{{GRANT_COUNT}}":   str(grant_count),
        "{{GRANT_CARDS}}":   cards_html,
    }
    for placeholder, value in replacements.items():
        page_html = page_html.replace(placeholder, value)

    # ── Write archive/YYYY-MM-DD.html ─────────────────────────────────────────
    archive_html_path = os.path.join(_ARCHIVE_DIR, f"{slug}.html")
    with open(archive_html_path, "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"[archive] Written {archive_html_path}")

    # ── Update archive/issues.json ────────────────────────────────────────────
    issues_json_path = os.path.join(_ARCHIVE_DIR, "issues.json")
    if os.path.exists(issues_json_path):
        with open(issues_json_path, "r", encoding="utf-8") as f:
            try:
                issues = json.load(f)
            except json.JSONDecodeError:
                issues = []
    else:
        issues = []

    new_entry = {
        "date":        slug,
        "title":       week_label,
        "slug":        slug,
        "grant_count": grant_count,
        "top_grants":  top_grants,
    }

    # Remove any existing entry for this same date (idempotent re-runs)
    issues = [i for i in issues if i.get("slug") != slug]

    # Prepend new entry (newest first)
    issues.insert(0, new_entry)

    with open(issues_json_path, "w", encoding="utf-8") as f:
        json.dump(issues, f, indent=2, ensure_ascii=False)
    print(f"[archive] Updated {issues_json_path} ({len(issues)} total issues)")

    return [
        f"archive/{slug}.html",
        "archive/issues.json",
    ]


# ---------------------------------------------------------------------------
# Step 6b: Load subscriber preferences for personalization
# ---------------------------------------------------------------------------

def load_subscriber_preferences() -> dict:
    """
    Load all subscriber preference files from data/preferences/ in GitHub repo.
    Returns dict keyed by SHA-256 email hash: { hash: preferences_dict }
    """
    url = "https://api.github.com/repos/Agent17D/grantsignal-site/contents/data/preferences"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            print("[prefs] data/preferences/ directory not found — returning empty prefs")
            return {}
        resp.raise_for_status()
        files = resp.json()
    except requests.RequestException as exc:
        print(f"[prefs] ERROR listing preferences directory: {exc}")
        return {}

    all_prefs = {}
    for file_entry in files:
        fname = file_entry.get("name", "")
        if not fname.endswith(".json"):
            continue
        hash_key = fname[:-5]  # strip .json extension
        file_url = file_entry.get("url", "")
        try:
            file_resp = requests.get(file_url, headers=headers, timeout=10)
            file_resp.raise_for_status()
            file_data = file_resp.json()
            raw_content = base64.b64decode(file_data["content"]).decode("utf-8")
            prefs = json.loads(raw_content)
            all_prefs[hash_key] = prefs
        except Exception as exc:
            print(f"[prefs] WARN could not load preferences for {fname}: {exc}")
            continue

    print(f"[prefs] Loaded {len(all_prefs)} subscriber preference profiles")
    return all_prefs


def filter_grants_for_subscriber(grants: list, prefs: dict) -> list:
    """
    Filter and sort grants based on a subscriber's preferences.
    Returns filtered list, preserving score-based ordering.
    """
    if not prefs:
        return grants

    # ── Budget filtering ────────────────────────────────────────────────────
    budget = prefs.get("budget", "Any size")
    budget_limits = {
        "Under $100K":    200000,
        "$100K – $500K":  750000,
        "$500K – $1M":    2000000,
    }
    ceiling = budget_limits.get(budget)  # None means no filter

    if ceiling is not None:
        filtered = [
            g for g in grants
            if g.get("awardCeiling") is None or (g.get("awardCeiling") or 0) <= ceiling
        ]
    else:
        filtered = list(grants)

    # ── State filtering (log only — grant API doesn't reliably include state) ─
    state = prefs.get("state", "")
    if state:
        print(f"[prefs] NOTE: subscriber has state preference '{state}' but state filtering is not applied (API limitation)")

    # ── Mission area boost for sorting ──────────────────────────────────────
    mission_area = (prefs.get("mission_area", "") or "").lower()

    def sort_key(g):
        base_score = g.get("_score", 0) or 0
        boost = 0.0
        if mission_area:
            categories = [c.lower() for c in (g.get("_categories", []) or [])]
            title_lower = (g.get("title", "") or "").lower()
            synopsis_lower = (g.get("synopsis", "") or "").lower()
            if (any(mission_area in cat for cat in categories)
                    or mission_area in title_lower
                    or mission_area in synopsis_lower):
                boost = 0.5
        return base_score + boost

    return sorted(filtered, key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# Federal Register integration — early-alert grant notices
# ---------------------------------------------------------------------------

def _fr_title_similarity(title_a: str, title_b: str) -> float:
    """Simple word-overlap similarity — no external libraries needed."""
    words_a = set(title_a.lower().split())
    words_b = set(title_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(len(words_a), len(words_b))


def fetch_federal_register_grants(existing_grants: list[dict] | None = None) -> list[dict]:
    """
    Pull recent grant notices from the Federal Register API and normalize them
    into the same dict format used by Grants.gov results.

    Runs 3 separate search queries and deduplicates against existing_grants
    (and against itself) by title similarity (>80% overlap → skip).
    """
    existing_grants = existing_grants or []
    existing_titles = [g.get("title", "") for g in existing_grants]

    # Exclude Federal Register notices that are NOT actual grant opportunities
    FR_EXCLUDE_KEYWORDS = [
        "information collection", "comment request", "paperwork reduction",
        "agency information collection", "proposed information collection",
        "notice of intent", "environmental impact", "record of decision",
        "availability of", "request for information", "rfi",
        "advance notice of proposed rulemaking", "anprm",
        "notice of funding availability" # these go through Grants.gov already
    ]

    SEARCH_TERMS = [
        "grants nonprofit",
        "funding opportunity nonprofit",
        "grant program community",
    ]

    FR_BASE = "https://www.federalregister.gov/api/v1/documents.json"
    FR_FIELDS = [
        "title", "document_number", "publication_date",
        "html_url", "abstract", "agencies", "dates",
    ]

    seen_doc_numbers: set[str] = set()
    results: list[dict] = []

    for term in SEARCH_TERMS:
        try:
            params: dict = {
                "conditions[type][]": "NOTICE",
                "conditions[term]": term,
                "per_page": 100,
                "order": "newest",
            }
            for f in FR_FIELDS:
                params.setdefault("fields[]", [])
                if isinstance(params["fields[]"], list):
                    params["fields[]"].append(f)
                else:
                    params["fields[]"] = [params["fields[]"], f]

            resp = requests.get(FR_BASE, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[federal-register] Warning: query '{term}' failed — {exc}")
            continue

        for doc in data.get("results", []):
            doc_number = doc.get("document_number", "")
            if not doc_number or doc_number in seen_doc_numbers:
                continue

            title = doc.get("title", "") or ""
            if not title:
                continue
            # Skip non-grant Federal Register notices
            title_lower = title.lower()
            if any(kw in title_lower for kw in FR_EXCLUDE_KEYWORDS):
                continue

            # Deduplicate against existing Grants.gov results
            skip = False
            for et in existing_titles:
                if _fr_title_similarity(title, et) > 0.80:
                    skip = True
                    break
            if skip:
                continue

            # Deduplicate within FR results
            already_similar = any(
                _fr_title_similarity(title, r["title"]) > 0.80 for r in results
            )
            if already_similar:
                continue

            seen_doc_numbers.add(doc_number)

            # Normalize dates
            pub_raw = doc.get("publication_date", "") or ""
            try:
                open_date = datetime.datetime.strptime(pub_raw, "%Y-%m-%d").strftime("%m/%d/%Y")
            except Exception:
                open_date = pub_raw

            # dates field from FR is a plain string like "Comments due May 26, 2026."
            # We can't reliably extract a close date from it, so leave blank.
            close_date = ""

            agencies = doc.get("agencies") or []
            agency_name = agencies[0]["name"] if agencies else "Federal Register"

            abstract = doc.get("abstract") or ""

            results.append({
                "id": doc_number,
                "title": title,
                "agency": agency_name,
                "openDate": open_date,
                "closeDate": close_date,
                "synopsis": abstract[:300] if abstract else "",
                "number": doc_number,
                "source": "federal_register",
                "url": doc.get("html_url", "https://www.federalregister.gov"),
            })

    return results


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

    # Also fetch from Federal Register for early alerts
    print("[federal-register] Fetching grant notices...")
    fr_grants = fetch_federal_register_grants(existing_grants=grants)
    print(f"[federal-register] Found {len(fr_grants)} additional opportunities")
    grants.extend(fr_grants)

    # Deduplicate by grant ID
    seen_ids = set()
    unique_grants = []
    for g in grants:
        gid = str(g.get("id") or g.get("number") or g.get("title",""))
        if gid not in seen_ids:
            seen_ids.add(gid)
            unique_grants.append(g)
    print(f"[dedup] {len(grants)} grants → {len(unique_grants)} unique")
    grants = unique_grants

    # ── 2–4. Filter, score, build digests ───────────────────────────────────
    free_digest, paid_digest = build_digests(grants)
    total_matched = len(paid_digest)

    if not paid_digest:
        print("No eligible, scored grants found. Exiting.")
        sys.exit(0)

    # Count urgency across ALL matched grants (for the free upgrade CTA)
    urgency_count = sum(1 for g in paid_digest if is_urgent(g.get("closeDate", "") or ""))

    print(f"[digest] Free digest: {len(free_digest)} grants | "
          f"Paid digest: {len(paid_digest)} grants | "
          f"Urgent: {urgency_count}")

    # ── 5. Build email HTML ──────────────────────────────────────────────────
    free_html = build_free_html(
        free_digest,
        total_matched=total_matched,
        urgency_count=urgency_count,
        all_paid_grants=paid_digest,
    )
    paid_html = build_paid_html(paid_digest)

    # ── 6. Fetch subscribers ─────────────────────────────────────────────────
    free_subscribers = fetch_subscribers("free")
    paid_subscribers = fetch_subscribers("premium")

    # Load subscriber preferences for personalization
    all_prefs = load_subscriber_preferences()

    # ── 7. Send emails ───────────────────────────────────────────────────────
    week_str = datetime.date.today().strftime("%B %d, %Y")

    send_email_batch(
        to_emails=free_subscribers,
        subject="📡 GrantSignal | Your Top 3 Federal Grant Matches This Week",
        html_body=free_html,
        label="FREE",
    )

    # Send personalized digest to each premium subscriber
    if not paid_subscribers:
        print("[resend] No premium recipients — skipping PAID digest.")
    else:
        print(f"[resend] Sending personalized PAID digest to {len(paid_subscribers)} premium subscribers…")
        paid_success = 0
        paid_errors = 0
        for i, email in enumerate(paid_subscribers, start=1):
            # Compute SHA-256 hash of subscriber email for prefs lookup
            email_hash = hashlib.sha256(email.strip().lower().encode()).hexdigest()
            sub_prefs = all_prefs.get(email_hash, {})
            # Filter/sort grants for this subscriber
            sub_grants = filter_grants_for_subscriber(paid_digest, sub_prefs)
            sub_html = build_paid_html(sub_grants)
            sub_count = len(sub_grants)
            payload = {
                "from":    FROM_EMAIL,
                "to":      [email],
                "subject": f"📡 GrantSignal | {sub_count} Federal Grant Matches This Week",
                "html":    sub_html,
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
                paid_success += 1
            except requests.RequestException as exc:
                paid_errors += 1
                print(f"[resend] WARN failed to send PAID to {email}: {exc}")
            if i % 10 == 0:
                print(f"[resend] Progress: {i}/{len(paid_subscribers)} sent…")
        print(f"[resend] PAID complete — {paid_success} sent, {paid_errors} errors.")

    # ── 8. Publish Beehiiv archive post ──────────────────────────────────────

    # ── 9. Save archive entry (runs always, including dry_run) ───────────────
    changed_files = save_archive_entry(paid_digest, datetime.date.today())
    if changed_files:
        print(f"[archive] Files written: {', '.join(changed_files)}")

    print("=" * 60)
    print("  Pipeline complete ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()


