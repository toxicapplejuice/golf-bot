#!/usr/bin/env python3
"""
Scheduler for Golf Booking Bot

Runs the booking bot every Monday at 8:00 PM CT.
Can also be run directly for testing.
"""

import schedule
import time
from datetime import datetime
import subprocess
import sys
import os

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_bot():
    """Execute the booking bot."""
    print(f"\n[{datetime.now()}] Starting golf booking bot...")

    try:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "bot.py")],
            capture_output=True,
            text=True,
            cwd=SCRIPT_DIR,
        )
        print(result.stdout)
        if result.stderr:
            print(f"STDERR: {result.stderr}")
    except Exception as e:
        print(f"Error running bot: {e}")


def main():
    """Main scheduler loop."""
    print("=" * 50)
    print("Golf Booking Bot Scheduler")
    print("Scheduled to run every Monday at 8:00 PM CT")
    print("=" * 50)

    # Schedule for Monday at 8:00 PM
    # Note: Assumes system is set to CT timezone
    schedule.every().monday.at("20:00").do(run_bot)

    print(f"Current time: {datetime.now()}")
    print(f"Next run: {schedule.next_run()}")
    print("\nWaiting for scheduled time... (Ctrl+C to exit)")

    while True:
        schedule.run_pending()
        time.sleep(30)  # Check every 30 seconds


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        # Run immediately for testing
        print("Running bot immediately (test mode)...")
        run_bot()
    else:
        main()
