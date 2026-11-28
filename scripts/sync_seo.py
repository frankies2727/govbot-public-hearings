#!/usr/bin/env python3
"""Keep the homepage's answer-engine markup in step with what people see.

Reads the visible FAQs (#faq .faq-item h3 + p) in site/index.html and writes, between the
<!-- seo:ld:start/end --> markers in <head>, one JSON-LD block: the site, who makes it,
the dataset (with its downloads) and an FAQPage of exactly those questions. Also fills
<!-- places:start/end --> with a plain link to every place's page (site/places/), from
site/participation.json plus Chicago, so crawlers that don't run JavaScript find them all.

    python3 scripts/sync_seo.py          # rewrite site/index.html
    python3 scripts/sync_seo.py --check  # exit 1 if it is out of date (the tests run this)
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
import main as scraper  # noqa: E402  (place_slug, SITE_URL)

INDEX = ROOT / "site" / "index.html"
PARTICIPATION = ROOT / "site" / "participation.json"
SITE = scraper.SITE_URL


def faqs(page):
    sec = re.search(r'<section[^>]*id="faq".*?</section>', page, re.S).group(0)
    out = []
    for q, a in re.findall(r'<div class="faq-item[^"]*"><h3>(.*?)</h3><p>(.*?)</p></div>', sec, re.S):
        out.append((html.unescape(re.sub(r"<[^>]+>", "", q)), html.unescape(re.sub(r"<[^>]+>", "", a))))
    return out


def places():
    rows = json.loads(PARTICIPATION.read_text())["jurisdictions"]
    items = [(r["code"], r["name"]) for r in rows] + [("chi", "Chicago City Council")]
    return sorted(items, key=lambda x: (x[0] != "us", x[1]))


def ld(page):
    org = {"@type": "Organization", "@id": SITE + "#org", "name": "Govbot (Chi Hack Night)",
           "url": "https://chihacknight.github.io/govbot/",
           "sameAs": ["https://github.com/chihacknight/govbot",
                      "https://github.com/frankies2727/govbot-public-hearings"]}
    graph = [
        {"@type": "WebSite", "@id": SITE + "#site", "name": "Govbot Public Hearings", "url": SITE,
         "description": "Upcoming state legislative hearings, Chicago City Council meetings and federal "
                        "rules open for comment, with the official way to testify or comment on each.",
         "publisher": {"@id": SITE + "#org"}, "inLanguage": "en-US"},
        org,
        {"@type": "Dataset", "name": "Govbot Public Hearings data",
         "description": "Every public hearing and committee meeting the site has listed, read from each "
                        "government's own website: one JSON record per meeting in git, plus CSV and "
                        "iCalendar files per place. Updated every 12 hours.",
         "url": "https://github.com/frankies2727/govbot-public-hearings/tree/main/data",
         "license": "https://creativecommons.org/licenses/by/4.0/", "isAccessibleForFree": True,
         "creator": {"@id": SITE + "#org"}, "spatialCoverage": "United States",
         "keywords": ["public hearings", "legislative committee hearings", "public comment",
                      "witness slips", "testimony", "open government"],
         "distribution": [
             {"@type": "DataDownload", "encodingFormat": "text/csv",
              "contentUrl": SITE + "downloads/hearings-all.csv"},
             {"@type": "DataDownload", "encodingFormat": "application/json",
              "contentUrl": SITE + "hearings.json"},
             {"@type": "DataDownload", "encodingFormat": "application/rss+xml",
              "contentUrl": SITE + "hearings.xml"}]},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faqs(page)]},
    ]
    body = json.dumps({"@context": "https://schema.org", "@graph": graph},
                      ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return f'<script type="application/ld+json">\n{body}\n</script>'


def render(page):
    page = re.sub(r"(<!-- seo:ld:start -->\n).*?(<!-- seo:ld:end -->)",
                  lambda m: m.group(1) + ld(page) + "\n" + m.group(2), page, count=1, flags=re.S)
    links = "\n".join(
        f'      <a href="places/{scraper.place_slug(c, n)}.html">{html.escape(n)}</a>'
        for c, n in places())
    return re.sub(r"(<!-- places:start -->\n).*?(<!-- places:end -->)",
                  lambda m: m.group(1) + links + "\n" + m.group(2), page, count=1, flags=re.S)


if __name__ == "__main__":
    page = INDEX.read_text()
    new = render(page)
    if "--check" in sys.argv:
        if new != page:
            print("site/index.html is out of date: run python3 scripts/sync_seo.py")
            sys.exit(1)
        print("seo markup up to date")
    else:
        INDEX.write_text(new)
        print(f"wrote {len(faqs(new))} FAQs and {len(places())} place links")
