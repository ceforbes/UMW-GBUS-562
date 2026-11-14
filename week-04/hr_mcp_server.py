"""Hartwell Foods HR tools, served over MCP (Model Context Protocol).

Tools
  search_policy(query)       The 4 HR-document chunks closest to the query, each
                             with its source file, so the model can cite it.
  get_leave_balance(email)   An employee's vacation, sick, and personal days
                             left, from employees.csv (HR-staff mode).
  get_leave_balance()        With --as-employee ADDRESS the tool takes no inputs
                             and returns only that employee's balance.

The server talks over stdio: an MCP client (Claude Code, or your own agent)
starts it as a subprocess. It never calls Claude and needs no API key.

Search works like week-03/ask.py: every HR document is split into one chunk per
section (long sections are split further), each chunk starts with
"<title> (<version>) > <section>", and the chunks are stored in a Chroma
database (week-04/chroma_db/, git-ignored) using Chroma's built-in embedding
model. The database is rebuilt automatically when the documents change.

Usage (from the top of the repository)
  .venv/bin/python week-04/hr_mcp_server.py                     # serve (HR-staff mode)
  .venv/bin/python week-04/hr_mcp_server.py --as-employee dana.whitfield@hartwellfoods.example
  .venv/bin/python week-04/hr_mcp_server.py --rebuild           # rebuild the database, then exit
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
import sys
from email.utils import parseaddr
from pathlib import Path

from pydantic import BaseModel

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

HERE = Path(__file__).resolve().parent            # week-04/
DATA_DIR = HERE / "hartwell-hr"
DOCS_DIR = DATA_DIR / "hr-docs"
EMPLOYEES_CSV = DATA_DIR / "employees.csv"
DB_DIR = HERE / "chroma_db"
COLLECTION = "hr_documents"

TOP_K = 4
MAX_QUERY_CHARS = 500
# Chroma's built-in embedding model reads only the first 256 word pieces of a
# chunk (about 180 words), so longer sections are split.
MAX_CHUNK_WORDS = 180

# All logging goes to stderr: on a stdio server, stdout carries the protocol.
log = logging.getLogger("hr_mcp_server")


# --------------------------------------------------------------------------
# Chunking (adapted from week-03/ask.py; the HR documents are all HTML)
# --------------------------------------------------------------------------

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_meta(meta: str) -> str:
    """'Version 6.1, current. Owner: Human Resources.' -> 'Version 6.1, current'"""
    parts = []
    match = re.search(r"\bVersion\s+([\w.]+)", meta, re.IGNORECASE)
    if match:
        parts.append(f"Version {match.group(1)}")
    lowered = meta.lower()
    if "superseded" in lowered or "archived" in lowered:
        parts.append("superseded")
    elif "current" in lowered:
        parts.append("current")
    return ", ".join(parts)


def parse_html(path: Path) -> tuple[str, str, list[tuple[str, list[str]]]]:
    """Return (title, version, [(section heading, paragraphs), ...])."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    for tag in soup.select(".bar"):                    # intranet banner
        tag.decompose()

    root = soup.find("main") or soup.body or soup
    h1 = root.find("h1")
    if h1:
        title = clean(h1.get_text())
    elif soup.title:
        title = clean(soup.title.get_text().split("|")[0])
    else:
        title = path.stem.replace("-", " ").title()

    meta_tag = root.find(class_="meta")
    version = parse_meta(clean(meta_tag.get_text())) if meta_tag else ""

    sections: list[tuple[str, list[str]]] = []
    heading, paragraphs = "Introduction", []

    for el in root.find_all(["h2", "h3", "p", "ul", "ol", "table", "pre", "blockquote"]):
        if el is meta_tag or el.find_parent(["ul", "ol", "table", "blockquote"]):
            continue                                   # handled with its parent
        if el.name == "h2":
            if paragraphs:
                sections.append((heading, paragraphs))
            heading, paragraphs = clean(el.get_text()), []
        elif el.name == "h3":
            paragraphs.append(clean(el.get_text()) + ":")
        elif el.name in ("ul", "ol"):
            items = [clean(li.get_text()) for li in el.find_all("li", recursive=False)]
            paragraphs.append("\n".join(f"- {item}" for item in items if item))
        elif el.name == "table":
            rows = [" | ".join(clean(c.get_text()) for c in tr.find_all(["th", "td"]))
                    for tr in el.find_all("tr")]
            paragraphs.append("\n".join(rows))
        else:
            text = clean(el.get_text())
            if text:
                paragraphs.append(text)
    if paragraphs:
        sections.append((heading, paragraphs))
    return title, version, sections


def split_long(paragraphs: list[str]) -> list[list[str]]:
    """Group paragraphs into pieces of at most MAX_CHUNK_WORDS words."""
    pieces, piece, words = [], [], 0
    for para in paragraphs:
        n = len(para.split())
        if piece and words + n > MAX_CHUNK_WORDS:
            pieces.append(piece)
            piece, words = [], 0
        if n > MAX_CHUNK_WORDS:                        # one very long paragraph
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                m = len(sentence.split())
                if piece and words + m > MAX_CHUNK_WORDS:
                    pieces.append(piece)
                    piece, words = [], 0
                piece.append(sentence)
                words += m
            continue
        piece.append(para)
        words += n
    if piece:
        pieces.append(piece)
    return pieces


def chunk_document(path: Path) -> list[dict]:
    title, version, sections = parse_html(path)
    label = f"{title} ({version})" if version else title
    chunks = []
    for heading, paragraphs in sections:
        pieces = split_long(paragraphs)
        for part_no, piece in enumerate(pieces, start=1):
            section = heading
            if len(pieces) > 1:
                section += f" (part {part_no} of {len(pieces)})"
            chunks.append({
                "id": f"{path.name}::{len(chunks) + 1}",
                "text": f"{label} > {section}\n" + "\n".join(piece),
                "metadata": {"source": path.name, "title": title,
                             "section": section, "version": version},
            })
    return chunks


# --------------------------------------------------------------------------
# Chroma database
# --------------------------------------------------------------------------

def document_files() -> list[Path]:
    return sorted(p for p in DOCS_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in (".html", ".htm"))


def docs_fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def chroma_client():
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(path=str(DB_DIR),
                                     settings=Settings(anonymized_telemetry=False))


def build_index(client) -> None:
    files = document_files()
    log.info("Indexing %d HR documents in %s ...", len(files), DOCS_DIR)
    all_chunks = []
    for path in files:
        try:
            chunks = chunk_document(path)
        except Exception as exc:                       # keep going on a bad file
            log.warning("  skipped %s: could not read it (%s)", path.name, exc)
            continue
        log.info("  %s: %d chunks", path.name, len(chunks))
        all_chunks.extend(chunks)

    if COLLECTION in [c.name for c in client.list_collections()]:
        client.delete_collection(COLLECTION)
    collection = client.create_collection(
        COLLECTION,
        configuration={"hnsw": {"space": "cosine"}},   # similarity = 1 - distance
        metadata={"fingerprint": docs_fingerprint(files)},
    )
    if all_chunks:
        collection.add(ids=[c["id"] for c in all_chunks],
                       documents=[c["text"] for c in all_chunks],
                       metadatas=[c["metadata"] for c in all_chunks])
    log.info("Stored %d chunks in %s", len(all_chunks), DB_DIR)


def open_collection(rebuild: bool = False):
    """Open the database, rebuilding it if asked, missing, or out of date."""
    client = chroma_client()
    exists = COLLECTION in [c.name for c in client.list_collections()]
    if exists and not rebuild:
        stored = (client.get_collection(COLLECTION).metadata or {}).get("fingerprint")
        if stored != docs_fingerprint(document_files()):
            log.info("The HR documents have changed since the database was built.")
            rebuild = True
    if rebuild or not exists:
        build_index(client)
    return client.get_collection(COLLECTION)


# --------------------------------------------------------------------------
# Employee records
# --------------------------------------------------------------------------

def normalize_email(value: str) -> str:
    """Accept 'dana@x.example' or 'Dana Whitfield <dana@x.example>'."""
    return parseaddr(value.strip())[1].strip().lower()


def find_employee(email: str) -> dict | None:
    """Read employees.csv fresh on each call, so edits take effect at once."""
    wanted = normalize_email(email)
    if not wanted:
        return None
    with EMPLOYEES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("email") or "").strip().lower() == wanted:
                return row
    return None


# --------------------------------------------------------------------------
# Tool results
# --------------------------------------------------------------------------

class PolicyChunk(BaseModel):
    rank: int
    source: str          # file name to cite, e.g. leave-policy.html
    title: str
    section: str
    similarity: float
    text: str


class PolicySearchResult(BaseModel):
    query: str
    results: list[PolicyChunk]


class LeaveBalance(BaseModel):
    email: str
    name: str
    department: str
    vacation_days_left: int
    sick_days_left: int
    personal_days_left: int


NO_RECORD_ADVICE = (
    "Do not state, estimate, or guess any leave balance in your reply. Say that "
    "HR will confirm the balance, and flag the reply for human review."
)


def balance_from_row(row: dict) -> LeaveBalance:
    try:
        return LeaveBalance(
            email=row["email"].strip().lower(),
            name=row["name"].strip(),
            department=row["department"].strip(),
            vacation_days_left=int(row["vacation_days_left"]),
            sick_days_left=int(row["sick_days_left"]),
            personal_days_left=int(row["personal_days_left"]),
        )
    except (KeyError, ValueError, AttributeError) as exc:
        raise ToolError("The employee record is incomplete or malformed. "
                        + NO_RECORD_ADVICE) from exc


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

def create_server(collection, as_employee: str | None) -> MCPServer:
    mode = (f"Employee mode: get_leave_balance returns only the balance of the "
            f"employee this server was started for." if as_employee else
            "HR-staff mode: get_leave_balance looks up any employee by work email.")
    server = MCPServer(
        name="hartwell-hr",
        instructions=("Hartwell Foods HR tools. search_policy searches the HR policy "
                      "documents; cite the source file of each fact you use. "
                      "get_leave_balance returns days left from the employee records. "
                      + mode),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    def search_policy(query: str) -> PolicySearchResult:
        """Search the Hartwell Foods HR policy documents.

        Returns the 4 passages closest in meaning to the query. Each has the
        source file to cite (for example leave-policy.html), the document title
        and section, and a similarity score from 0 to 1. Only state policy that
        appears in the returned text; if none of it answers the question, say
        the policies don't cover it.
        """
        query = clean(query)[:MAX_QUERY_CHARS]
        if not query:
            raise ToolError("The search query is empty. Pass a few words describing "
                            "the policy question.")
        found = collection.query(query_texts=[query], n_results=TOP_K,
                                 include=["documents", "metadatas", "distances"])
        results = [
            PolicyChunk(rank=rank, source=meta["source"], title=meta["title"],
                        section=meta["section"], similarity=round(1 - dist, 3), text=text)
            for rank, (text, meta, dist) in enumerate(
                zip(found["documents"][0], found["metadatas"][0], found["distances"][0]),
                start=1)
        ]
        return PolicySearchResult(query=query, results=results)

    if as_employee:
        def get_leave_balance() -> LeaveBalance:
            """Get the leave balance of the employee you are helping.

            Takes no inputs: this server is set up for one employee and can
            return only their vacation, sick, and personal days left. It cannot
            look up anyone else.
            """
            row = find_employee(as_employee)
            if row is None:
                raise ToolError("No employee record matches the address this server "
                                "was started for. " + NO_RECORD_ADVICE)
            return balance_from_row(row)
    else:
        def get_leave_balance(email: str) -> LeaveBalance:
            """Look up an employee's leave balance by work email (for HR staff).

            Returns their vacation, sick, and personal days left from the
            employee records. Use the exact work email address, for example
            dana.whitfield@hartwellfoods.example.
            """
            row = find_employee(email)
            if row is None:
                shown = clean(email)[:120]
                raise ToolError(f"No employee record matches the email {shown!r}. "
                                + NO_RECORD_ADVICE)
            return balance_from_row(row)

    server.add_tool(get_leave_balance, annotations=read_only)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP server with Hartwell Foods HR tools (search_policy, get_leave_balance).")
    parser.add_argument("--as-employee", metavar="EMAIL",
                        help="serve one employee: get_leave_balance takes no inputs and "
                             "returns only this employee's balance")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the Chroma database from the HR documents, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    as_employee = None
    if args.as_employee is not None:
        as_employee = normalize_email(args.as_employee)
        if "@" not in as_employee:
            parser.error(f"--as-employee needs an email address, got {args.as_employee!r}")
        if find_employee(as_employee) is None:
            log.warning("Warning: no employee record matches %s; get_leave_balance "
                        "will return an error.", as_employee)

    # Build or open the database before serving, so the first search is fast
    # (the first run also downloads Chroma's embedding model, about 80 MB).
    collection = open_collection(rebuild=args.rebuild)
    if args.rebuild:
        return

    create_server(collection, as_employee).run("stdio")


if __name__ == "__main__":
    main()
