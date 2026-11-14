"""Load the Hartwell HR sample emails into a Gmail sandbox inbox.

Signs in to Gmail over IMAP with the login stored in Agent-Email.txt (in the
folder above this repository) and places each .eml file from
week-04/hartwell-hr/sample-emails into INBOX as an unread message dated today.

Nothing is sent: messages are added directly to the mailbox with IMAP APPEND.
The password is only held in memory; it is never printed, logged, or saved.

Agent-Email.txt is normally:
    EMAIL_ADDRESS=your.sandbox.address@gmail.com
    APP_PASSWORD=abcdefghijklmnop
Also accepted: "email: ..." / "password: ..." lines (":" or "=", any case),
or two lines (address, then password), or one line "address password".
Gmail needs an app password (Google Account > Security > App passwords).

Usage (from the repository root):
  .venv/bin/python week-04/load_inbox.py
  .venv/bin/python week-04/load_inbox.py --dry-run   # list what would be loaded
"""

import argparse
import imaplib
import re
import sys
import time
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMAILS_DIR = HERE / "hartwell-hr" / "sample-emails"
LOGIN_FILE = Path("/Users/cef/Desktop/GBUS_562/Agent-Email.txt")
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAILBOX = "INBOX"

# Keys are compared after lowercasing and turning "_" / "-" into spaces,
# so EMAIL_ADDRESS, email-address and "Email address" all match.
USER_KEYS = {"email address", "email", "e mail", "user", "username", "login",
             "address", "account"}
PASS_KEYS = {"app password", "apppassword", "password", "pass"}


def load_login(path: Path) -> tuple[str, str]:
    """Read (address, password) from the login file. Never echoes its contents."""
    if not path.exists():
        sys.exit(f"Login file not found: {path}")
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]

    user = password = None
    for ln in lines:
        m = re.match(r"^([A-Za-z _-]+?)\s*[:=]\s*(.+)$", ln)
        if not m:
            continue
        key = re.sub(r"[_-]+", " ", m.group(1)).strip().lower()
        value = m.group(2).strip().strip("\"'")
        if key in USER_KEYS:
            user = value
        elif key in PASS_KEYS:
            password = value

    if not (user and password):
        if len(lines) >= 2:
            user, password = lines[0], lines[1]
        elif len(lines) == 1 and len(lines[0].split()) >= 2:
            user, password = lines[0].split(maxsplit=1)

    if not (user and password and "@" in user):
        sys.exit(f"Could not find an email address and password in {path.name}. "
                 "Expected 'email: ...' and 'password: ...' lines "
                 "(or the address on one line and the password on the next).")

    # Gmail app passwords are shown as four groups of four letters; drop the spaces.
    squeezed = password.replace(" ", "")
    if re.fullmatch(r"[A-Za-z]{16}", squeezed):
        password = squeezed
    return user.strip(), password


def prepare_message(path: Path) -> tuple[bytes, str]:
    """Return the message bytes with today's Date and a fresh Message-ID."""
    msg = BytesParser(policy=policy.default).parse(path.open("rb"))
    del msg["Date"]
    msg["Date"] = formatdate(localtime=True)
    del msg["Message-ID"]
    msg["Message-ID"] = make_msgid(domain="hartwellfoods.example")
    raw = msg.as_bytes(policy=policy.SMTP)  # CRLF line endings, as IMAP expects
    return raw, str(msg.get("Subject", "(no subject)"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="list the emails that would be loaded, without signing in")
    args = parser.parse_args()

    files = sorted(EMAILS_DIR.glob("*.eml"))
    if not files:
        sys.exit(f"No .eml files found in {EMAILS_DIR}")

    if args.dry_run:
        for f in files:
            _, subject = prepare_message(f)
            print(f"would load: {f.name}  ({subject})")
        return

    user, password = load_login(LOGIN_FILE)
    print(f"Signing in to {IMAP_HOST} ...")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except OSError as e:
        sys.exit(f"Could not connect to {IMAP_HOST}: {e}")

    try:
        try:
            imap.login(user, password)
        except imaplib.IMAP4.error:
            sys.exit("Sign-in failed. Check the login in Agent-Email.txt; Gmail "
                     "requires IMAP access and an app password, not the account password.")
        finally:
            password = None  # drop the reference as soon as it is used

        loaded = 0
        for f in files:
            raw, subject = prepare_message(f)
            # Empty flag list => no \Seen flag, so the message arrives unread.
            # Internal date = now, so Gmail sorts it as received today.
            status, data = imap.append(MAILBOX, "", imaplib.Time2Internaldate(time.time()), raw)
            if status == "OK":
                loaded += 1
                print(f"loaded: {f.name}  ({subject})")
            else:
                print(f"FAILED: {f.name}  ({status} {data})")
        print(f"\n{loaded} of {len(files)} emails placed in {MAILBOX} as unread.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
