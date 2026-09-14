import mysql.connector
import pandas as pd

#DB_CONFIG from the main script
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '####',
    'database': 'job_bot'
}

def fetch_all_jobs():
    db = mysql.connector.connect(**DB_CONFIG)
    df = pd.read_sql("SELECT * FROM seen_jobs ORDER BY date_found DESC", db)
    db.close()
    
    if df.empty:
        print("No jobs found in database.")
    else:
        print(df[['title', 'company', 'date_found', 'job_url']])

if __name__ == "__main__":
    fetch_all_jobs()
