# SIH 2026 result monitor

Checks sih.gov.in every 60 seconds on GitHub Actions (runs 24/7, laptop not needed) and pushes
a phone notification the moment anything changes.

What it watches:
- **New links / new text** on https://sih.gov.in/ and https://finale.sih.gov.in/
- **Expected result URLs going live** (currently 404), e.g. `/sih2026`, `/sih2026/sih2026-grand-finale-result`,
  `/sih2026/nodalcenter` (same pattern SIH used for 2023-2025 results)
- Changes containing words like *result / shortlist / batch / finalist / nodal / 2026* are sent as **urgent**
- If the site stops responding for ~15 min straight, you get a heads-up (often means results just dropped and traffic spiked)

## Notifications
- **ntfy** (default): install the ntfy app (Android / iOS), subscribe to the topic stored in the `NTFY_TOPIC` repo secret.
- **Telegram** (optional): add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets.

## Commands
```bash
gh workflow run monitor.yml -f test_notify=true   # send a test notification
gh run list --workflow monitor.yml                # see runs
gh workflow disable monitor.yml                    # stop after results are out
python3 monitor.py --loop-minutes 60 --interval 30 # run locally
```
