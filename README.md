# UMW-GBUS-562
Repository for GBUS 562 Coursework (Chris Forbes)

## About
This repository contains materials created for GBUS 562: AI Language Models at the University of Mary Washington (UMW). The content is organized into weekly folders, one for each week of the course.

## Weekly Work
1. week-01
   - [Connect 4](week-01/connect4.html): A single-page browser version of Connect 4. Play against a computer opponent with Easy, Medium, or Hard difficulty, or switch to 2 Players to take turns with a friend on the same device. Open the HTML file in any browser to play.
2. week-02
   - [Prompt Lab](week-02/prompt-lab.html): A web app for testing prompts against Claude, built with Claude Code.
   - [Writeup](week-02/writeup.md): What I built, why I chose each prompting technique, an example run, and my reflection. Run logs for the four tasks are also in the folder.
3. week-03
   - [RAG tool](week-03/ask.py): a RAG tool that answers questions from a folder of documents (Copperline Manufacturing's finance documents by default, or any folder with `--docs`, such as Larkfield Credit Union's member services documents), built with Claude Code.
   - [Writeup](week-03/writeup.md): What I built, why I chose this chunking approach, an example run, and my reflection. Saved runs for the test questions are in [week-03/runs](week-03/runs).
4. week-04
   - [HR email agent](week-04/agent.py): an AI agent that prepares replies to HR emails using tools from an MCP server, built with Claude Code. Run it on one email file (`--email`), or with `--inbox` to handle each unread email in a Gmail sandbox, creating a threaded Gmail draft for each (when flagged, the subject starts with [NEEDS HR REVIEW] and the thread gets the Gmail label "Needs HR Review"). It only creates drafts; it never sends. `--save FILE` also writes the run's output to a text file.
   - [Inbox loader](week-04/load_inbox.py): places the sample HR emails into a Gmail sandbox inbox over IMAP as unread messages dated today (nothing is sent).
