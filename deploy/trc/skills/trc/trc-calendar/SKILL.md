---
name: trc-calendar
description: "Meetings, emails and calls by day or period"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, Affinity, Calendar, Meetings]
    related_skills: [trc-contacts-and-relationships]
---

# Calendar: meetings, emails and calls by day

For: "what meetings do we have today", "who are we meeting tomorrow", "what did we have
yesterday", "meetings this week", "a person's meetings tomorrow", "meetings with a
company this month", "when is our next meeting with a company", "who did we email
yesterday", "any calls this week".

## Settle the meaning first
- "We", "I", "my" and "our" mean the firm: the calendar of everyone at TRC that Affinity
  records. Pass no person unless the user named one.
- A named person narrows to their meetings: `person`. A named company narrows to meetings
  with anyone who works there: `company`.
- "Meetings" is `kind="meeting"` (the default), "emails" `kind="email"`, "calls"
  `kind="call"`.

## Which call answers it
- Always `list_interactions`. Pass `when` as the user's own phrase: today, tomorrow,
  yesterday, this week, next week, last week, this month, next month, last month, or
  next for "our next meeting". Do not turn the phrase into a date yourself: the server
  uses the user's own calendar and timezone.
- Only when the user named dates, pass `start_date` and `end_date` (YYYY-MM-DD) instead.
- "When did we last meet X" looks backwards over a contact, not a day: use
  `find_relationships` with `entity` (its `last_contact`), not this tool.

## Reading the result
- Follow `present`. List every item in `interactions` in order with its local time,
  title and people: the organizer, then our people (`internal`), then others
  (`external`). An all-day item has a date and no time.
- `count` above `shown` means the list is cut: say "showing N of M".
- `freshness_note` set: relay it in one sentence. The answer may miss what was booked
  since `as_of`.
- `matched` names the record a person or company filter was read as: say which one.
- Status `ambiguous`: ask which one the user meant, listing `candidates` by name.
- A `note` saying no person or organisation matches: the name was not found. Ask for the
  full name; never say there were no meetings.
- Status `not_permitted`: the user has no access to the CRM calendar. Say so.
- Status `unavailable`: the calendar cannot be read right now. Say so, and never say
  there are no meetings.
- An empty list with status ok is a real "nothing in that window".
