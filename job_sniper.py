import os
import re
import time
import html
import smtplib
import datetime
from email.message import EmailMessage

import pandas as pd
import requests
from dotenv import load_dotenv
from jobspy import scrape_jobs

load_dotenv()

# =====================================================================
# CONFIGURATION
# =====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
APP_PASSWORD = os.getenv("APP_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
# --- 1. Sources ---
# A failing source is skipped; it never stops the others.
# "naukri" needs a recent python-jobspy (pip install -U python-jobspy).
SITES = ["indeed", "linkedin", "glassdoor", "naukri", "google"]

SEARCH_QUERIES = [
    "Data Analyst",
    "MIS Executive",
    "Business Analyst",
    "Power BI Developer",
    "(SQL OR Python) AND Analyst",
]
LOCATION = "Noida"
RESULTS_PER_QUERY = 15
HOURS_OLD = 48
PAUSE_BETWEEN_SEARCHES = 3  # seconds, to avoid rate limits

# --- 4. Location filter ---
TIER1_LOCATIONS = ["noida", "greater noida", "ghaziabad"]
TIER2_LOCATIONS = ["delhi", "new delhi", "gurgaon", "gurugram", "faridabad"]
ALLOW_REMOTE = True
ALLOW_UNKNOWN_LOCATION = True   # jobs with no location listed
ONLY_TIER1 = False              # True = Noida/Greater Noida/Ghaziabad only

# --- 5. Experience (your range: 0-3 years) ---
MAX_EXPERIENCE_YEARS = 3   # reject if the job's *minimum* required exp is above this

# --- 3. Salary ---
MIN_SALARY_LPA = 0         # 0 = off. Only rejects jobs whose stated max salary is below this.

# --- 2. Relevance ---
MIN_SCORE = 40             # 0-100, raise to get fewer/cleaner alerts
MAX_ALERTS_PER_RUN = 25

REQUIRED_TITLE_WORDS = [
    "data", "analyst", "mis", "analytics", "bi", "sql", "python",
    "power bi", "excel", "reporting", "business intelligence", "insights",
]
EXCLUDED_TITLE_WORDS = [
    "senior", "sr", "lead", "manager", "head", "principal", "director", "vp",
    "architect", "finance", "sales", "customer", "support", "voice", "bpo",
    "calling", "telecalling", "us process", "international", "hr",
    "recruiter", "trainer", "faculty",
]

TITLE_SCORES = {
    "data analyst": 40, "mis executive": 40, "mis analyst": 40,
    "reporting analyst": 35, "business analyst": 30, "analytics": 30,
    "mis": 30, "power bi": 30, "business intelligence": 30,
    "analyst": 20, "data": 15, "bi": 10,
}
SKILL_SCORES = {
    "power bi": 6, "sql": 6, "python": 5, "excel": 4, "tableau": 4,
    "pandas": 3, "dashboard": 3, "vba": 3, "data visualization": 3,
    "pivot": 2, "looker": 2, "reporting": 2,
}
SKILL_CAP = 30
BAD_DESCRIPTION_PHRASES = [
    "cold calling", "telecalling", "target based", "target-based",
    "field sales", "commission only", "voice process",
]

SEEN_FILE = "seen_jobs.txt"


# =====================================================================
# HELPERS
# =====================================================================
def safe_str(row, key, default=""):
    v = row.get(key)
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    return str(v)


def has_word(text, words):
    """Whole-word match, so 'bi' doesn't match 'mobile' and 'hr' doesn't match 'through'."""
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


# =====================================================================
# 5. EXPERIENCE DETECTION
# =====================================================================
RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\+?\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b"
)
SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b")
FRESHER_RE = re.compile(r"\bfreshers?\b|entry[- ]level|no experience|\b0 years?")


def _near_experience(text, start, end):
    return "exp" in text[max(0, start - 45): end + 45]


def detect_experience(description, experience_range=""):
    """
    Returns (min_years_required or None, label).
    Understands '0-2 years', '1 to 3 yrs', '2+ years of experience',
    'minimum 3 years', 'freshers'. Only counts numbers near the word
    'experience' so '10 years of company history' is ignored.
    """
    text = (description or "").lower()
    if experience_range:
        text += f" experience {experience_range.lower()}"

    mins, range_spans = [], []
    for m in RANGE_RE.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo <= hi <= 40 and _near_experience(text, m.start(), m.end()):
            mins.append(lo)
            range_spans.append(m.span())

    for m in SINGLE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in range_spans):
            continue
        n = float(m.group(1))
        if n <= 40 and _near_experience(text, m.start(), m.end()):
            mins.append(n)

    if not mins and FRESHER_RE.search(text):
        mins = [0.0]

    if not mins:
        return None, "Not stated"
    lo = max(mins)
    return lo, ("Fresher OK" if lo == 0 else f"{lo:g}+ yrs")


# =====================================================================
# 3. SALARY EXTRACTION
# =====================================================================
_ANNUAL_FACTOR = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}
_NUM = r"([\d,]+(?:\.\d+)?)"
_CUR = r"(?:₹|rs\.?|inr)"

LPA_RE = re.compile(
    rf"{_CUR}?\s*(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*"
    r"(?:lpa|l\.p\.a|lakhs?|lacs?)\b"
)
MONTHLY_RE = re.compile(
    rf"{_CUR}\s*{_NUM}(?:\s*(?:-|–|to)\s*{_CUR}?\s*{_NUM})?\s*"
    r"(?:/|per|a)?\s*(?:month|mo\b|pm\b|monthly)"
)
ANNUAL_RE = re.compile(
    rf"{_CUR}\s*{_NUM}(?:\s*(?:-|–|to)\s*{_CUR}?\s*{_NUM})?\s*"
    r"(?:/|per|a)?\s*(?:annum|year|yearly|p\.a|pa\b)"
)


def _num(s):
    return float(s.replace(",", ""))


def _fmt_lpa(lo, hi):
    if hi is None or abs(hi - lo) < 0.05:
        return f"₹{lo:.1f} LPA"
    return f"₹{lo:.1f}–{hi:.1f} LPA"


def extract_salary(row, description):
    """Returns (label, max_lpa or None). Uses structured data first, then the description text."""
    lo, hi = row.get("min_amount"), row.get("max_amount")
    interval = safe_str(row, "interval").lower()
    currency = safe_str(row, "currency", "INR").upper()
    try:
        if lo is not None and not pd.isna(lo) and interval in _ANNUAL_FACTOR:
            f = _ANNUAL_FACTOR[interval]
            lo_a = float(lo) * f
            hi_a = float(hi) * f if hi is not None and not pd.isna(hi) else lo_a
            if currency in ("INR", ""):
                lo_l, hi_l = lo_a / 1e5, hi_a / 1e5
                if 0.5 <= lo_l <= 200:
                    return _fmt_lpa(lo_l, hi_l), hi_l
            else:
                return f"{currency} {lo_a:,.0f}–{hi_a:,.0f}/yr", None
    except (TypeError, ValueError):
        pass

    text = (description or "").lower()

    m = LPA_RE.search(text)
    if m:
        lo_l = float(m.group(1))
        hi_l = float(m.group(2)) if m.group(2) else lo_l
        if 0.5 <= lo_l <= 200:
            return _fmt_lpa(lo_l, hi_l), hi_l

    for regex, mult in ((MONTHLY_RE, 12), (ANNUAL_RE, 1)):
        m = regex.search(text)
        if m:
            lo_a = _num(m.group(1)) * mult
            hi_a = _num(m.group(2)) * mult if m.group(2) else lo_a
            lo_l, hi_l = lo_a / 1e5, hi_a / 1e5
            if 0.5 <= lo_l <= 200:
                return _fmt_lpa(lo_l, hi_l), hi_l

    return "Not disclosed", None


# =====================================================================
# 4. LOCATION FILTER
# =====================================================================
def check_location(row, title):
    """Returns (allowed, points, label)."""
    loc = safe_str(row, "location").lower()
    is_remote = str(row.get("is_remote")).lower() == "true" or "remote" in loc or "remote" in title

    if any(c in loc for c in TIER1_LOCATIONS):
        return True, 10, "Noida region"
    if not ONLY_TIER1 and any(c in loc for c in TIER2_LOCATIONS):
        return True, 6, "Delhi NCR"
    if ALLOW_REMOTE and is_remote:
        return True, 5, "Remote"
    if not loc.strip() and ALLOW_UNKNOWN_LOCATION:
        return True, 2, "Location not listed"
    return False, 0, loc.title()


# =====================================================================
# 2. RELEVANCE SCORING
# =====================================================================
def score_job(title, description, min_exp, loc_points, has_salary):
    score = max((pts for phrase, pts in TITLE_SCORES.items() if has_word(title, [phrase])), default=0)

    skills = sum(pts for skill, pts in SKILL_SCORES.items() if skill in description)
    score += min(skills, SKILL_CAP)

    if min_exp is None:
        score += 6
    elif min_exp <= 1:
        score += 15
    elif min_exp <= 2:
        score += 12
    else:
        score += 8

    score += loc_points
    score += 3 if has_salary else 0
    score -= 15 * sum(1 for p in BAD_DESCRIPTION_PHRASES if p in description)
    return max(0, min(100, score))


def score_badge(score):
    return "🔥" if score >= 75 else "⭐" if score >= 60 else "✅"


# =====================================================================
# DEDUPLICATION (Title + Company, so the same job on two sites counts once)
# =====================================================================
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(f.read().splitlines())


def mark_seen(seen, title, company):
    job_id = f"{title.lower().strip()}|{company.lower().strip()}"
    if job_id in seen:
        return False
    seen.add(job_id)
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(job_id + "\n")
    return True


# =====================================================================
# NOTIFICATIONS
# =====================================================================
def send_telegram_message(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("ERROR: Telegram credentials missing. Check your .env file. Stopping.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")


def format_telegram(job):
    e = html.escape
    return (
        f"{score_badge(job['score'])} <b>{e(job['title'])}</b>  ·  {job['score']}/100\n"
        f"🏢 {e(job['company'])}\n"
        f"📍 {e(job['location'])}\n"
        f"💼 {e(job['experience'])}\n"
        f"💰 {e(job['salary'])}\n"
        f"🔎 {e(job['site'].title())}\n\n"
        f"<a href='{e(job['url'], quote=True)}'>Apply Now</a>"
    )


def send_email_digest(jobs):
    if not (SENDER_EMAIL and APP_PASSWORD and RECEIVER_EMAIL) or not jobs:
        return
    e = html.escape
    rows = "".join(
        f"<tr>"
        f"<td>{score_badge(j['score'])} {j['score']}</td>"
        f"<td><b>{e(j['title'])}</b><br>{e(j['company'])}</td>"
        f"<td>{e(j['location'])}</td><td>{e(j['experience'])}</td>"
        f"<td>{e(j['salary'])}</td><td>{e(j['site'].title())}</td>"
        f"<td><a href='{e(j['url'], quote=True)}'>Apply</a></td></tr>"
        for j in jobs
    )
    body = f"""
    <html><body style="font-family: Arial; line-height: 1.5;">
      <h2 style="color:#0056b3;">{len(jobs)} new matching jobs</h2>
      <table border="0" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
        <tr style="background:#f0f0f0;"><th>Score</th><th>Role</th><th>Location</th>
        <th>Experience</th><th>Salary</th><th>Source</th><th></th></tr>
        {rows}
      </table>
    </body></html>"""

    msg = EmailMessage()
    msg["Subject"] = f"🚨 {len(jobs)} new Data/MIS jobs (best score: {jobs[0]['score']})"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.set_content("Please enable HTML to view this email.")
    msg.add_alternative(body, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.send_message(msg)
    except Exception as ex:
        print(f"Gmail Error: {ex}")


# =====================================================================
# SCRAPING
# =====================================================================
def fetch_jobs(site, query):
    kwargs = dict(
        site_name=[site],
        search_term=query,
        location=LOCATION,
        results_wanted=RESULTS_PER_QUERY,
        country_indeed="india",
        hours_old=HOURS_OLD,
        description_format="markdown",
        verbose=0,
    )
    if site == "linkedin":
        kwargs["linkedin_fetch_description"] = True  # needed for experience/salary detection
    if site == "google":
        kwargs["google_search_term"] = f"{query} jobs near {LOCATION} since yesterday"
    return scrape_jobs(**kwargs)


def evaluate_row(row, query):
    """Returns a job dict if the row passes all filters, else None."""
    title = safe_str(row, "title").lower().strip()
    company = safe_str(row, "company", "Unknown Company")
    url = safe_str(row, "job_url")
    description = safe_str(row, "description").lower()
    if not title or not url:
        return None

    if not has_word(title, REQUIRED_TITLE_WORDS) or has_word(title, EXCLUDED_TITLE_WORDS):
        return None

    loc_ok, loc_points, loc_label = check_location(row, title)
    if not loc_ok:
        return None

    min_exp, exp_label = detect_experience(description, safe_str(row, "experience_range"))
    if min_exp is not None and min_exp > MAX_EXPERIENCE_YEARS:
        return None

    salary_label, salary_max = extract_salary(row, description)
    if MIN_SALARY_LPA and salary_max is not None and salary_max < MIN_SALARY_LPA:
        return None

    score = score_job(title, description, min_exp, loc_points, salary_max is not None)
    if score < MIN_SCORE:
        return None

    return {
        "title": title.title(), "company": company, "url": url,
        "location": loc_label, "experience": exp_label, "salary": salary_label,
        "score": score, "site": safe_str(row, "site", "unknown"), "query": query,
    }


def run_scraper():
    print(f"\n--- Deep Scan Started: {datetime.datetime.now():%Y-%m-%d %H:%M} ---")
    seen = load_seen()
    matches = []

    for site in SITES:
        for query in SEARCH_QUERIES:
            print(f"[{site}] {query}...")
            try:
                df = fetch_jobs(site, query)
            except Exception as e:
                print(f"  Skipped ({site}): {e}")
                continue
            finally:
                time.sleep(PAUSE_BETWEEN_SEARCHES)

            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                job = evaluate_row(row, query)
                if job and mark_seen(seen, job["title"], job["company"]):
                    matches.append(job)

    matches.sort(key=lambda j: j["score"], reverse=True)
    matches = matches[:MAX_ALERTS_PER_RUN]

    for job in matches:
        send_telegram_message(format_telegram(job))
        time.sleep(0.5)
    send_email_digest(matches)

    print(f"Scan Complete. {len(matches)} alerts sent.")


if __name__ == "__main__":
    run_scraper()