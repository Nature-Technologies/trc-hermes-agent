---
name: trc-calendar
description: "Meetings, emails, calls and notes by day"
version: 1.0.0
author: TRC
license: Proprietary
metadata:
  hermes:
    tags: [TRC, Affinity, Calendar, Meetings]
    related_skills: [trc-contacts-and-relationships]
---

# Calendar: meetings, emails, calls and notes by day

For: "what meetings do we have today", "who are we meeting tomorrow", "what did we have
yesterday", "meetings this week", "a person's meetings tomorrow", "meetings with a
company this month", "when is our next meeting with a company", "who did we email
yesterday", "any calls this week", "notes written yesterday", "a person's notes this
week", "notes about a company this month", "what do I need to know before 10am today",
"prep me for tomorrow's meetings".

## Settle the meaning first
- "We", "I", "my" and "our" mean the firm: the calendar of everyone at TRC that Affinity
  records. Pass no person unless the user named one.
- A named person narrows to their meetings: `person`. A named company narrows to meetings
  with anyone who works there: `company`.
- "Meetings" is `kind="meeting"` (the default), "emails" `kind="email"`, "calls"
  `kind="call"`, "notes" `kind="note"`.
- For notes, who WROTE it is `author` ("Ned's notes", "notes Ned wrote"); who or what
  it is ABOUT is `person` or `company` ("notes about Acme"). When the user's words could
  mean either, prefer `author` for a colleague and `company`/`person` for anyone else.

## Which call answers it
- A briefing (what to know before a time, prep for a day's meetings): one
  `meeting_briefing` call with `when` and `before`/`after` as the user said them
  ("10am", "2pm"). It already carries each attendee's company, last contact,
  relationship band, latest notes and deals. Do not follow it with other calls.
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
- A note has `created`, `author`, `about` (what it mentions) and `text`. Summarise the
  text in a sentence or two; `text_cut` true means it is an excerpt, so say so.
- A briefing: go meeting by meeting in time order (time, title, who), then for
  each person in `people` their company, last contact, band, a one-line gist of
  their latest note and any deals with stage. Say plainly where we have no
  notes or no contact. `unknown_people` are attendees the CRM has no record
  for: give the count, never guess who they are.
- `count` above `shown` means the list is cut: say "showing N of M".
- `freshness_note` set: relay it in one sentence. The answer may miss what was booked
  since `as_of`.
- `matched` (or `matched_author`) names the record a filter was read as: say which one.
- Status `ambiguous`: ask which one the user meant, listing `candidates` by name.
- A `note` saying no person or organisation matches: the name was not found. Ask for the
  full name; never say there were no meetings.
- Status `not_permitted`: the user has no access to the CRM calendar. Say so.
- Status `unavailable`: the calendar cannot be read right now. Say so, and never say
  there are no meetings.
- An empty list with status ok is a real "nothing in that window".
