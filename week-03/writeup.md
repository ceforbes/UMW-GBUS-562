## What I Built

I built a retrieval-augmented generation (RAG) tool, `ask.py`, that answers questions from a folder of documents. It first worked only with Copperline Manufacturing's finance documents. I then extended it to work with any folder and tested it on Larkfield Credit Union's member services documents. The tool splits every document into chunks and stores them in a Chroma vector database. For each question it retrieves the four most similar chunks and sends them to Claude with the question. Claude answers only from those chunks and cites the source file for each fact. The `--docs` option picks the document folder, and each folder gets its own Chroma database. The `--save` option writes the complete run to a text file: the retrieved chunks with their rank, similarity score, and source file, then a line containing only ANSWER, then Claude's answer. The run files for my seven test questions are in the `runs/` folder.

## Why This Approach

Chunking method: Structure. I split each document by section, one chunk per heading, because each section of these policy documents covers one topic, such as overdraft fees, wire transfers, or who can approve a refund. Splitting at headings keeps a rule and its conditions together instead of cutting them off partway through.

Makes sense on its own: Yes. Each chunk starts with the document title, version, and section heading, for example "Consumer Fee Schedule (Version 8, current) > Overdraft and returned item fees". A chunk that says only "the fee is $29 per item" is meaningless by itself, but with the title and heading attached it is clear which fee, which document, and which version it comes from.

Label and source: Both. Every chunk records the file it came from, and that file name is shown next to each retrieved chunk and passed to Claude. Claude is told to cite the file in square brackets after each fact, so I can check every answer against the document it came from.

Short enough: Yes. Chroma's built-in embedding model only reads about the first 180 words of a chunk and ignores the rest, so any section longer than 180 words is split into numbered parts. The Larkfield sections are all short (33 to 119 words, about 59 on average), so none of them had to be split.

## Example Run

[1] similarity 0.712  source consumer-fee-schedule.html
Consumer Fee Schedule (Version 8, current) > Overdraft and returned item fees
When Larkfield pays a check, debit card purchase, or automatic payment that overdraws a member's checking account, the overdraft fee is $29 per item paid. When Larkfield returns an item unpaid because the account does not have enough money, the returned item fee is $29 per item returned. No more than three overdraft or returned item fees are charged to one account in a single business day.
No overdraft fee is charged if the account is overdrawn by $10 or less at the end of the business day.
Members can link a savings account for overdraft transfers. An overdraft transfer from a linked savings account is free.

[2] similarity 0.702  source consumer-fee-schedule-v7.html
Consumer Fee Schedule (Version 7, current) > Overdraft and returned item fees
When Larkfield pays a check, debit card purchase, or automatic payment that overdraws a member's checking account, the overdraft fee is $35 per item paid. When Larkfield returns an item unpaid because the account does not have enough money, the returned item fee is $35 per item returned. No more than four overdraft or returned item fees are charged to one account in a single business day.
Members can link a savings account for overdraft transfers. An overdraft transfer from a linked savings account is free.

[3] similarity 0.646  source consumer-fee-schedule.html
Consumer Fee Schedule (Version 8, current) > Checking account monthly fees
Everyday Checking has no monthly service fee. Plus Checking has a monthly service fee of $8. The Plus Checking fee is waived for any month in which the account keeps a minimum daily balance of $1,000, or receives direct deposits totaling $500 or more.

[4] similarity 0.643  source consumer-fee-schedule-v7.html
Consumer Fee Schedule (Version 7, current) > Checking account monthly fees
Everyday Checking has no monthly service fee. Plus Checking has a monthly service fee of $8. The Plus Checking fee is waived for any month in which the account keeps a minimum daily balance of $1,000, or receives direct deposits totaling $500 or more.

ANSWER
The sources show conflicting figures, both labeled "current":

- **Consumer Fee Schedule Version 8** states the overdraft fee is **$29 per item paid** (and $29 per returned item), with a maximum of three such fees per account per business day, and no fee if the account is overdrawn by $10 or less at day's end [consumer-fee-schedule.html].
- **Consumer Fee Schedule Version 7** states the overdraft fee is **$35 per item paid**, with a maximum of four such fees per account per business day [consumer-fee-schedule-v7.html].

Both versions are labeled "current," so the documents do not clarify which figure actually applies — you may want to confirm which version supersedes the other.

## Reflection

What was wrong: The Larkfield folder contained two versions of the Consumer Fee Schedule, Version 7 and Version 8, and both were labeled "current". When I asked about the overdraft fee, the overdraft sections from both versions were retrieved with almost the same similarity score (0.712 and 0.702). Claude reported both a $29 fee and a $35 fee and said it could not tell which one applied (`runs/q1-before.txt`). Version 8 says it replaces Version 7, but that line was not included in the chunks, so Claude never saw it.

How I fixed it: I removed the outdated Version 7 file from the document folder, renamed the current file to `consumer-fee-schedule-v8.html`, and rebuilt the Larkfield database. Asking the same question again returned only Version 8 chunks, and Claude gave one clear answer of $29 per item (`runs/q1-after.txt`).

Prevention: Only current documents should go in the folder the tool searches, and superseded versions should be moved to an archive folder before the database is built, as I did with the Copperline documents. The tool could also do more to catch this: include the "Replaces Version 7" note in each chunk so Claude can tell which version is in effect, or warn when two documents have the same title and are both marked current.

Question 6: I asked for the current rate on a 30-year fixed mortgage, which none of the Larkfield documents cover. The search still returned four chunks, because it always does, but the scores were very low (0.253 at most, compared with 0.53 to 0.71 for the other questions). One of them listed share certificate rates. Claude correctly said the documents do not cover mortgage rates and did not offer the certificate rates as an answer. This showed me that the instruction to answer only from the sources and say when they don't cover the question is just as important as retrieving the right chunks.
