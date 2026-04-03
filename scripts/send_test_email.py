"""Run this script to send a welcome email to a test address.

Usage:
    python scripts/send_test_email.py

Ensure your .env contains ZOHO_* settings before running.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path so `app` can be imported when running this script directly
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.email_service import send_welcome_email

logging.basicConfig(level=logging.INFO)


async def main():
    to_email = "abdulabiola21@gmail.com"
    name = "Jimoh Abdulsomad Abiola"
    print(f"Sending welcome email to {to_email} (name={name})...")
    await send_welcome_email(to_email, name)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
