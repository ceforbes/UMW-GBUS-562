"""Hartwell Foods HR email agent.

Prepares draft replies to employee emails with Claude (claude-sonnet-5) and
tool use. It never sends anything: it only creates drafts for a person on the
HR team to review.

Two ways to run it:
  --email PATH   one email file (.eml); the draft is written to week-04/drafts/.
  --inbox        signs in to the Gmail sandbox over IMAP and handles each unread
                 email in INBOX, one at a time, as its own separate agent run.
                 Each email is marked as read before work starts, so no email is
                 handled twice. The draft is created in Gmail's Drafts folder as
                 a reply to the sender in the same thread; if Claude says it
                 needs human review, the subject starts with [NEEDS HR REVIEW]
                 and the thread gets the Gmail label "Needs HR Review".
                 This version only reads mail and creates drafts. There is no
                 code that sends email.

For each email the agent
  1. reads the sender's address from the email's From: header,
  2. starts week-04/hr_mcp_server.py with --as-employee <sender>, so the
     get_leave_balance tool can return only the sender's own balance,
  3. offers Claude that server's tools (search_policy, get_leave_balance) plus
     one local tool, save_reply, which creates the draft,
  4. loops until Claude stops asking for tools or 10 tool calls have run.

The system prompt is read from week-04/hartwell-hr/system_prompt.txt on every
run, so edits take effect without changing code.

API key: taken from the ANTHROPIC_API_KEY environment variable if set,
otherwise read from API_Key.txt (or API-Key.txt) in the folder above the
repository. Gmail login: read from Agent-Email.txt in that same folder (see
load_inbox.py for the format). Both are only held in memory, never written or
printed.

Usage (from the top of the repository)
  .venv/bin/python week-04/agent.py --email week-04/hartwell-hr/sample-emails/01-dana-vacation.eml
  .venv/bin/python week-04/agent.py --inbox
  .venv/bin/python week-04/agent.py --inbox --save week-04/runs/inbox-run.txt
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import imaplib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Callable, TextIO

import anthropic
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from load_inbox import IMAP_HOST, IMAP_PORT, LOGIN_FILE, load_login

HERE = Path(__file__).resolve().parent              # week-04/
REPO = HERE.parent                                  # UMW-GBUS-562/
SERVER_SCRIPT = HERE / "hr_mcp_server.py"
SYSTEM_PROMPT_FILE = HERE / "hartwell-hr" / "system_prompt.txt"
DRAFTS_DIR = HERE / "drafts"
KEY_FILES = [REPO.parent / "API_Key.txt", REPO.parent / "API-Key.txt"]

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_TOOL_CALLS = 10
PREVIEW_CHARS = 300

INBOX = "INBOX"
REVIEW_TAG = "[NEEDS HR REVIEW]"
# Gmail label put on the thread of a flagged email (--inbox mode), so the
# review status shows in Gmail's conversation list.
REVIEW_LABEL = "Needs HR Review"

# Where the MCP server's own log output (its stderr) goes. Replaced by a pipe
# when --save is used, so those lines are saved too.
SERVER_ERRLOG: TextIO | None = None

SAVE_REPLY_TOOL = {
    "name": "save_reply",
    "description": (
        "Save your finished reply to the employee as a draft for the HR team to "
        "review. This only saves the draft; it never sends anything. Call it "
        "exactly once, when the reply is complete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The full text of the reply email to the employee.",
            },
            "needs_human_review": {
                "type": "boolean",
                "description": (
                    "true if a person from HR must review or follow up before "
                    "anything is sent (sensitive topic, policy gap, missing "
                    "record); false otherwise."
                ),
            },
        },
        "required": ["reply_text", "needs_human_review"],
        "additionalProperties": False,
    },
    "strict": True,
}

# A saver turns Claude's save_reply input into a draft and returns
# (message for Claude, text to show at the end of the run).
Saver = Callable[[str, bool], tuple[str, str]]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    for path in KEY_FILES:
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
    sys.exit("No API key: set ANTHROPIC_API_KEY or put the key in "
             f"{KEY_FILES[0]}.")


def parse_email(raw: bytes, source: str) -> dict:
    """Parse a raw email. Raises ValueError if it has no usable From: address."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(msg.get("From", "")))[1].strip().lower()
    if "@" not in sender:
        raise ValueError(f"could not read a sender address from the From: line of {source}.")
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    return {
        "source": source,
        "from": str(msg.get("From", "")),
        "sender": sender,
        "reply_to": str(msg.get("Reply-To", "") or ""),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "") or ""),
        "message_id": str(msg.get("Message-ID", "") or "").strip(),
        "references": " ".join(str(msg.get("References", "") or "").split()),
        "body": body.strip(),
    }


def read_email(path: Path) -> dict:
    try:
        return parse_email(path.read_bytes(), str(path))
    except ValueError as exc:
        sys.exit(str(exc).capitalize())


def email_prompt(email: dict) -> str:
    return (
        "Prepare a reply to this employee email.\n\n"
        "<email>\n"
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n\n"
        f"{email['body']}\n"
        "</email>"
    )


def reply_subject(subject: str, needs_human_review: bool) -> str:
    subject = subject.strip() or "(no subject)"
    if not re.match(r"(?i)re\s*:", subject):
        subject = "Re: " + subject
    return f"{REVIEW_TAG} {subject}" if needs_human_review else subject


# --------------------------------------------------------------------------
# Gmail (IMAP): read mail, mark it read, create drafts. Nothing here sends.
# --------------------------------------------------------------------------

class MailboxError(Exception):
    pass


class GmailMailbox:
    """IMAP connection to the Gmail sandbox. Reads mail and creates drafts only."""

    def __init__(self) -> None:
        self.address, self._password = load_login(LOGIN_FILE)
        self.imap: imaplib.IMAP4_SSL | None = None
        self.drafts_mailbox: str | None = None

    def __repr__(self) -> str:                  # keep the password out of any repr
        return "GmailMailbox()"

    def connect(self) -> None:
        self.close()
        try:
            self.imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=60)
        except OSError as exc:
            raise MailboxError(f"could not connect to {IMAP_HOST}: {exc}") from None
        try:
            self.imap.login(self.address, self._password)
        except imaplib.IMAP4.error:
            raise MailboxError(
                "Gmail sign-in failed. Check the login in Agent-Email.txt; Gmail needs "
                "IMAP access and an app password, not the account password.") from None
        if self.drafts_mailbox is None:
            self.drafts_mailbox = self._find_drafts_mailbox()
        self._check(self.imap.select(INBOX), f"opening {INBOX}")

    def close(self) -> None:
        if self.imap is not None:
            with contextlib.suppress(Exception):
                self.imap.logout()
            self.imap = None

    @staticmethod
    def _check(result, what: str):
        typ, data = result
        if typ != "OK":
            raise MailboxError(f"{what} failed: {typ} {data}")
        return data

    def _run(self, what: str, command: Callable[[], tuple]):
        """Run one IMAP command; if the connection dropped, reconnect once and retry."""
        try:
            return self._check(command(), what)
        except (imaplib.IMAP4.abort, OSError):
            self.connect()
            return self._check(command(), what)

    def _find_drafts_mailbox(self) -> str:
        """Find Gmail's Drafts folder by its \\Drafts flag (its name varies by language)."""
        data = self._check(self.imap.list(), "listing mailboxes")
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if m and b"\\drafts" in m.group("flags").lower():
                name = m.group("name").decode("utf-8", "replace")
                if name.startswith('"') and name.endswith('"'):
                    name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                return name
        return "[Gmail]/Drafts"

    def unread_uids(self) -> list[str]:
        data = self._run("searching for unread mail",
                         lambda: self.imap.uid("SEARCH", None, "UNSEEN"))
        return [u.decode() for u in (data[0] or b"").split()]

    def fetch_if_unread(self, uid: str) -> bytes | None:
        """Return the raw message without marking it read, or None if it is
        already read or no longer in the inbox."""
        data = self._run(f"fetching message {uid}",
                         lambda: self.imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])"))
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                flags = imaplib.ParseFlags(item[0])
                if b"\\Seen" in flags:
                    return None
                return item[1]
        return None

    def mark_read(self, uid: str) -> None:
        self._run(f"marking message {uid} as read",
                  lambda: self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)"))

    def create_draft(self, message: EmailMessage) -> None:
        raw = message.as_bytes(policy=policy.SMTP)
        stamp = imaplib.Time2Internaldate(time.time())
        self._run("creating the draft",
                  lambda: self.imap.append(self.drafts_mailbox, "(\\Draft \\Seen)", stamp, raw))

    def add_label(self, uid: str, label: str) -> None:
        """Put a Gmail label on an INBOX message (Gmail's X-GM-LABELS IMAP
        extension). Gmail shows a message's labels on its whole thread, and
        creates the label if it does not exist yet."""
        quoted = '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
        self._run(f"labeling message {uid} '{label}'",
                  lambda: self.imap.uid("STORE", uid, "+X-GM-LABELS", f"({quoted})"))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def mcp_tool_to_claude(tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


def mcp_result_text(result) -> tuple[str, bool]:
    """Flatten an MCP CallToolResult to text for a Claude tool_result."""
    parts = []
    for block in result.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")
    text = "\n".join(parts)
    if not text and getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content)
    return text, bool(getattr(result, "is_error", False))


def save_reply_to_file(email: dict, email_path: Path, reply_text: str,
                       needs_human_review: bool) -> tuple[str, str]:
    """--email mode: write the draft to week-04/drafts/. Saves only; never sends."""
    DRAFTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DRAFTS_DIR / f"{stamp}-{email_path.stem}.txt"
    path.write_text(
        "DRAFT - NOT SENT\n"
        f"To: {email['from']}\n"
        f"Subject: {reply_subject(email['subject'], needs_human_review)}\n"
        f"Needs human review: {'YES' if needs_human_review else 'no'}\n"
        f"Source email: {email_path.name}\n"
        f"Created: {datetime.now().isoformat(timespec='seconds')}\n"
        "\n"
        f"{reply_text.strip()}\n",
        encoding="utf-8",
    )
    where = path.relative_to(REPO)
    return (f"Draft saved to {where} (not sent).",
            f"Draft ({where}):\n\n" + path.read_text(encoding="utf-8"))


def build_reply_draft(email: dict, from_address: str, reply_text: str,
                      needs_human_review: bool) -> EmailMessage:
    """A reply to the sender, threaded with the original via In-Reply-To/References."""
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = email["reply_to"] or email["from"]
    msg["Subject"] = reply_subject(email["subject"], needs_human_review)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[-1])
    if email["message_id"]:
        msg["In-Reply-To"] = email["message_id"]
        msg["References"] = f"{email['references']} {email['message_id']}".strip()
    quoted = "\n".join("> " + line if line else ">" for line in email["body"].splitlines())
    on_line = f"On {email['date']}, {email['from']} wrote:" if email["date"] \
        else f"{email['from']} wrote:"
    msg.set_content(f"{reply_text.strip()}\n\n{on_line}\n{quoted}\n")
    return msg


def save_reply_to_gmail(mailbox: GmailMailbox, uid: str, email: dict, reply_text: str,
                        needs_human_review: bool) -> tuple[str, str]:
    """--inbox mode: create a Gmail draft replying to the sender in the same
    thread, and label the thread REVIEW_LABEL if it needs human review.
    Creates a draft only; never sends."""
    draft = build_reply_draft(email, mailbox.address, reply_text, needs_human_review)
    mailbox.create_draft(draft)
    label_note = ""
    if needs_human_review:
        try:
            mailbox.add_label(uid, REVIEW_LABEL)
            label_note = f"Gmail label: {REVIEW_LABEL} (added to the thread)\n"
        except MailboxError as exc:          # the draft exists; don't fail over the label
            label_note = f"Gmail label: NOT added ({exc})\n"
    shown = (
        f"Gmail draft (in {mailbox.drafts_mailbox}, NOT SENT):\n\n"
        f"To: {draft['To']}\n"
        f"Subject: {draft['Subject']}\n"
        f"Needs human review: {'YES' if needs_human_review else 'no'}\n"
        f"{label_note}"
        f"In reply to: {email['message_id'] or '(original had no Message-ID)'}\n"
        "\n"
        f"{reply_text.strip()}\n"
    )
    return f"Draft created in Gmail Drafts as a reply to {email['sender']} (not sent).", shown


def preview(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:PREVIEW_CHARS] + ("..." if len(text) > PREVIEW_CHARS else "")


# --------------------------------------------------------------------------
# Agent loop (one email per run)
# --------------------------------------------------------------------------

async def run_agent(email: dict, client: anthropic.AsyncAnthropic, saver: Saver) -> dict:
    """Run the agent on one email. Returns stats for the run."""
    system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()

    print(f"Email:   {email['source']}")
    print(f"Sender:  {email['sender']}")
    print(f"Subject: {email['subject']}")
    print(f"Model:   {MODEL}\n")

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT), "--as-employee", email["sender"]],
        cwd=str(REPO),
    )

    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    draft_shown: str | None = None
    needs_review = False
    api_error = False
    stop_reason_text = ""

    async with stdio_client(server, errlog=SERVER_ERRLOG or sys.__stderr__) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            mcp_names = {t.name for t in mcp_tools}
            tools = [mcp_tool_to_claude(t) for t in mcp_tools] + [SAVE_REPLY_TOOL]
            print("Tools offered: " + ", ".join(t["name"] for t in tools) + "\n")

            messages = [{"role": "user", "content": email_prompt(email)}]

            while True:
                try:
                    response = await client.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        thinking={"type": "adaptive"},
                        tools=tools,
                        messages=messages,
                    )
                except anthropic.AuthenticationError:
                    stop_reason_text, api_error = "the API key was rejected (authentication error).", True
                    break
                except anthropic.RateLimitError:
                    stop_reason_text, api_error = "the API rate limit was hit; try again shortly.", True
                    break
                except anthropic.APIStatusError as exc:
                    stop_reason_text, api_error = f"the API returned an error ({exc.status_code}).", True
                    break
                except anthropic.APIConnectionError:
                    stop_reason_text, api_error = "could not connect to the Claude API.", True
                    break

                usage = response.usage
                input_tokens += (usage.input_tokens
                                 + (usage.cache_creation_input_tokens or 0)
                                 + (usage.cache_read_input_tokens or 0))
                output_tokens += usage.output_tokens

                if response.stop_reason != "tool_use":
                    reasons = {
                        "end_turn": "Claude finished and sent a reply with no tool request.",
                        "max_tokens": f"Claude's reply hit the {MAX_TOKENS}-token limit.",
                        "refusal": "Claude declined the request (refusal).",
                        "stop_sequence": "Claude's reply hit a stop sequence.",
                        "pause_turn": "Claude paused its turn without a tool request.",
                    }
                    stop_reason_text = reasons.get(
                        response.stop_reason,
                        f"Claude's reply was not a tool request (stop reason: {response.stop_reason}).")
                    final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                    if final_text:
                        print(f"Claude's final message: {preview(final_text)}\n")
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                limit_hit = False

                for block in (b for b in response.content if b.type == "tool_use"):
                    if tool_calls >= MAX_TOOL_CALLS:
                        limit_hit = True
                        break
                    tool_calls += 1
                    print(f"Step {tool_calls}: {block.name}")
                    print(f"  input:  {json.dumps(block.input, ensure_ascii=False)}")

                    is_error = False
                    if block.name == "save_reply":
                        if draft_shown is not None:
                            text, is_error = ("A draft was already saved for this email; "
                                              "save_reply can be called only once.", True)
                        else:
                            reply = block.input.get("reply_text", "")
                            review = block.input.get("needs_human_review")
                            if not isinstance(reply, str) or not reply.strip() or not isinstance(review, bool):
                                text, is_error = ("save_reply needs non-empty reply_text and "
                                                  "needs_human_review set to true or false.", True)
                            else:
                                try:
                                    text, draft_shown = saver(reply, review)
                                    needs_review = review
                                except (MailboxError, OSError, ValueError) as exc:
                                    text, is_error = f"The draft could not be saved: {exc}", True
                    elif block.name in mcp_names:
                        try:
                            result = await session.call_tool(block.name, block.input)
                            text, is_error = mcp_result_text(result)
                        except Exception as exc:          # server or transport failure
                            text, is_error = f"The tool failed: {exc}", True
                    else:
                        text, is_error = f"Unknown tool: {block.name}", True

                    print(f"  result{' (error)' if is_error else ''}: {preview(text)}\n")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    })

                if limit_hit or tool_calls >= MAX_TOOL_CALLS:
                    stop_reason_text = f"reached the limit of {MAX_TOOL_CALLS} tool calls."
                    break
                messages.append({"role": "user", "content": tool_results})

    print(f"Stopped: {stop_reason_text}\n")
    print("=" * 70)
    print(draft_shown if draft_shown else "No draft was saved.")
    print("=" * 70)
    print(f"Tool calls:    {tool_calls}")
    print(f"Input tokens:  {input_tokens:,}")
    print(f"Output tokens: {output_tokens:,}")
    return {
        "draft": draft_shown is not None,
        "needs_review": needs_review,
        "api_error": api_error,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


async def run_file(email_path: Path) -> None:
    email = read_email(email_path)
    client = anthropic.AsyncAnthropic(api_key=load_api_key())
    await run_agent(email, client,
                    lambda reply, review: save_reply_to_file(email, email_path, reply, review))


async def run_inbox() -> None:
    client = anthropic.AsyncAnthropic(api_key=load_api_key())
    mailbox = GmailMailbox()
    print(f"Signing in to {IMAP_HOST} ...")
    try:
        mailbox.connect()
        uids = mailbox.unread_uids()
    except MailboxError as exc:
        mailbox.close()
        sys.exit(f"Gmail: {exc}")
    print(f"Signed in. Unread emails in {INBOX}: {len(uids)}. "
          f"Drafts go to: {mailbox.drafts_mailbox}\n")

    handled = drafts = flagged = 0
    total_in = total_out = 0
    try:
        for n, uid in enumerate(uids, 1):
            print("#" * 70)
            print(f"Inbox email {n} of {len(uids)} (UID {uid})")
            print("#" * 70)
            raw = mailbox.fetch_if_unread(uid)
            if raw is None:
                print("Already read or no longer in the inbox; skipped.\n")
                continue
            mailbox.mark_read(uid)                 # before any work, so it is never handled twice
            print("Marked as read.\n")
            handled += 1

            try:
                email = parse_email(raw, f"{INBOX} message UID {uid}")
            except ValueError as exc:
                print(f"Skipped: {exc}\n")
                continue

            try:
                stats = await run_agent(
                    email, client,
                    lambda reply, review, email=email, uid=uid:
                        save_reply_to_gmail(mailbox, uid, email, reply, review))
            except Exception as exc:               # one bad run must not stop the rest
                print(f"The agent run failed: {type(exc).__name__}: {exc}")
                print("This email stays marked as read; mark it unread in Gmail to try again.\n")
                continue

            drafts += stats["draft"]
            flagged += stats["draft"] and stats["needs_review"]
            total_in += stats["input_tokens"]
            total_out += stats["output_tokens"]
            print()
            if stats["api_error"]:
                print("Stopping: the Claude API failed, so the remaining emails are left unread.")
                print(f"UID {uid} is marked as read; mark it unread in Gmail to try again.\n")
                break
    except MailboxError as exc:
        print(f"Gmail: {exc}\nStopping; emails not yet reached are left unread.")
    finally:
        mailbox.close()

    print("=" * 70)
    print(f"Inbox run finished. Emails handled: {handled}. Drafts created: {drafts} "
          f"({flagged} marked {REVIEW_TAG} and labeled '{REVIEW_LABEL}').")
    print(f"Total input tokens:  {total_in:,}")
    print(f"Total output tokens: {total_out:,}")


# --------------------------------------------------------------------------
# --save: copy everything printed to a UTF-8 text file
# --------------------------------------------------------------------------

class Tee:
    """A text stream that writes to several streams at once."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            for stream in self.streams:
                stream.write(text)
        return len(text)

    def flush(self) -> None:
        with self.lock:
            for stream in self.streams:
                stream.flush()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def output_saved_to(path: Path | None):
    """While active, everything printed (stdout, stderr, and the MCP server's
    log output) also goes to `path`, encoded as UTF-8."""
    global SERVER_ERRLOG
    if path is None:
        yield
        return

    out, err = sys.stdout, sys.stderr
    log = path.open("w", encoding="utf-8")
    sys.stdout, sys.stderr = Tee(out, log), Tee(err, log)

    # The MCP server is a subprocess, so it needs a real file descriptor for
    # its stderr: give it a pipe and copy what arrives to our (teed) stderr.
    read_fd, write_fd = os.pipe()
    SERVER_ERRLOG = os.fdopen(write_fd, "w", encoding="utf-8")
    reader = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")

    def pump() -> None:
        for line in reader:
            sys.stderr.write(line)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    print(f"Saving output to {path}\n")
    try:
        yield
    except SystemExit as exc:                    # save the exit message too
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            raise SystemExit(1) from None
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        raise SystemExit(1) from None
    finally:
        SERVER_ERRLOG.close()
        SERVER_ERRLOG = None
        pump_thread.join(timeout=2)
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout, sys.stderr = out, err
        log.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Draft replies to Hartwell Foods employee emails (creates drafts; never sends).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--email", type=Path, metavar="PATH",
                        help="path of one email file (.eml); the draft is saved in week-04/drafts/")
    source.add_argument("--inbox", action="store_true",
                        help="handle each unread email in the Gmail sandbox inbox, one at a time; "
                             "drafts are created in Gmail")
    parser.add_argument("--save", type=Path, metavar="FILE",
                        help="also write everything printed to FILE (UTF-8 text)")
    args = parser.parse_args()

    if args.email and not args.email.is_file():
        parser.error(f"email file not found: {args.email}")
    if args.save:
        save = args.save.resolve()
        if not save.parent.is_dir():
            parser.error(f"folder for --save does not exist: {args.save.parent}")
        if save in {LOGIN_FILE.resolve(), *(k.resolve() for k in KEY_FILES)}:
            parser.error("--save must not overwrite a login or API key file")
        if save.suffix == ".py" or save == SYSTEM_PROMPT_FILE.resolve():
            parser.error("--save must name a new output file, not a program or prompt file")

    with output_saved_to(args.save):
        if args.inbox:
            asyncio.run(run_inbox())
        else:
            asyncio.run(run_file(args.email.resolve()))


if __name__ == "__main__":
    main()
