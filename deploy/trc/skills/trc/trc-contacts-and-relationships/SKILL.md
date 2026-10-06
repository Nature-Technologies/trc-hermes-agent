---
name: trc-contacts-and-relationships
description: "Who we contacted, relationship strength, lapsed contacts"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, Affinity, Relationships, CRM]
    related_skills: [trc-deals-and-investments]
---

# Contacts and relationships

For: "who did we contact last week", "who did we email this month", "our strongest
relationships", "all our weak relationships", "who haven't we spoken to since June",
"strong relationships we're losing touch with", "who at a company knows us best".

## Settle the meaning first
- "Affinity" in a question is the CRM we read, never the company called Affinity. Pass
  `source="affinity"`; never look the word up as a person or organisation.
- "contacted": the latest email, meeting or call Affinity records for a person or
  company. Affinity keeps only the LATEST contact per record, not a full history.
- "last week" is the last seven days; "this month" runs from the 1st to today. Work the
  dates out from today's date and pass them as YYYY-MM-DD.
- "strong" is band `regular` (score 0.7 and up), "moderate" is `occasional` (0.4 to
  0.7), "weak" is `sporadic` (under 0.4). These are Affinity's own bands.
- "people" or "individuals" means `category="PERSON"`; "companies" means `"COMPANY"`.
  Omit `category` when the user means both.

## Which call answers it
- Who was contacted in a period: `list_entities` with `contacted_since` (and
  `contacted_until` if the period has ended).
- Who has not been contacted since a date: `list_entities` with `not_contacted_since`.
- Every relationship in a band, or the complete ranking: `list_entities` with `band`.
- Strong relationships losing touch: `list_entities` with `band="regular"` and
  `not_contacted_since` about 90 days back, unless the user gave a period.
- The firm's top relationships at a glance: `find_relationships` with `firm_wide=true`
  (top 25 only, stored scores).
- One named person or company, who knows them, or their recent emails and meetings:
  `find_relationships` with `entity`.
- The members of a named CRM list, ranked: `find_relationships` with `list_name` and
  `band`.
Never answer these with `query`. It reads a handful of documents and cannot list
everyone; its "Last contact" stamps can be older than the CRM's.

## Reading the result
- Each row carries `last_contact`, `score` and `band`. Show the date and the band; quote
  the score exactly as returned, never rounded or recomputed.
- `pages` above 1 means this is one page. Say "page P of N, T in total" and offer the
  next page. Never present one page as everyone.
- `coverage` (scored of total) appears with a band. Scores are filled by a nightly job
  and do not yet cover every record, so say "of the N with a stored score".
- `contact_note` explains the dates. Relay it when it says a window ends before today
  or that records never contacted are left out.
- An empty list with status ok is a real "nobody matched". Status `unavailable` means
  the records could not be read right now. Say so, and never say nobody was contacted.

If the user names a period and a band together ("weak relationships we emailed this
month"), pass both `contacted_since` and `band` in one call. If the request is still
unclear, make one broad call, then ask one question with options drawn from what came
back.
