#!/usr/bin/env python3
"""Copy Govbot's shared header, mobile drawer and footer into site/index.html.

Govbot keeps one template for its site shell (docs/theme/header.html + footer.html in
chihacknight/govbot). This page lives on its own domain, so every link in the copied shell
points at Govbot by its full address, and Govbot's own hearings link points back here.

  python3 scripts/sync_shell.py            # rewrite the blocks in place
  python3 scripts/sync_shell.py --check    # exit 1 if the page has drifted (no writes)
  python3 scripts/sync_shell.py --from DIR # read the templates from a local Govbot checkout

Fail-soft: if Govbot can't be reached the page is left as it is.
"""
import pathlib
import re
import sys
import urllib.request

PAGE = pathlib.Path(__file__).resolve().parents[1] / "site/index.html"
RAW = "https://raw.githubusercontent.com/chihacknight/govbot/main/docs/theme/"
GOVBOT = "https://chihacknight.github.io/govbot/"
HERE = "https://govbot-public-hearings.vercel.app/"

BLOCKS = [
    ("header.html", '<header class="gb-header">', "</header>"),
    ("header.html", '<div class="gb-drawer" data-gb-drawer>', "</div>"),
    ("footer.html", '<footer class="gb-footer">', "</footer>"),
]


def block_re(open_tag, close):
    return re.compile(r"^([ \t]*)" + re.escape(open_tag) + r".*?^\1" + re.escape(close), re.S | re.M)


def load(name, local):
    if local:
        return (pathlib.Path(local) / "docs/theme" / name).read_text()
    req = urllib.request.Request(RAW + name, headers={"User-Agent": "govbot-public-hearings/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def render(template, open_tag, close, indent):
    body = template[template.index("-->") + 3:]  # drop the template's header comment
    block = block_re(open_tag, close).search(body).group(0)
    block = (block.replace("{HOME}", GOVBOT).replace("{ROOT}", GOVBOT)
             .replace("{DASH}", GOVBOT + "dashboard/"))
    # Govbot's hearings links (to its old page or to this site) come back here: same tab, no ↗.
    block = block.replace('href="' + GOVBOT + 'dashboard/hearings.html"', 'href="/"')
    block = re.sub(r'href="' + re.escape(HERE) + r'"((?:\s+role="menuitem")?)\s+target="_blank"\s+rel="noopener"',
                   r'href="/"\1', block)
    block = block.replace('href="' + HERE + '"', 'href="/"')
    block = block.replace("Hearings &amp; Public Comment ↗<", "Hearings &amp; Public Comment<")
    block = block.replace('href="/">Hearings ↗<', 'href="/">Hearings<')
    return "\n".join((indent + line) if line else line for line in block.split("\n"))


def main(argv):
    local = argv[argv.index("--from") + 1] if "--from" in argv else None
    try:
        templates = {name: load(name, local) for name in {b[0] for b in BLOCKS}}
    except OSError as e:
        print(f"couldn't read Govbot's shell ({e}); page left as is")
        return 0
    text = orig = PAGE.read_text()
    for name, open_tag, close in BLOCKS:
        m = block_re(open_tag, close).search(text)
        new = render(templates[name], open_tag, close, m.group(1))
        text = text[:m.start()] + new + text[m.end():]
    if text == orig:
        print("in sync")
        return 0
    if "--check" in argv:
        print("site shell out of sync (run python3 scripts/sync_shell.py)")
        return 1
    PAGE.write_text(text)
    print("updated site/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
