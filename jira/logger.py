#!/usr/bin/env python3
"""
Standalone Jira time logger.

Usage:
    python logger.py <task_ids> <start_date> <end_date> [hours_per_day] [--dry-run] [--skip DATE ...]

    task_ids        - Comma-separated Jira issue keys (e.g. PROJ-1,PROJ-2,PROJ-3)
    start_date      - Start date in DD/MM/YYYY format
    end_date        - End date in DD/MM/YYYY format (inclusive)
    hours_per_day   - Hours to log each weekday (default: 8)
    --dry-run       - Preview what would be logged without making requests
    --skip          - One or more dates to skip, each in DD/MM/YYYY format

Multiple tasks rotate round-robin: day 1 -> task 1, day 2 -> task 2, etc.

Requires JIRA_PAT environment variable to be set.
"""

import sys
import os
import requests
from datetime import datetime, timedelta

JIRA_DOMAIN = 'https://jira.critical.pt'


def log_work(issue_key, time_spent, started, headers):
    worklog_url = f'{JIRA_DOMAIN}/rest/api/2/issue/{issue_key}/worklog'
    payload = {
        "started": started,
        "timeSpent": time_spent,
    }
    resp = requests.post(worklog_url, headers=headers, json=payload)
    return resp.status_code == 201, resp.text


def weekdays_between(start, end):
    """Yield each weekday (Mon-Fri) between start and end inclusive."""
    day = start
    while day <= end:
        if day.weekday() < 5:  # 0=Mon .. 4=Fri
            yield day
        day += timedelta(days=1)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    dry_run = '--dry-run' in sys.argv

    # Extract --skip dates
    skip_dates = set()
    raw_args = sys.argv[1:]
    if '--skip' in raw_args:
        skip_idx = raw_args.index('--skip')
        i = skip_idx + 1
        while i < len(raw_args) and not raw_args[i].startswith('--'):
            try:
                skip_dates.add(datetime.strptime(raw_args[i], '%d/%m/%Y'))
            except ValueError:
                print(f"Error: invalid skip date '{raw_args[i]}', must be DD/MM/YYYY format.")
                sys.exit(1)
            i += 1
        raw_args = raw_args[:skip_idx] + raw_args[i:]

    args = [a for a in raw_args if a != '--dry-run']

    task_ids = [t.strip() for t in args[0].split(',')]
    start_str = args[1]
    end_str = args[2]
    hours = args[3] if len(args) > 3 else "8"

    pat = os.getenv('JIRA_PAT')
    if not pat:
        print("Error: JIRA_PAT environment variable is not set.")
        sys.exit(1)

    try:
        start_date = datetime.strptime(start_str, '%d/%m/%Y')
        end_date = datetime.strptime(end_str, '%d/%m/%Y')
    except ValueError:
        print("Error: dates must be in DD/MM/YYYY format.")
        sys.exit(1)

    if end_date < start_date:
        print("Error: end date must be on or after start date.")
        sys.exit(1)

    time_spent = f"{hours}h"
    headers = {
        'Authorization': f'Bearer {pat}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    days = [d for d in weekdays_between(start_date, end_date) if d not in skip_dates]
    mode = "[DRY RUN] " if dry_run else ""
    tasks_str = ', '.join(task_ids)
    skip_str = f", skipping {len(skip_dates)} date(s)" if skip_dates else ""
    print(f"{mode}Logging {time_spent}/day across [{tasks_str}] (round-robin) "
          f"for {len(days)} weekdays ({start_str} -> {end_str}{skip_str})\n")

    for i, day in enumerate(days):
        task_id = task_ids[i % len(task_ids)]
        # Jira worklog "started" format: "2024-01-15T09:00:00.000+0000"
        started = day.strftime('%Y-%m-%dT09:00:00.000+0000')
        if dry_run:
            print(f"  {day.strftime('%a %d/%m/%Y')} — {task_id} — would log {time_spent}")
        else:
            ok, detail = log_work(task_id, time_spent, started, headers)
            status = "OK" if ok else f"FAILED ({detail})"
            print(f"  {day.strftime('%a %d/%m/%Y')} — {task_id} — {status}")

    print("\nDone.")


if __name__ == '__main__':
    main()
