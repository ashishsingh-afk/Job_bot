import os
import smtplib
from email.message import EmailMessage
import requests
from jobspy import scrape_jobs
import datetime
import re

# --- CONFIGURATION ---
TELEGRAM_TOKEN = 'My Telegram Token' 
TELEGRAM_CHAT_ID = 'My Chat Id of Telegram'

SENDER_EMAIL = 'xyz@gmail.com'
APP_PASSWORD = 'Mailapppassword' 
RECEIVER_EMAIL = 'xyz@gmail.com' 

SEARCH_QUERIES = [
    "Data Analyst", 
    "MIS Executive",
    "(Power BI OR Advanced Excel) AND Data", 
    "(SQL OR Python) AND Analyst"
]
LOCATION = "Noida"  
RESULTS_PER_QUERY = 15

# 1. Must-Have Words: Title MUST contain at least one of these
REQUIRED_KEYWORDS = ["data", "analyst", "mis", "analytics", "bi", "sql", "python", "power bi", "excel"]

# 2. Banned Words: Title MUST NOT contain any of these
EXCLUDED_KEYWORDS = [
    "senior", "sr", "lead", "manager", "head", "finance", "sales", 
    "customer", "support", "voice", "bpo", "calling", "telecalling", 
    "us process", "international", "associate", "hr"
]

# --- FUNCTIONS ---
def send_telegram_message(text):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def send_email_alert(role, company, url, query):
    msg = EmailMessage()
    msg['Subject'] = f"🚨 Verified 0-1 YOE Match: {role}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL

    html_content = f"""
    <html>
      <body style="font-family: Arial; line-height: 1.6;">
        <h2 style="color: #0056b3;">Verified Job Found</h2>
        <p><b>Role:</b> {role}</p>
        <p><b>Company:</b> {company}</p>
        <p><b>Source Query:</b> {query}</p>
        <br>
        <a href="{url}" style="background-color: #28a745; color: white; padding: 10px; text-decoration: none; border-radius: 5px;">View Job</a>
      </body>
    </html>
    """
    msg.set_content("Please enable HTML.")
    msg.add_alternative(html_content, subtype='html')

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"Gmail Error: {e}")

def is_job_new(title, company):
    """Tracks Title + Company instead of URL to permanently stop duplicates."""
    filename = "seen_jobs.txt"
    # Create a unique lowercase ID for the job
    job_id = f"{title.lower().strip()}|{company.lower().strip()}"
    
    if not os.path.exists(filename):
        open(filename, 'w', encoding='utf-8').close()
        
    with open(filename, 'r', encoding='utf-8') as file:
        if job_id in file.read().splitlines():
            return False
            
    with open(filename, 'a', encoding='utf-8') as file:
        file.write(job_id + '\n')
    return True

def requires_too_much_experience(description):
    """Scans the actual job description text for '2+ years', '3 years', etc."""
    if not description:
        return False
    # Regex looks for numbers 2 through 15 followed by the word 'year' or 'years'
    match = re.search(r'([2-9]|1[0-5])[\+\-\s]*years?', description.lower())
    return bool(match)

# --- MAIN LOGIC ---
def run_scraper():
    print(f"\n--- Deep Scan Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} ---")
    new_jobs_found = 0

    for query in SEARCH_QUERIES:
        print(f"Searching for: {query}...")
        try:
            jobs_df = scrape_jobs(
                site_name=["indeed", "linkedin"], 
                search_term=query,
                location=LOCATION,
                results_wanted=RESULTS_PER_QUERY,
                country_circa="india",
                hours_old=48,
                experience_level=["intern", "entry_level"] 
            )
            
            if jobs_df.empty:
                continue

            for _, row in jobs_df.iterrows():
                title = str(row.get('title', '')).lower()
                company = str(row.get('company', 'Unknown Company'))
                job_url = row.get('job_url', '')
                description = str(row.get('description', '')).lower()

                # FILTER 1: Does it have a REQUIRED word?
                is_relevant = any(req_word in title for req_word in REQUIRED_KEYWORDS)
                
                # FILTER 2: Does it have a BANNED word?
                is_junk = any(bad_word in title for bad_word in EXCLUDED_KEYWORDS)
                
                # FILTER 3: Does the description ask for 2+ years?
                too_much_exp = requires_too_much_experience(description)

                # Final Execution: Must be relevant, must NOT be junk, must NOT want >1 year, and must be new.
                if is_relevant and not is_junk and not too_much_exp:
                    if job_url and is_job_new(title, company):
                        display_title = title.title()
                        
                        tg_msg = (
                            f"🚨 <b>Verified Data Role!</b>\n\n"
                            f"<b>{display_title}</b>\n"
                            f"{company}\n\n"
                            f"<a href='{job_url}'>Apply Now</a>"
                        )
                        send_telegram_message(tg_msg)
                        send_email_alert(display_title, company, job_url, query)
                        new_jobs_found += 1
                    
        except Exception as e:
            print(f"Error during {query} search: {e}")

    print(f"Scan Complete. {new_jobs_found} ultra-clean alerts sent.")

if __name__ == "__main__":
    run_scraper()
