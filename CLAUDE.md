# Working in this repo

The Govbot Public Hearings site: a static page (`site/index.html`) plus a stdlib-only
Python scraper (`scraper/main.py`) that a GitHub Action runs every 3 hours. Vercel serves
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
