# Flight Daily Tracker (UA SFO -> PVG)

Tracks daily for fixed window:
- Depart: 2026-09-24 to 2026-09-30
- One-way SFO to PVG, non-stop only
- Outputs: cheapest UA-only non-stop Economy award, cheapest UA-only non-stop Premium Economy award, and a historical mileage-deal signal

## Setup

1. Create env file:

```bash
cp .env.example .env
```

2. Edit `.env` and set:
- `REPORT_EMAIL` (optional, supports multiple emails)
  Example: `REPORT_EMAIL=me@example.com,partner@example.com`
- `SMS_TO` (optional, supports multiple numbers in E.164 format)
  Example: `SMS_TO=+14155550123,+14155550124`
- For SMS delivery: set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`
- Recommended for reliable delivery: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`

3. Run once:

```bash
.venv/bin/python scripts/daily_flight_report.py
```

4. Sign in to United once in the tracker browser profile:

```bash
.venv/bin/python scripts/setup_united_browser_login.py
```

5. Install the macOS `launchd` job (starts daily at 07:00 local time, then sends around 08:00):

```bash
bash scripts/install_launchd.sh
```

6. Make sure the Mac wakes before the job runs:

```bash
bash scripts/install_wake_schedule.sh
```

## Output

- Daily report files: `reports/daily_YYYY-MM-DD.txt`
- `launchd` stdout log: `reports/launchd.out.log`
- `launchd` stderr log: `reports/launchd.err.log`
- macOS notification after each run

## Notes

- Deal detection uses this tracker's own history: it marks a deal when mileage is near the tracked low or meaningfully below the tracked average.
- The daily search uses United.com in a persistent local Chrome profile, not a flight-pricing API. If United expires the session or asks for MFA, run the login setup command again.
- If `REPORT_EMAIL` is empty, report is still generated locally and notified on desktop.
- Without SMTP config, script falls back to local `mail`/postfix, which may not relay to external inboxes on macOS.
- If `SMS_TO` is set, Twilio SMS is sent daily (requires Twilio credentials and a Twilio phone number).
- Current mode: United.com signed-in browser award search covering 7 September dates across Economy and Premium Economy, reading non-stop itineraries only.
- The scheduled scan starts before 08:00 so slow United pages have time to load; reports are held until 08:00 if the scan finishes early.
- `launchd` is the recommended scheduler on macOS; plain `cron` is less reliable for sleeping laptops.
- A scheduled wake is separate from the report scheduler. If the Mac stays asleep, the report cannot run until the machine wakes.
