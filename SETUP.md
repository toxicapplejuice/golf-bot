# Austin Golf Booking Bot - Setup Guide

## Quick Start

1. **Install dependencies:**
   ```bash
   cd ~/golf-bot
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Configure credentials:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Test the setup:**
   ```bash
   python test_setup.py
   ```

4. **Run manually (test):**
   ```bash
   python scheduler.py --now
   ```

## Configuration (.env file)

```
GOLF_USERNAME=your_vermont_systems_username
GOLF_PASSWORD=your_vermont_systems_password

# For Gmail, use an App Password (not your regular password)
# Generate at: https://myaccount.google.com/apppasswords
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password
NOTIFICATION_EMAIL=your_email@gmail.com
```

## Server Deployment (Cron)

To run automatically every Monday at 8:00 PM CT:

```bash
# Edit crontab
crontab -e

# Add this line (adjust path and timezone as needed):
0 20 * * 1 cd /path/to/golf-bot && /usr/bin/python3 bot.py >> /var/log/golf-bot.log 2>&1
```

**Timezone note:** The cron time should be in your server's timezone.
- If server is in CT: `0 20 * * 1` (8:00 PM)
- If server is in UTC: `0 2 * * 2` (2:00 AM Tuesday = 8:00 PM Monday CT)

## How It Works

1. Bot runs every Monday at 8pm CT (when weekend times are released)
2. Logs in to Vermont Systems with your credentials
3. Searches for Saturday and Sunday tee times
4. Filters for Lions, Roy Kizer, and Jimmy Clay courses
5. Prioritizes times: 9am > 10am > 8am > 11am
6. Books for 4 players
7. Sends email confirmation

## Troubleshooting

**Login fails:**
- Verify credentials at https://txaustinweb.myvscloud.com/webtrac/web/
- Check .env file formatting (no quotes needed)

**No tee times found:**
- Times may be sold out already
- Site structure may have changed (check selectors in bot.py)

**Email not sending:**
- For Gmail, you must use an App Password
- Check SMTP settings and firewall rules

## Files

- `bot.py` - Main booking logic
- `scheduler.py` - Runs bot on schedule (or `--now` for testing)
- `test_setup.py` - Verify configuration
- `config.py` - Courses, times, preferences
- `.env` - Your credentials (gitignored)
