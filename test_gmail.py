"""
Gmail App Password Diagnostic Tool
===================================
Run this BEFORE trying to send from the main app.
It tells you exactly what is wrong and how to fix it.

Usage:
    python test_gmail.py
"""

import smtplib, json, os, sys

def load_config():
    """Load saved config from fm_mappings.json"""
    try:
        with open("fm_mappings.json", "r") as f:
            data = json.load(f)
        return data.get("config", {})
    except FileNotFoundError:
        return {}

def test_gmail(gmail_address, app_password):
    """
    Run 4 checks:
      1. Password format (spaces stripped → must be exactly 16 chars)
      2. SMTP connection to Gmail
      3. STARTTLS upgrade
      4. LOGIN authentication
    """
    print()
    print("=" * 56)
    print("  Gmail App Password Diagnostic")
    print("=" * 56)
    print(f"  Gmail : {gmail_address}")
    print(f"  Stored: {repr(app_password)}")

    # ── Check 1: password format ──────────────────────────────
    clean = app_password.replace(" ", "")
    print()
    print(f"[1] Password after removing spaces: {repr(clean)}")
    print(f"    Character count: {len(clean)}")
    if len(clean) != 16:
        print(f"    ✗ WRONG LENGTH — Google App Passwords are always 16 characters.")
        print(f"      You have {len(clean)}. Check you copied the full password.")
        print(f"      Example correct password: fytexhclhuangsxn  (16 chars)")
        return
    else:
        print(f"    ✓ Length OK (16 chars)")

    # ── Check 2: SMTP connection ──────────────────────────────
    print()
    print("[2] Connecting to smtp.gmail.com:587 ...")
    try:
        s = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        print("    ✓ Connected OK")
    except Exception as e:
        print(f"    ✗ Connection failed: {e}")
        print("      → Check your internet / firewall / VPN.")
        return

    # ── Check 3: STARTTLS ─────────────────────────────────────
    print()
    print("[3] Upgrading to TLS (STARTTLS) ...")
    try:
        s.ehlo()
        s.starttls()
        s.ehlo()
        print("    ✓ TLS OK")
    except Exception as e:
        print(f"    ✗ TLS failed: {e}")
        s.quit()
        return

    # ── Check 4: LOGIN ────────────────────────────────────────
    print()
    print("[4] Logging in ...")
    try:
        s.login(gmail_address, clean)
        print("    ✓ LOGIN SUCCESSFUL — App Password is correct!")
        print()
        print("  ✅ Everything is working. The main app should send emails fine.")
        s.quit()
    except smtplib.SMTPAuthenticationError as e:
        print(f"    ✗ Authentication failed — error code {e.smtp_code}")
        print()
        print("  REASON & FIX:")
        print()
        print("  The most common cause is that 2-Step Verification is not")
        print("  enabled on your Google account. App Passwords ONLY work")
        print("  when 2-Step Verification is ON.")
        print()
        print("  Steps to fix:")
        print("  1. Go to  https://myaccount.google.com/security")
        print("  2. Under 'How you sign in to Google' enable '2-Step Verification'")
        print("  3. Then go to  https://myaccount.google.com/apppasswords")
        print("  4. Create a NEW App Password → select 'Mail' and 'Windows Computer'")
        print("  5. Copy the 16-character password (e.g. fyte xhcl huan gsxn)")
        print("  6. Paste it in the app:  Mapping tab → Gmail Config → App password")
        print("  7. Click 'Save all mappings & config'")
        print()
        print("  Other possible causes:")
        print("  • 'Less secure app access' is blocked (normal — use App Password)")
        print("  • The App Password was revoked — create a new one")
        print("  • Wrong Gmail address typed in the config")
        s.quit()
    except Exception as e:
        print(f"    ✗ Unexpected error: {e}")
        s.quit()

if __name__ == "__main__":
    cfg = load_config()

    # Allow overriding from command line: python test_gmail.py email@gmail.com password
    if len(sys.argv) == 3:
        gmail    = sys.argv[1]
        password = sys.argv[2]
    elif cfg.get("gmail") and cfg.get("password"):
        gmail    = cfg["gmail"]
        password = cfg["password"]
        print("(Using credentials from fm_mappings.json)")
    else:
        print("No credentials found in fm_mappings.json.")
        gmail    = input("Enter Gmail address: ").strip()
        password = input("Enter App Password : ").strip()

    test_gmail(gmail, password)
