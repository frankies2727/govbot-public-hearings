# Hearings data

Every public hearing and committee meeting this site has listed, one JSON file each, kept
in git so anyone can clone it, diff it or load it. Public meetings are public information;
take it and build on it.

```
data/<code>/<id>.json   one meeting (code: il, wa, va, chi, …)
data/_status.json       each place's last finished scrape, its meeting count, failed runs since
```

- **Where it comes from.** Illinois, Washington, Massachusetts, Alaska and Chicago: our own
  scrapers (`scraper/main.py`). Every other state: [Open States](https://github.com/openstates/openstates-scrapers)'
  open-source meeting scrapers, which we run ourselves twice a day and which read each
  legislature's own website. Every record links back to its official page (`details_url`).
- **History stays.** Past meetings are never deleted. An upcoming meeting is removed only
  when a scrape of that place finished and no longer lists it (it was moved or dropped).
  A meeting that was rescheduled appears once, at its new time.
- **Shape.** The hearing record in `schemas/govbot.hearings.schema.json`, plus `first_seen`
  (when this site first saw it). Times are local wall-clock time in the record's `timezone`.
- **Ready-made downloads** (rebuilt every refresh) at
  `https://govbot-public-hearings.vercel.app/downloads/`:
  `hearings-all.csv`, `hearings-<code>.csv` (every meeting on file, for spreadsheets),
  `<code>.ics` (upcoming meetings; paste its address into Google, Apple or Outlook
  calendar to subscribe) and `manifest.json` (the list of files with counts).
- **Federal** comment periods aren't copied here: the Federal Register already keeps them
  ([federalregister.gov](https://www.federalregister.gov/)).

**License:** CC BY 4.0. Credit "Govbot Public Hearings (Chi Hack Night)".
