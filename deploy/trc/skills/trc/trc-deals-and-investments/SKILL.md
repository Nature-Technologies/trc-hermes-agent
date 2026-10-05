---
name: trc-deals-and-investments
description: "TRC deals: new investments, follow-ons, commitments"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, Deals, Investments]
    related_skills: [trc-performance-and-profit, trc-aum-and-portfolio-value]
---

# TRC deals and investments

For: "the last / latest / first investment", "what did we invest in during 2024",
"how many deals", "our biggest commitment", new deals versus follow-ons.

## Settle the meaning first
- "investment": a NEW deal, a FOLLOW-ON into an existing holding, a COMMITMENT (the
  amount pledged), or a HOLDING still on the books today.
- "last": the latest by inception or close date, or simply the latest row in a file.
- "TRC": the firm as a whole, or one of its funds or vehicles.
- "in 2024": the calendar year unless the user says fiscal.
If two readings stay open and would give different answers, follow the prompt's rule:
one broad call, then ONE question with options drawn from what it found.

## Where the answer usually is
- A yearly deal-summary deck (Dropbox). Aggregates only: deals seen, deals invested,
  new versus follow-on, total committed, average check, the top deals by amount, and
  which quarter was busiest. It has NO per-deal dates, so it cannot say which was last.
- A "new investments year-to-date" spreadsheet (Dropbox, xlsx). One row per investment
  with an inception date and a commitment. Several snapshots exist through a year; use
  the latest one dated in or just after that year. A snapshot from September cannot
  say what came in December.
- Addepar: what is held now and what it is worth. Not a deal log.

## Steps
1. `query` with the year in `start_date`/`end_date`, asking for the list of new
   investments with inception dates and commitments for that year.
2. If `more_documents` lists xlsx files dated in or just after that year, open the one
   closest after year end with `read_document`.
3. To pick the last, first or largest, sort the rows in `execute_code`; print each row
   used with its marker and date.
4. Answer with the deal, its date and amount, and the as-of date of the snapshot it
   came from. If the snapshot ends before year end, say "latest as of <that date>".

If no per-deal dates turn up: one line saying so, one line with the year's aggregates,
then ask, for example: "Do you mean (a) the most recent new deal by date, (b) the
largest commitment that year, or (c) the latest in one particular fund?"
