# Govbot Public Hearings

**Find a hearing. Weigh in.** Upcoming legislative committee hearings and federal rules
open for public comment, each with the official place to file a witness slip, sign in
to testify or submit a comment. Follow any state, bill or hearing by RSS.

**Live site:** https://govbot-public-hearings.vercel.app/

A project built on [Govbot](https://chihacknight.github.io/govbot/) (Chi Hack Night). It
used to live inside the Govbot repo as `dashboard/hearings.html`; it moved here so the
site can change and deploy in about a minute without waiting on Govbot's data pipeline.
Old links still work: Govbot's `hearings.html` forwards here, and Govbot keeps
publishing copies of the feeds at their old addresses.

## How it works

```
GitHub Actions, every 3 hours (.github/workflows/refresh.yml)
  scraper/main.py ── ilga.gov · leg.wa.gov · malegislature.gov · akleg.gov · federalregister.gov
        │            + Govbot's bill records (titles + topics for each hearing's bills)
        ▼
  site/hearings.json  site/hearings.xml  site/hearings/*.xml   ── commit ──▶ Vercel redeploys
```

| Folder | What's in it |
| --- | --- |
| `site/` | The published site: `index.html` (the page), `hearings.json`, `participation.json` (how each of 56 jurisdictions takes public input), the RSS feeds and `feed.xsl` (shows a feed as a readable page in a browser). Vercel serves this folder as is; there is no build step. |
| `scraper/` | `main.py`, standard-library Python only, plus offline tests and fixtures. See [`scraper/README.md`](scraper/README.md) for each source. |
| `schemas/` | JSON Schemas for `hearings.json` and `participation.json`. |
| `scripts/sync_shell.py` | Copies Govbot's shared header, menu and footer into the page (runs daily). |

The page uses Govbot's shared styles and scripts (`govbot.css`, `govbot-shell.js`,
`govbot-utils.js`) and state flags straight from Govbot's site, so it always matches.

## Run it locally

```bash
python3 -m http.server -d site 8000          # open http://localhost:8000
python3 scraper/test_scrape_hearings.py      # offline tests, no network
bash scraper/render-snapshots.sh             # rebuild the expected snapshot after a parser change
python3 scraper/main.py --jurisdictions il,wa,ma,ak,us \
  --output site/hearings.json --rss site/hearings.xml --rss-feeds-dir site/hearings \
  --participation site/participation.json    # a live scrape
```

## Workflows

| Workflow | When | What |
| --- | --- | --- |
| `refresh.yml` | every 3 hours + by hand | Scrapes and commits new data. A source outage never blanks the site: files are only replaced when the run found at least one hearing. |
| `test.yml` | every push and pull request | Scraper tests, snapshot check, every JSON/XML file parses. |
| `sync-shell.yml` | daily + by hand | Pulls Govbot's latest header and footer. |

Vercel deploys only `main` (no preview deployments for other branches or pull requests, to save Vercel storage), the same as Know Your Ballot.

## License

Code: same as [Govbot](https://github.com/chihacknight/govbot). Hearing data comes from
each legislature's and the Federal Register's public sites; each record links its source.
