---
name: trc-aum-and-portfolio-value
description: "AUM, total assets or portfolio value at a date"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, AUM, Valuation]
    related_skills: [trc-performance-and-profit, trc-deals-and-investments]
---

# AUM and portfolio value

For: "total AUM", "how much do we manage", "assets in Q2", "portfolio value of X".

## Settle the meaning first
- WHOSE: the firm's total AUM, one fund's net assets, or one client's portfolio value.
- WHEN: AUM is a point in time. "AUM in Q2 2024" means at the quarter end, 2024-06-30,
  unless the user asks for an average. State the date you used.
- WHAT counts: assets managed (market value) versus capital committed or invested.

## Where the answer usually is
- A document that STATES the firm's AUM: quarterly letters, investor or board decks,
  firm overviews, regulatory filings. This is the only reliable source of a firm-wide
  total.
- A fund's financial statements: that fund's net assets, never the firm's.
- Addepar: each client's or entity's value. `lookup_live` (one entity, ask first) or
  `period_series` (a value series) per entity.

## Steps
1. `query` asking for TRC's total assets under management, with `start_date` and
   `end_date` around the as-of date (for a quarter end, the whole quarter).
2. If a document states it, quote it with its date and marker. If its date differs
   from the one asked, say so.
3. Do not build a firm-wide total by summing entities: a run allows twenty tool calls,
   far fewer than the firm's entities, and a partial sum presented as AUM is wrong.
   Summing a short NAMED list (up to about fifteen) in `execute_code` is fine; call
   it "the total of these N", never AUM.

When no document states it: one line saying so, then ask, for example: "Should I
(a) give one client's or fund's value at 2024-06-30, (b) total a short list you name,
or (c) look for the nearest quarter where a stated AUM exists?"
