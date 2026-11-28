# Working in this repo

The Govbot Public Hearings site: a static page (`site/index.html`) plus a stdlib-only
Python scraper (`scraper/main.py`) that a GitHub Action runs every 12 hours. Vercel serves
`site/` with no build step. See README.md for the layout.

## Before calling any change done

Run every change — feature, fix or redesign — through these five questions, and say what
you found when you report back:

1. **Is it built in the most secure way?** No secrets in the repo or the browser; outside
   text (hearing titles, committee names, feed text) escaped before it hits the page
   (`textContent`, never raw `innerHTML` with outside data); external links
   `rel="noopener"`; only documented public APIs, never scraped search pages.
2. **Is it built in the most efficient way?** The page downloads only what it shows;
   animations stop when unseen and animate only `transform`/`opacity`; no live SVG filters.
3. **What regressions could it introduce?** Check both themes, phone + desktop, reduced
   motion, the RSS popover, `#hg-<code>` links, and the feeds. Prove it: run
   `python3 scraper/test_scrape_hearings.py`, and click through with
   `python3 -m http.server -d site`.
4. **Is there dead code, duplicate logic or an unused component?** Delete leftovers.
5. **Is anything more complicated than it needs to be?**

## Rules

- **Scraper:** Python standard library only. Pure `parse_*` functions are tested offline
  against `scraper/__snapshots__/raw/`; only `fetch_*` touches the network. Fail-soft: a
  source outage gives zero hearings for that source, never a crash. After an intended
  output change run `bash scraper/render-snapshots.sh` and review the diff.
- **Never invent data.** A hearing, bill or comment link comes from its official source.
- **The header, drawer and footer belong to Govbot.** Don't edit them here; they're copied
  from `chihacknight/govbot` `docs/theme/header.html` + `footer.html` by
  `scripts/sync_shell.py`. `govbot.css`, `govbot-shell.js`, `govbot-utils.js` and the
  flags load from Govbot's site by full address.
- **Feed addresses are public promises.** `site/hearings.xml` and `site/hearings/<name>.xml`
  are followed in people's readers; keep their names (`hearing_feed_name`,
  `bill_feed_name` in `scraper/main.py`). Govbot republishes copies of these files at
  `chihacknight.github.io/govbot/dashboard/hearings.xml` and `.../hearings/`, and its
  homepage, search and elections pages read `hearings.json` the same way, so keep the
  JSON shape (`schemas/govbot.hearings.schema.json`) backward compatible.
- `SITE_URL` in `scraper/main.py` is this site's address; feeds' links point there.
- **No one else's API to lean on (per request, Nov 2026).** New places come from code we run
  against the government's own site, never a third-party service that can shut off. Open
  States' meeting scrapers run in their own workflow step (Docker), so `scraper/main.py`
  itself stays standard-library only and just reads their JSON files.
- **Every hearing link is that hearing's own (per request, Nov 2026).** Its official page, else its own agenda or
  notice, else its video/audio page — never a calendar, schedule, meeting list, search page or committee profile.
  With nothing specific, the row links the official calendar it was listed on, labelled as its source ("Source:
  <place>'s meeting calendar ↗", `calendar_url`, set only then; Senate: its schedule page) — per request, as a fallback.
  `specific_link` (`_GENERIC_LINK` minus URLs with a meeting id, `_MEETING_ID`) + "shared by two meetings of the
  same place = a list page" (`load_os_events`) + a shared file named with the meeting's date still counts
  (`_date_tokens`, Kentucky's "12-1-26 Agenda.pdf"). Senate rows link their senate.gov video page or nothing.
  The page labels the link by what it opens (`linkLabel`: "Hearing details / Hearing agenda / Watch the hearing ↗").
  "See all N ↗" lists are the only links to a list. Tests: `HearingLinks`.
- **Never get around a block.** A scraper that uses a bot-protection bypass (`cloudscraper`:
  oh, mp) or a site that answers 403 (mi) is left out of `OS_STATES`, not worked around.

## Page and scraper notes (carried over from Govbot's CLAUDE.md, Nov 2026)

`site/index.html` is participation-first and wears its own **"hand-drawn civic zine"** look (Nov 2026,
from the Govbot presentation deck; the shared header, drawer and footer are untouched): warm paper
`#FBFAF5` and near-black ink `#1B1A17` (dark theme = "night paper", the same marks inverted; `--z-*`
tokens in the page's `<style>`), **Roboto** only (Google Fonts, scoped to `.wrap` + the RSS popover;
hierarchy from size), 2px ink outlines on rounded shapes, hard 4px offset ink shadows (no blur) on the
buttons that take you somewhere, **pencil-sketch double lines** behind each card (`.z-card` /
`.hg-frame` `::before/::after`, z-index −1 inside the isolated `.wrap` / `.participation-grid`; the
hearings cards get theirs from a `.hg-frame` wrapper because the federal card scrolls and would clip
them), and a thin, slightly tilted **paper frame** around the content (`.wrap::before`, doubled by `.wrap::after`).
A **"Pencil layer"** at the end of the page's styles pushes it to a heavy graphite sketch: SVG-noise
paper grain + faint smudges on the body; the doubled sketch outlines get uneven hand-drawn corners
(irregular `border-radius`, one partly dashed) — **no live SVG filters**: turbulence filters on the outlines
(Nov 2026) made the page ~3× slower to load and scroll on a phone-speed CPU, so they were removed;
coloured-pencil hatching on the coloured pills kept to their padding — the text sits on a solid patch of the
same colour (content box, `--pf`), since hatching behind small type was unreadable (Nov 2026) and faint hatching inside cards; scribbled underlines under section titles, a scribbled circle round
"say." (`.hh-circle`); pencil rules instead of dashed lines; and margin doodles (a star by each section, a squiggle under the stats). All static masks/gradients, nothing animated. Colour only
marks where you are: the deck's four section colours `--z-c1..c4` (#8EC5F0 blue, #8FD9A0 green,
#FFD166 yellow, #F4A3C0 pink; text on them is always dark ink). Top to bottom: a hero card (caps eyebrow
"Hearings & public comment", "Have your say.", the tagline, the **America-250 fireworks in pencil** —
`drawFireworks()` builds ink-ray bursts in `#hh-fw` (`.fw-stage`), each ray doubled by a lighter sketch
line and given fixed seeded jitter, shooting up on dotted trails over a hand-lettered outline "250th" on a
yellow marker swipe; **every moving piece is its own small HTML box holding a still SVG**, and only the box
moves (transform/opacity, so the compositor animates it with zero repaint — animating `<g>`s inside one
SVG repainted the whole hero card every frame); the lettering "writes" by sliding a clip window over it
(`.fw-reveal`, two opposite transforms); paused
off screen (IntersectionObserver), a still composed frame under reduced motion — and a green pencil-shaded **"See our data on GitHub ↗"** pill (`.hh-gh`, the octocat inline, → this repo) under the tagline, and the live stats as **big
light numerals with caps labels**: "N upcoming hearings and open to public comment" — upcoming and
comment-open — and "N jurisdictions"); then **01 Pick a place**, the map (see "Map-first layout" below). The federal kinds' sections take the colours in turn too (`fedN`). Status chips are small
all-caps ink-outlined pills; colour only where it means something: "Comment due" yellow, "Public
comment open" / "Live hearings" green, "Canceled" pink. The White House hero art and the old colour
fireworks canvas were retired with this redesign (`whitehouse-hero.png` deleted). **Following by RSS** (`#rss-pop`, one popover for the "Follow all", per-state, per-bill and per-hearing
buttons): the feed address with Copy, one-tap "Add to Feedly / Inoreader / NewsBlur" links, "Preview the feed"
(the `feed.xsl` page) and **"Copy a link to share"** (the page's `#hg-<code>` section, which previews as a page
with the `og:` tags in `site/index.html`'s head + `site/assets/og-hearings.png` — "Govbot · Public participation / Your voice belongs in
the room. / Find a hearing. Weigh in.", the White House line art kept on the right), plus a line saying a feed address is for
a reader app, not a post. Feed items (`_add_item` in `scraper/main.py`) lead with the subject
(or committee) and the date, and carry a labelled HTML description (Committee / Where / When / Place / Bills with
titles and links / Take part / Details) that readers and `feed.xsl` (`disable-output-escaping`) render. The pill
next to each jurisdiction's name, and each row's action pill, read **"Official: file a witness slip / sign in to
testify / submit testimony / comment portal ↗"** (`PARTICIPATION`). **One link and one follow button per hearing** (per request, Oct 2026): the filled pill opens the
hearing itself — **"Hearing details ↗"** to its `details_url`, **"Open this rule ↗"** for federal — falling
back to `witness_slip_url` only when a hearing has no page of its own; the place to take part is the
"Official: …" pill by the jurisdiction's name (the `witness_slip_url` table is in
`scraper/README.md`). The bill chips no longer carry their own RSS trigger (the per-bill
feeds are still built; nothing on the page links them), so each row has just "Follow (RSS)". Counts in
the map labels and chips, "x of y showing" and "See all N" carry thousands separators (`fmt`). Feed titles say it all
on one line for widgets that show only titles: "Illinois · Housing Committee — Sep 9, 2026, 2:00 PM ·
HB25, SB1486"; federal "USA (Federal) · <rule> (<agency>) — Comments due Oct 16, 2026". The `<title>` was also corrected (it had been a stray "Legislation Dashboard").
The hearing/participation render engine is otherwise unchanged.


The scraper `scraper/` taps ilga.gov, leg.wa.gov,
malegislature.gov, and akleg.gov directly (not OpenStates), plus **USA (Federal)**: every proposed rule /
rule whose public comment period is open, from the keyless **Federal Register API** (`parse_fr_documents`,
one request, ~200 rules, each linked to its own Regulations.gov comment form; **only the 7 closing
soonest** (`SHOW_MAX`, applied in `assemble` via `federal_preview`) ship in `hearings.json`, and the rules
section ends with "Showing the 7 closing soonest." + a **"See all N ↗"** button (`.hgroup-more`) to the
Federal Register's own search for the same set (`open_total` / `full_list_url` on the `us` jurisdiction
entry). The ~800 open **notices** (announcements that ask for comments without being a rule) are fetched too
(a second query, `fetch_fr` follows the API's 1,000-per-page `next_page_url`), sorted into plain kinds by
title (`fr_notice_category` / `NOTICE_CATEGORIES`: Requests for ideas & information, Environment & wildlife
reviews, Permits/licenses/approvals, Meetings, Privacy & government records, Forms & paperwork reviews
(~70%), Other — paperwork is matched first), and only the **2 closing soonest per kind** (`NOTICE_SHOW`)
ship, each record carrying its `category`; the `us` entry adds `notice_total`, `notice_categories`
(key/label/full count) and `notice_list_url` (the Federal Register's search for every open notice — it
has no list of Govbot's kinds). The page draws the federal group with `federalSections`. **The whole
federal card is one shorter scroll box** (`.hgroup--fed`, 640px; 540px on phones, an ink scrollbar): its header ("USA (Federal) · x of y showing · Follow this state (RSS)") is **sticky**
at the top, and each kind's title (`.fsec-h`) sticks just under it (`--fed-head-h`, the header's height
measured by a ResizeObserver, since it wraps on phones). The rules sit in their own colour-bordered box
(`.fsec-rows`, its kind's colour on a dot before the title, no inner scroll) reading "7 of N showing" with "Showing the 7 closing soonest. **See all N ↗**" (the
Federal Register's list), then **"Other federal notices open for comment"**: one short block per kind,
each clearly labelled with its kind and "2 of N showing" (`fedSection`; `noticePreview` keeps 2 per kind
even from an older file), and **one** line at the bottom — "Showing the X closing soonest. **See all Y ↗**"
→ `notice_list_url`. (The per-kind "See all" buttons, the `notices-<kind>.json` lists and the scraper's
`--notices-dir`/`notice_lists` were removed with this.) **Every state list shows at most 7**
(`SHOW_MAX`), reads **"x of y showing"** (`showing()`; the federal header totals rules + notices), and a
longer list gets "Showing the next 7. **See all N ↗**" to its `portal_url` (`seeAllRow`, `.hgroup-more`,
the button right next to the text) — search's `#hg-<code>` links land on the state's section, which may
not show a hearing past the 7th (the states' `.hgroup-rows` scrollbars are ink);
the old Regulations.gov `DEMO_KEY` path was rate-limited and its placeholder seed had expired, so both
were removed) — federal leads the list, above the states. It writes `site/hearings.json` + a whole-calendar
RSS `hearings.xml` + granular RSS feeds under `site/hearings/` — per bill
(`<jurisdiction>-<NORMALIZED_ID>.xml`), per jurisdiction (`<code>.xml`), and per hearing
(`hearing-<id>.xml`) — so a reader can follow one bill, a whole state, or a single hearing
(schema `schemas/govbot.hearings.schema.json`), plus a static 56-jurisdiction participation
directory `site/participation.json` (schema
`schemas/govbot.participation.schema.json`). Passing `--participation <file>` also emits an
empty placeholder `<code>.xml` for every participation state with no live hearings yet, so
each place's panel carries a "Follow this state (RSS)" link that starts empty
and fills when that state opens (its filter box has a colored border + search icon). `.github/workflows/refresh.yml` rebuilds the feed every 12 hours. Hearings parsers are
offline-snapshot-tested: `python3 scraper/test_scrape_hearings.py`.

**Sketchbook layer (Nov 2026, per request: "too polished, more pencil sketch").** The last block of the page's
styles turns the ink drawing into a graphite wireframe: lines are pencil grey (`--z-graphite` / `--z-graphite-2`,
`--z-line` 1.5px), boxes squarer (`--z-radius` 5–8px, pills stay round), each card's two extra outlines are now
straight pencil passes whose top/bottom and side lines **run past the corners** (`.z-card` / `.hg-frame`
`::before/::after`), cards are the paper itself (light `--z-card` #FDFCF8), buttons, chips and letters are **shaded
in pencil** (`--z-pencil`, two crossed hatch gradients) with smudged graphite shadows (`--z-press`) instead of hard
ink ones. All text stays in **Roboto** (a hand-lettered heading font was tried and dropped, per request). A still **pencil** doodle (`assets/sketch-pencil.svg`, a
graphite-filled CSS mask) sits by 02 Witness slips; by 01 stands the **hearing room drawn while you watch** (below).
Both come from `scripts/sketch_doodles.py` (re-run it after changing a shape or the drawing; needs Pillow + numpy). The sketch layer itself is static (no filters, nothing animated; only the
hearing-room drawing and the hero fireworks move); text keeps the full-contrast ink. The circle round "say." (`.hh-circle::after`) is a heavy loop plus a lighter second pass
with room around the word, so it reads as a circle in both themes rather than strokes through the letters.

**Map-first layout (Nov 2026, per request: "more of a map, users simply click the state/locality, less scrolling").**
The stacked per-state hearing lists, the 56-card "Weigh in — by state" grid, its A–Z letters and the "Jump to" pills were
replaced by **01 Pick a place** (`#places`): a **pencil map** of the states (`buildMap`, `assets/us-states.json`, a
copy of Govbot's legislation-map outlines rounded to one decimal, ~36 KB gzipped), each state a paper fill inside a
graphite line traced again by a lighter, shifted pass (`.mp-pass`); states with hearings coming up hatched green with
"WA · 41" labels (an SVG `<pattern>`, `#mp-hatch-live`), the picked one hatched yellow; Chicago a dot on Illinois'
north-east corner. USA (Federal), Chicago, DC and the territories are chips under the map (`OFF_MAP`), and a
"pick from the list" `<select>` (`buildPicker`, places with hearings first) reaches every place by keyboard and screen
reader; each state shape is a focusable `role=button` too. Picking (`pick`) fills **one panel** (`#mp-panel`, sticky beside
the map on wide screens, under it on phones, scrolled into view): flag, name, "N upcoming" and how-to-take-part pills,
the place's how-to text, its "Official: …" portal button and "Follow this state (RSS)", then its hearings card (built
once per place by `renderHearings` into `groups[code]`; federal keeps its scrolling kinds). Before a pick the panel lists
the places with hearings as quick buttons (`showIntro`). Places merge the participation directory with the feed's
jurisdictions (`buildPlaces`, so Chicago has one). `#hg-<code>` links (search, shared feeds, Govbot's old addresses)
open that place (`openFromHash`, also on `hashchange`), and picking writes `#hg-<code>` to the address bar.
hearings.json, participation.json and the map load together (`start`, `Promise.allSettled`): any one can fail alone.
**"All upcoming hearings"** (`#mp-all`, beside the list picker, and offered in the intro panel; `#hg-all`): every place's
hearings and comment deadlines (`allUpcoming`, not canceled), **grouped by place** (USA (Federal) first, then A–Z; soonest
first within each; `pickAll`), under **sticky "Jump to" pills**, one per place in its colour with its count (`.mp-jump`;
one swipeable row on phones). A pill lands its group's heading just under the pills (measured, done twice because the
first move can pin them), inside the panel on wide screens or the page on phones; each group heading has "How to take
part →" to that place's panel. On phones the swipeable pill row shows a **swipe cue** (`.mp-jumpwrap`: the row fades at
an edge with pills past it, a "›" arrow on the right that nudges three times, `has-more` / `has-back` set on scroll).
Each place's group wears a **four-sided coloured-pencil border** in its pill's colour (`--gc`, traced again by a
lighter offset pass; deepened on light paper), and each pill takes its group's colour (`.mp-jumpchip`). **The pill follows you:** as you scroll the list, the pill of the place being read
fills in its colour, lifts and casts a pencil shadow (`aria-current`), and on phones the row slides to centre it
(`spy`, read once per frame from a passive scroll listener on the panel and the page; it unhooks itself once the
list is replaced; instant under reduced motion). The panel's pencil outlines and the page's paper frame are straight (no tilt), since over
a page this tall a fraction of a degree drifts into the text. **No section numbers** (the giant "01"/"02" were removed,
per request); the headings are "Pick a place" and "Witness slips, Illinois".

**The hearing room, sketched while you watch (Nov 2026, per request), in the hero beside the fireworks.** `#hr-art`
in `.hh-art` (drawing + fireworks side by side; stacked under the heading below 1000px, the fireworks capped at 220px on
phones): a coloured-pencil drawing of people at a hearing ("Open government: in the room, for the people"),
`assets/hearing-room.webp` (600×400, ~60 KB, `fetchpriority=low`). A **coloured pencil** (`assets/pencil-color.svg`:
graphite point, wood cone, yellow body, ferrule, pink eraser), held at an angle, sketches its pencil lines
(`assets/hearing-room-lines.webp`, ~60 KB, from `sketch_doodles.py`'s `pencil_lines`) in **8 back-and-forth strokes**,
scribbling up and down as it goes (`.hr-pencil i`, `hr-scribble`), then the colour fills in: about 8 s, transform and
opacity only, once, when on screen. `drawHearingRoom` adds `.hr-anim` at once (blank paper), and only when the figure
is on screen loads the lines picture and builds the 8 `.hr-band` strips and the pencil, then `.is-on`. So without JS,
without IntersectionObserver or under reduced motion the page loads just the finished colour drawing.

**The circle round "say."** is six coloured-pencil loops (`.hh-circle::after`, an SVG background): #5EBD3E, #FFB900,
#F78200, #E23838, #973999, #009CDF, each a little off the last with a grainy broken line beside it; fixed colours, so
it reads the same in both themes; it sits behind the word (`z-index: -1` inside the isolated `.hh-circle`) with room
around it, so the letters are never covered. **The place list** (`#mp-pick` in `.mp-rainbow`) wears Govbot's moving six-colour
border (its "Download the data" pill): a conic gradient spinning behind the paper-coloured select, transform only,
paused off screen (IntersectionObserver in `buildPicker`), still under reduced motion. The browser's own select arrow is hidden
(`appearance: none`; it was faint on dark paper) and a clear ink chevron (`.mp-rainbow::after`, a mask) marks it as a dropdown.

**02 Witness slips, Illinois (Nov 2026).** A section (`#witness-slips`, yellow `--z-c3`) under the map, shown only while
Illinois is picked (`#witness-slips` links open Illinois at it; the Illinois panel links down to it):
a numbered **how-to** (pick a bill while it's posted, open its slip form from the bill's ilga.gov page, "Self" / "N/A"
for filing as yourself, Proponent / Opponent / No position, "Record of appearance only", a free my.ilga.gov account)
with a note that **slips are public**, beside a **tracker** (`renderSlips`): every bill on an upcoming, not-canceled
Illinois hearing, once (its soonest hearing), closing soonest first, each with a "Closes today / tomorrow / in N days"
chip (slips close when the hearing starts; `wsCloses`), its Govbot title and topics, committee + time + room, the
slip counts when the opt-in `--slips` step filled them, "File a slip ↗" (the bill's ilga.gov status page, which
carries the working Witness Slips button), "The hearing ↗", and **☆ Save** (bill ids in `localStorage`
`ph-saved-slips`, this device only, try/catch'd). Search, a topic filter built from the bills' own Govbot topics, and
a "★ Saved" toggle. No positions are taken. With no bills posted (between sessions) it says so and offers the
Illinois RSS feed. Inspired by Strong Towns Chicago's witness-slip page and tracker; the words and design are our own.

**Chicago City Council (Nov 2026), the first city.** Code `chi` (three letters so it never clashes with a
state; the schema's code pattern is `^[a-z]{2,3}$`). `parse_chi_meetings` reads the City Clerk's keyless eLMS API
(`api.chicityclerkelms.chicago.gov/meeting-agenda`, one request, fixture `raw/chi_meetings.json`): every council and
committee meeting, its time converted from UTC to Chicago wall-clock, `details_url` = the Clerk's meeting page,
`witness_slip_url` = the meeting's Notice PDF (it names where written comments go). `chi_comment_deadline` pulls
"Written Public Comment Deadline: …" out of the published comment into `comment_deadline`; the rest is `title` (the
subject). The page shows Chicago's subject line and a "Comments due …" chip only while that deadline is ahead, never
"Public comment open" without one; "See all" goes to the Clerk's meeting list (`full_list_url`), and the RSS button
reads "Follow this city". **Agenda items (Nov 2026):** each committee meeting's posted Agenda PDF (`chi_agenda_pdfs`; the API's
own agenda list is empty for almost every committee meeting) is read with `pdftotext` (`pdf_text`, a system tool
the refresh workflow installs; fail-soft) and every Clerk record number in it becomes a bill, in agenda order, at most
`CHI_AGENDA_MAX` (40) per meeting (`parse_chi_agenda_text`; layouts differ by committee, so no title is parsed). City
Council agendas (~650 items) are skipped. The refresh downloads Govbot's `bills/chi.json` and `chi_aux.json`, so
`enrich_from_govbot` adds each item's title and topics (~95% matched). Agendas post a few days before a meeting, so
items appear on a later refresh. (The "via govbot ↗" links were removed, per request.)

**Speed (Nov 2026).** The `<head>` preconnects to Govbot's site and Google Fonts first, preloads `hearings.json`,
and fetches all three font families in one stylesheet, loaded without blocking the first paint (`media="print"` swapped
to `all` on load, with a `<noscript>` copy). Each place's hearings card is built the first time it's picked
(`groupFor`, from `pending`), not all at load; the map is its own layer with `contain: layout paint`, and hover and the
pick are flat tints (only states with hearings carry the hatch pattern, drawn once); `us-states.json` holds whole-pixel
absolute points (~28 KB gzipped). Measured on a 4×-slower CPU: main thread blocked during load 162 ms → 59 ms, page
nodes ~3,950 → ~2,090, first paint 804 → 676 ms. `govbot-utils.js` is `defer`red in the head (a synchronous
cross-site script mid-page used to stop the page from being read until it arrived), so the page starts its data
work on `DOMContentLoaded`, which runs after deferred scripts. The jump pills animate only `transform` (their
"you are here" background and shadow used to animate on every scroll), and the inner scroll boxes (`.hgroup-rows`,
`.hgroup--fed`) carry `contain: paint`. Measured on slow 4G with a 4x-slower CPU: hearings on screen 3.2 s → 2.3 s.


**Every other state: Open States' meeting scrapers + a `data/` archive (Nov 2026).** The refresh workflow's
"Scrape state meeting calendars" step runs `openstates/scrapers` (Docker, `<code> events --scrape`, 8 at a time,
15 min cap each) for every place in `OS_STATES`: each reads its legislature's own website (the same open-source
project Govbot's bills come from). Each place's `event_*.json` land in `$RUNNER_TEMP/os/<code>/` with an `_ok` file
when the scraper finished ("no objects returned" = none posted, still ok). Left out: il wa ma ak (our own scrapers
win), ca (database dump), ny dc in usa (need an API key; add a repo secret + env to enable), oh mp (`cloudscraper`),
mi (403). `load_os_events` / `parse_os_event` turn them into hearing records: UTC → local time by `STATE_TZ`; local
midnight = "time not posted" (NJ); id `<code>-os-<sha1 of upstream id | page+name+start>` (Open States' `_id` changes
every run); chamber from the committee name (`_os_chamber`); agenda bills (`_os_bill_id`, url null → the row links
the meeting page); `_plain` strips HTML/entities and link-label lines ("(Agenda)", VA); a title that only repeats the
committee or the location is dropped; rows named after a time of day (`_os_junk_name`, MS's misread PDF) are
dropped. Jurisdiction entries for these places come from `site/participation.json` (`participation_jurisdictions`;
the schema's `participation` enum now has its eight kinds). **`data/<code>/<id>.json`** (`archive_hearings`) keeps
every meeting forever (minus Govbot's `govbot_*` enrichment, plus `first_seen`); a run rewrites a file only when
it changed; an upcoming meeting is deleted only after a *finished* scrape of that place stops listing it. A place
whose scraper broke shows its stored upcoming meetings (`restore_from_archive`); `data/_status.json`
(`update_status`) counts failed runs and the run prints a `::warning::` at 3. Federal isn't archived.
**Downloads** (`write_downloads`, `site/downloads/`, rebuilt every run): `hearings-<code>.csv` (all meetings on
file) + `hearings-all.csv` (UTF-8 BOM, CRLF, formula guard), `<code>.ics` (upcoming; UTC times, no invented end
time, DTSTAMP = `first_seen` so files don't churn), `manifest.json`. The place panel links "Add to my calendar (.ics)
· Spreadsheet (CSV)" for every place with hearings but federal. `data/README.md` documents it (CC BY 4.0). Tests:
`OpenStatesMeetings` / `ArchiveAndDownloads` against real files in `__snapshots__/raw/os_events/`. On Nov 28, 2026
(most legislatures out of session) the run found 351 upcoming hearings in 27 places; expect several thousand in
session, so `hearings.json` (258 KB, 25 KB gzipped then) may need splitting per place.

**Answer engines (AEO / GEO, Nov 2026, per request).** The map is JavaScript, which most search and AI crawlers
don't run, so the answers also exist as plain HTML: (1) **`site/places/<slug>.html`**, one short page per place (57:
participation.json + Chicago; slug = lowercase name, `us` → `federal`, `chi` → `chicago`; `place_slug` in Python,
`placeSlug` in the page), rebuilt every refresh by `write_place_pages` (`--places-dir`, with `site/sitemap.xml`): the
answer in the first sentence ("To weigh in at a Virginia legislative hearing, …", `ACTION` per participation kind),
the place's how-to, numbered steps (`STEP`), up to 40 upcoming hearings, FAQs, and JSON-LD (FAQPage, HowTo,
BreadcrumbList, an ItemList of Events with local-offset start times); all outside text `html.escape`d and the JSON-LD's
`<` escaped. Each map panel links "Step-by-step guide for <place> →". (2) The homepage's visible **FAQs** (`#faq`,
answer-first `.faq-item` h3 + p) and a plain list of every place page; **`scripts/sync_seo.py`** writes the `<head>`
JSON-LD (WebSite, Organization, Dataset with its downloads, FAQPage of exactly the visible questions) between
`seo:ld` markers and the list between `places` markers — edit the questions, run it; `PlacePages` fails on drift.
(3) `site/robots.txt` (every crawler welcome, AI ones named) and `site/llms.txt` (what the site is, where the data
is, how to cite). README opens with the answer and ends with FAQs. Keep new copy answer-first, short, factual.

**U.S. Congress (Nov 2026), code `usa`** (Govbot's code for federal bills, so agenda bills pick up Govbot's titles;
`us` stays the Federal Register's comment periods; `fedRank` / `rank` put `us` then `usa` first). `fetch_usa`, our own
scraper, no key: the Senate's `hearings.xml` (`parse_senate_hearings`; one request; "No committee hearings" rows
skipped; details link = senate.gov's hearings page) and the House Clerk's committee repository: the week calendar
(`parse_house_week`, `HOUSE_WEEKS` = 4 weeks ahead) lists EventIDs, and each meeting page's official "Download Meeting
XML" button (an ASP.NET form posting back to the page; `_house_meeting_xml` posts it with the page's hidden fields) gives
the public-domain meeting XML (`parse_house_meeting`: a subcommittee names its parent via `parent-name`; building codes
spelled out, `HOUSE_BUILDINGS`; status C/P = canceled, P titled "Postponed:"; bills from the meeting's govinfo document
links, `BILLS-119hr2347…` → `HR2347`, plus any in the title). Bill links go to congress.gov's bill page
(`congress_bill`, `congress_number`). `JURISDICTIONS["usa"]` carries `how_to` (the schema's jurisdiction entries
may now) and `full_list_url` = congress.gov's committee schedule. The page shows it as the "Congress" chip (US flag),
"Follow Congress (RSS)", "Find your members of Congress ↗", "Contact your members"; its place page is
`places/congress.html` (own answer: contact your representative and senators; statements for the record where a
committee takes them). Rows now show the meeting's subject (`.h-subject`, 3 lines) for Chicago, Congress and Open
States records — not our IL/WA/MA/AK scrapers, whose titles are often just "Meeting materials at: <link>".
Fixtures: `raw/senate_hearings.xml`, `raw/house_week.html`, `raw/house_meeting_118863.xml` / `_119601.xml`;
tests `CongressHearings`.

**Readability + taking part (Nov 2026, per request).** (1) **Status pills are circled by hand, never filled**: Canceled,
Comment due, Public comment open, "N upcoming" and the witness-slip "Closes" chips get a thick, loose marker loop in their
colour (`--pc` → `--ring`, deepened 62% toward ink on light paper) drawn around the words: `::after` masked by
`--z-ring` (an SVG loop + lighter second pass, non-scaling stroke, squarish ends, the stroke starting along the top so it
never crosses a letter), ink text on paper. (2) **How-to texts** in
`site/participation.json` were rewritten answer-first ("what you do"), with no comparisons to Illinois or other
states; `tips` (top level) are four general tips shown in every place's panel and page. (3) A bill with no link of its
own takes its official page from Govbot's record (`enrich_from_govbot` fills `url`); a bill with no link at all is a
plain chip, never `#`. (4) No "via govbot" links. (5) Every hearing row has **"Add to my calendar (.ics)"**
(`downloadIcs`: built in the browser from the row, times converted to UTC with `Intl`, `zonedUtc`; untimed and federal
deadlines are all-day; no end time; no extra files). (6) **Participation links say what they do, with ↗**: rows link
the place's participation tool (`takePart`, `ACT_LABEL`: "Sign in to testify ↗", "File a witness slip ↗", "Request to
speak ↗", "Submit written testimony ↗"…) only when `participation.json` gives a `participate_url` (the official page
is itself the form: ak az ca co ct fl id il in ia me mo mt nv or wa us); federal rows link the rule's own
Regulations.gov comment form ("Submit a comment ↗"; "Read the rule ↗" is the Federal Register page), Chicago rows its
meeting notice. The panel button is that tool, else "Official legislature site ↗" (Chicago: "How public comment
works ↗", Congress: "Find your members of Congress ↗") — never a homepage dressed up as a comment form. Place pages
follow the same rule (`PART_LABEL`). (The per-row "How to take part in this hearing →" link was removed, per request: the panel already links the
place's step-by-step guide.) A row with no page of its own links its source calendar (`calendar_url`, "Source: … ↗"). A place's form shows only
where it fits: bill-based forms (`BILL_FORMS`: Request to Speak, witness slip, appearance form, position letter) only on
hearings that list bills; Iowa has no `participate_url` (its Public Hearings page covers only its bill hearings, not
committee meetings like the Revenue Estimating Conference). The place-wide calendar link reads "Add all <place> hearings to my calendar (.ics)".

**Iowa public hearings (Nov 2026, per request: "that is what this page is about").** `parse_ia_public_hearings` reads
legis.iowa.gov's own Public Hearings list (`IA_HEARINGS`, one table, one row per hearing; the page never closes its
`<tbody>`, so it reads to `</table>`): date/time (Central), room, the bill (linked to Iowa's BillBook page), the subject,
the hearing's own notice (`details_url`, "Hearing notice ↗") and, while sign-up is open, its own "Signup / Comment" form
(`witness_slip_url` → "Sign up or comment ↗" + "Public comment open"). id `ia-hearing-<meetingID>`. Iowa's ordinary
committee meetings still come from Open States: `OS_SKIP` (il wa ma ak chi us usa) is what Open States is ignored for,
so a place can have both, and an Open States run prunes only its own (`-os-`) archive files. Fixture
`raw/ia_public_hearings.html`; tests `IowaPublicHearings`. The "Hearing" kind pill is circled too (blue, `--z-c1`).

**Place pills pinned in both views (Nov 2026, per request).** `placePills` builds the pill row once for both: in "All
upcoming" ("Jump to") a pill jumps to its place's group; with a place picked ("Places") the row leads with "All upcoming"
and every place with hearings, the current one filled (`aria-current`) and scrolled into view, and a pill switches
places (`pick` / `pickAll`). Pinned at the top of the panel (`.mp-jumpwrap`, sticky), short with its own thin scrollbar
on wide screens (`.mp-jump` max-height 118px), one swipeable row on phones. `upcomingByPlace` gives both the same order
and colours (`zc(gi)`).

**New York public hearings (Nov 2026).** `parse_ny_public_hearings` reads the Legislature's joint Public Hearing Calendar
(`NY_HEARINGS`, nyassembly.gov/leg/?sh=hear; posted all year): each `ul.hrg-info-list` gives committees + chairs (several =
joint), subject, place, time and the hearing's own notice page (`details_url`, "Hearing notice ↗"; the notice PDF says
how to testify). Dates have no year: the calendar's release date (`#hdr-date`) supplies it (a January hearing on a
December calendar is next year). id `ny-hearing-<N>`. New York's committee meetings still need an Open States key.
Fixture `raw/ny_public_hearings.html`; tests `NewYorkPublicHearings`. Other states checked (Nov 2026): most have no
public-hearing list apart from their committee calendar; candidates for later are Oregon's keyless OData
(`CommitteeAgendaItems` with MeetingType "Public Hearing"), Connecticut's per-hearing notices (unreachable from the
sandbox), Vermont's announcements (free text), Nebraska's hearing schedule. Hawaii and the NY Senate site answer 403,
Maryland's witness sign-up needs a login, and Texas's comment API needs a token + reCAPTCHA — all left out.
The place dropdown's arrow (`.mp-rainbow::after`) is 20px and bold with a thin divider before it, so it reads as a dropdown.

**Oregon from its own open data (Nov 2026).** `parse_or_meetings` / `fetch_or` read api.oregonlegislature.gov (keyless
OData, the records OLIS shows): `CommitteeMeetings` (from today; each has its own `AgendaUrl` → `details_url`, "Hearing
agenda ↗"), `Committees` per session (name, `HouseOfAction` H/S/J → chamber) and `CommitteeAgendaItems` (type: "Public
Hearing" items make the subject start "Public hearing: …"; `MeasurePrefix`+`MeasureNumber` → bills linked to OLIS's
measure page; the first line of each item's `Comments` → up to 4 topics). "Invited speakers only" is said once at the
end, "(invited speakers only)", and such a row gets no testify link. id `or-<session>-<committee>-<yyyymmddhhmm>`. Open
States' Oregon scraper found nothing, so `or` is in `OS_SKIP` and out of `OS_STATES`. Oregon's participate link is its
how-to-testify page, so participation.json's `participate_label` ("How to testify") overrides the generic wording (page
and place pages). Fixtures `raw/or_meetings.json`, `or_committees.json`, `or_agenda_items.json`; tests `OregonMeetings`.
**Warnings:** a broken Open States scraper logs a plain line each run and warns only at 3 runs in a row (one warning per
broken place). As of Nov 28, 2026: nm (www.nmlegis.gov has no DNS record; the upstream scraper still uses it), wv and vi
(their sites time out from GitHub), ct (its FTP committee list refuses connections) — all upstream, shown from saved
data meanwhile. Workflows use actions/checkout@v6 and setup-python@v6 (Node 24).

**Every hearing shown can be checked (Nov 2026, per request).** `has_official_link`: a hearing needs its own page, a
form, or its source calendar; Open States records and saved copies without any are dropped (Connecticut's saved
meetings from before the link rules had none, so Connecticut shows nothing until its scraper works again — e.g. "IOREBTA
Housing Advisory Committee Meeting (ZOOM1)" looked unexplained). When a place's scraper broke and its meetings are saved
copies, its jurisdiction entry carries `last_confirmed` (from `data/_status.json`), and the panel says "We couldn't read
<place>'s legislature site on the latest refreshes, so these meetings were last confirmed <date>" (`.mp-stale`).
