#!/usr/bin/env python3
"""
🏃‍♂️ Simple Garmin Connect API Example
=====================================

This example demonstrates the basic usage of python-garminconnect:
- Authentication with email/password
- Token storage and automatic reuse
- MFA (Multi-Factor Authentication) support
- Comprehensive error handling for all API calls
- Basic API calls for user stats

For a comprehensive demo of all available API calls, see demo.py

Dependencies:
pip install -r requirements.txt

Environment Variables (optional; load from .env if present):
GARMIN_EMAIL / GARMIN_PASSWORD (same as collector), or legacy EMAIL / PASSWORD
GARMINTOKENS — directory for garmin_tokens.json (default: .garmin-tokens under repo root)

If password login hits HTTP 429, seed tokens with:
  pip install -r requirements-browser.txt && python -m playwright install chromium
  python scripts/garmin_playwright_login.py --verify
"""

import logging
import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path

import requests
from dotenv import load_dotenv

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Suppress garminconnect library logging to avoid tracebacks in normal operation
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)


def safe_api_call(api_method, *args, **kwargs):
    """
    Safe API call wrapper with comprehensive error handling.

    This demonstrates the error handling patterns used throughout the library.
    Returns (success: bool, result: Any, error_message: str)
    """
    try:
        result = api_method(*args, **kwargs)
        return True, result, None

    except requests.HTTPError as e:
        error_str = str(e)
        status_code = getattr(getattr(e, "response", None), "status_code", None)

        if status_code == 400 or "400" in error_str:
            return (
                False,
                None,
                "Endpoint not available (400 Bad Request) - Feature may not be enabled for your account",
            )
        elif status_code == 401 or "401" in error_str:
            return (
                False,
                None,
                "Authentication required (401 Unauthorized) - Please re-authenticate",
            )
        elif status_code == 403 or "403" in error_str:
            return (
                False,
                None,
                "Access denied (403 Forbidden) - Account may not have permission",
            )
        elif status_code == 404 or "404" in error_str:
            return (
                False,
                None,
                "Endpoint not found (404) - Feature may have been moved or removed",
            )
        elif status_code == 429 or "429" in error_str:
            return (
                False,
                None,
                "Rate limit exceeded (429) - Please wait before making more requests",
            )
        elif status_code == 500 or "500" in error_str:
            return (
                False,
                None,
                "Server error (500) - Garmin's servers are experiencing issues",
            )
        elif status_code == 503 or "503" in error_str:
            return (
                False,
                None,
                "Service unavailable (503) - Garmin's servers are temporarily unavailable",
            )
        else:
            return False, None, f"HTTP error: {e}"

    except FileNotFoundError:
        return (
            False,
            None,
            "No valid tokens found. Please login with your email/password to create new tokens.",
        )

    except GarminConnectAuthenticationError as e:
        return False, None, f"Authentication issue: {e}"

    except GarminConnectConnectionError as e:
        return False, None, f"Connection issue: {e}"

    except GarminConnectTooManyRequestsError as e:
        return False, None, f"Rate limit exceeded: {e}"

    except Exception as e:
        return False, None, f"Unexpected error: {e}"


def get_credentials():
    """Get email and password from environment or user input."""
    email = os.getenv("GARMIN_EMAIL") or os.getenv("EMAIL")
    password = os.getenv("GARMIN_PASSWORD") or os.getenv("PASSWORD")

    if not email:
        email = input("Login email: ")
    if not password:
        password = getpass("Enter password: ")

    return email, password


def _resolve_tokenstore_path() -> Path:
    """Project-local default for GARMINTOKENS (same idea as collector.resolve_tokenstore_path)."""
    root = Path(__file__).resolve().parent
    raw = os.getenv("GARMINTOKENS")
    if not raw:
        return root / ".garmin-tokens"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    return p


def init_api() -> Garmin | None:
    """Initialize Garmin API with authentication and token management."""

    load_dotenv(Path(__file__).resolve().parent / ".env")
    tokenstore_path = _resolve_tokenstore_path()

    print(f"🔐 Token storage: {tokenstore_path}")

    if tokenstore_path.exists():
        print("📄 Found existing token directory")
        token_files = list(tokenstore_path.glob("*.json"))
        if token_files:
            print(
                f"🔑 Found {len(token_files)} token file(s): {[f.name for f in token_files]}"
            )
        else:
            print("⚠️ Token directory exists but no token files found")
    else:
        print("📭 No existing token directory found")

    try:
        print("🔄 Attempting to use saved authentication tokens...")
        garmin = Garmin()
        garmin.login(str(tokenstore_path))
        print("✅ Successfully logged in using saved tokens!")
        return garmin

    except (
        FileNotFoundError,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
    ):
        print("🔑 No valid tokens found. Requesting fresh login credentials.")

    while True:
        try:
            email, password = get_credentials()

            print("🔐 Logging in with credentials...")
            garmin = Garmin(email=email, password=password)
            garmin.login(str(tokenstore_path))
            tokenstore_path.mkdir(parents=True, exist_ok=True)
            garmin.client.dump(str(tokenstore_path))
            print(f"💾 Session saved to: {tokenstore_path}")
            print("✅ Login successful!")
            return garmin

        except GarminConnectTooManyRequestsError:
            print("❌ Garmin rate-limited credential login (429).")
            print(
                "💡 Run: pip install -r requirements-browser.txt && python -m playwright install chromium"
            )
            print("   then: python scripts/garmin_playwright_login.py --verify")
            return None

        except GarminConnectAuthenticationError:
            print("❌ Authentication failed:")
            print("💡 Please check your username and password and try again")
            continue

        except (GarminConnectConnectionError, requests.exceptions.HTTPError) as err:
            print(f"❌ Connection error: {err}")
            print("💡 Please check your internet connection and try again")
            return None

        except KeyboardInterrupt:
            print("\n👋 Cancelled by user")
            return None


def display_user_info(api: Garmin):
    """Display basic user information with proper error handling."""
    print("\n" + "=" * 60)
    print("👤 User Information")
    print("=" * 60)

    # Get user's full name
    success, full_name, error_msg = safe_api_call(api.get_full_name)
    if success:
        print(f"📝 Name: {full_name}")
    else:
        print(f"📝 Name: ⚠️ {error_msg}")

    # Get user profile number from device info
    success, device_info, error_msg = safe_api_call(api.get_device_last_used)
    if success and device_info and device_info.get("userProfileNumber"):
        user_profile_number = device_info.get("userProfileNumber")
        print(f"🆔 Profile Number: {user_profile_number}")
    else:
        if not success:
            print(f"🆔 Profile Number: ⚠️ {error_msg}")
        else:
            print("🆔 Profile Number: Not available")


def display_daily_stats(api: Garmin):
    """Display today's activity statistics with proper error handling."""
    today = date.today().isoformat()

    print("\n" + "=" * 60)
    print(f"📊 Daily Stats for {today}")
    print("=" * 60)

    # Get user summary (steps, calories, etc.)
    success, summary, error_msg = safe_api_call(api.get_user_summary, today)
    if success and summary:
        steps = summary.get("totalSteps", 0)
        distance = summary.get("totalDistanceMeters", 0) / 1000  # Convert to km
        calories = summary.get("totalKilocalories", 0)
        floors = summary.get("floorsClimbed", 0)

        print(f"👣 Steps: {steps:,}")
        print(f"📏 Distance: {distance:.2f} km")
        print(f"🔥 Calories: {calories}")
        print(f"🏢 Floors: {floors}")

        # Fun motivation based on steps
        if steps < 5000:
            print("🐌 Time to get those legs moving!")
        elif steps > 15000:
            print("🏃‍♂️ You're crushing it today!")
        else:
            print("👍 Nice progress! Keep it up!")
    else:
        if not success:
            print(f"⚠️ Could not fetch daily stats: {error_msg}")
        else:
            print("⚠️ No activity summary available for today")

    # Get hydration data
    success, hydration, error_msg = safe_api_call(api.get_hydration_data, today)
    if success and hydration and hydration.get("valueInML"):
        hydration_ml = int(hydration.get("valueInML", 0))
        hydration_goal = hydration.get("goalInML", 0)
        hydration_cups = round(hydration_ml / 240, 1)  # 240ml = 1 cup

        print(f"💧 Hydration: {hydration_ml}ml ({hydration_cups} cups)")

        if hydration_goal > 0:
            hydration_percent = round((hydration_ml / hydration_goal) * 100)
            print(f"🎯 Goal Progress: {hydration_percent}% of {hydration_goal}ml")
    else:
        if not success:
            print(f"💧 Hydration: ⚠️ {error_msg}")
        else:
            print("💧 Hydration: No data available")


def main():
    """Main example demonstrating basic Garmin Connect API usage."""
    print("🏃‍♂️ Simple Garmin Connect API Example")
    print("=" * 60)

    # Initialize API with authentication (will only prompt for credentials if needed)
    api = init_api()

    if not api:
        print("❌ Failed to initialize API. Exiting.")
        return

    # Display user information
    display_user_info(api)

    # Display daily statistics
    display_daily_stats(api)

    print("\n" + "=" * 60)
    print("✅ Example completed successfully!")
    print("💡 For a comprehensive demo of all API features, run: python demo.py")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🚪 Exiting example. Goodbye! 👋")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
