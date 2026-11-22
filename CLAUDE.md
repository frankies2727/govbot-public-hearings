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
same colour (content box, `--pf`), since hatching behind small type was unreadable (Nov 2026) and faint hatching inside cards; section numbers solid in the section colour inside an ink outline; scribbled underlines under section titles, a scribbled circle round
"say." (`.hh-circle`); pencil rules instead of dashed lines; and margin doodles (an arrow into "Jump to",
a star by each section, a squiggle under the stats). All static masks/gradients, nothing animated. Colour only
marks where you are: the deck's four section colours `--z-c1..c4` (#8EC5F0 blue, #8FD9A0 green,
#FFD166 yellow, #F4A3C0 pink; text on them is always dark ink). Top to bottom: a hero card (caps eyebrow
"Hearings & public comment", "Have your say.", the tagline, the **America-250 fireworks in pencil** —
`drawFireworks()` builds ink-ray bursts in `#hh-fw` (`.fw-stage`), each ray doubled by a lighter sketch
line and given fixed seeded jitter, shooting up on dotted trails over a hand-lettered outline "250th" on a
yellow marker swipe; **every moving piece is its own small HTML box holding a still SVG**, and only the box
moves (transform/opacity, so the compositor animates it with zero repaint — animating `<g>`s inside one
SVG repainted the whole hero card every frame); the lettering "writes" by sliding a clip window over it
(`.fw-reveal`, two opposite transforms); paused
off screen (IntersectionObserver), a still composed frame under reduced motion — and the live stats as **big
light numerals with caps labels**: "N upcoming hearings and open to public comment" — upcoming and
comment-open — and "N jurisdictions"); the **"Jump to" nav pills** (`#hh-jump`, hidden with fewer
than two jurisdictions; one per jurisdiction in section order with its count, linking to `#hg-<code>`;
the pill of the jurisdiction on screen fills with its colour and gets the hard shadow —
`markJumpInView`, an IntersectionObserver setting `aria-current`); then two **section dividers**
(giant outlined number, title, one line): **01 Upcoming hearings** and **02 Weigh in — by state**.
Each jurisdiction's card puts its name on a tab in its colour (`zc(i)` cycles `--z-c1..c4`, the same
colour as its jump pill; `.hgroup` keeps `scroll-margin-top`, and search's `#hg-<code>` links use the
same ids). The federal kinds' sections take the colours in turn too (`fedN`). Status chips are small
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
the jump row, "x of y showing" and "See all N" carry thousands separators (`fmt`). Feed titles say it all
on one line for widgets that show only titles: "Illinois · Housing Committee — Sep 9, 2026, 2:00 PM ·
HB25, SB1486"; federal "USA (Federal) · <rule> (<agency>) — Comments due Oct 16, 2026". **"Weigh in — by state"** has an
**A–Z letter row** next to its filter box (`buildLetters` / `pLetter`): a letter shows only states
starting with it (letters no state starts with are disabled; clicking the active one, or "All",
shows every state), combined with the typed filter; "No states match." when both exclude everything. The `<title>` was also corrected (it had been a stray "Legislation Dashboard").
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
each "Weigh in — by state" card carries a "Follow this state (RSS)" link that starts empty
and fills when that state opens (its filter box has a colored border + search icon). `.github/workflows/refresh.yml` rebuilds the feed every 12 hours. Hearings parsers are
offline-snapshot-tested: `python3 scraper/test_scrape_hearings.py`.
