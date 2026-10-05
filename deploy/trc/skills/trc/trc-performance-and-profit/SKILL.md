---
name: trc-performance-and-profit
description: "Profit, gains, returns or performance of holdings"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, Performance, Returns]
    related_skills: [trc-aum-and-portfolio-value, trc-deals-and-investments]
---

# Profit, gains and performance

For: "which companies made a profit", "how did X perform", "our gains in Q4",
"best and worst performers", "return on X".

## Settle the meaning first
- WHICH measure: an unrealised gain (a holding's change in value while still held), a
  realised gain (on a sale or exit; in documents only), income (dividends, interest,
  distributions; in statements), or a return percentage (time-weighted).
- WHOSE companies: TRC's own portfolio companies, one fund's holdings, or a client's
  portfolio. "Companies that made a profit" can also mean the companies' OWN operating
  profit, which lives only in their reporting packs, not in Addepar.
- WHICH period: a quarter is its first to last day; say the dates you used.

## What the tools can give
- `lookup_live` with `source="addepar"` and a date range returns, for ONE named
  entity, the end-of-period value, the time-weighted return and the unrealised gain.
  Ask before using it: it spends the firm's request budget, one call per request.
- `period_series` returns VALUES per month or quarter for one entity per call.
  A change in value is NOT profit, because money added or withdrawn moves it too. If
  you use it, call the figure "change in value", never "profit".
- `query` with dates finds documents: quarterly letters, performance reports, fund
  statements and company reporting packs.
- No tool returns gains for many entities at once, so a firm-wide ranking cannot be
  built in one turn. Do not imply one was.

## Steps
1. `query` for a performance or quarterly report covering the period; it may already
   rank holdings.
2. If none, and the user named a short list (up to about fifteen entities) or one
   client's portfolio, get each one's change in value over the period with
   `period_series` in `execute_code`, rank it there, and label it as change in value.
3. For ONE named entity's true return or gain, offer `lookup_live`.

When the request is a firm-wide ranking with no such report, say so in one line and
ask, for example: "Should I (a) rank one client's or fund's holdings by change in value
over Q4, (b) get the Q4 return and unrealised gain for one company you name, or
(c) look for the Q4 performance report?"
