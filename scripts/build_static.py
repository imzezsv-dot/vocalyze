#!/usr/bin/env python3
"""Build the static copies of the interface.

Two outputs, because two hosts want different shapes:

`public/`  — a byte-identical mirror of `app/web/`, for a host that serves the
             HTML from the Python app and the assets from a CDN at `/static/`.
             A test asserts it stays identical to what the service serves.

`site/`    — a self-contained build for GitHub Pages, where absolute `/static/`
             paths break: a project page lives under `/<repo>/`, so the same
             HTML has to reference its assets relatively. This build also runs
             the models in the browser, so it transcribes real audio with no
             server behind it.

    python -m scripts.build_static            # public/
    python -m scripts.build_static --pages    # public/ and site/
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "web"
MIRROR = ROOT / "public"
PAGES = ROOT / "site"


def build_mirror() -> None:
    if MIRROR.exists():
        shutil.rmtree(MIRROR)
    shutil.copytree(SRC, MIRROR)
    print(f"Mirrored {SRC} → {MIRROR} ({sum(1 for _ in MIRROR.rglob('*'))} entries).")


def build_pages() -> None:
    if PAGES.exists():
        shutil.rmtree(PAGES)
    shutil.copytree(SRC, PAGES)

    for page in PAGES.glob("*.html"):
        markup = page.read_text(encoding="utf-8")

        # /static/x -> ./x, and every in-site link relative, so the build works
        # at a repository subpath as well as at a domain root.
        markup = re.sub(r'(href|src)="/static/', r'\1="./', markup)
        markup = re.sub(r'href="/([a-z-]+\.html)"', r'href="./\1"', markup)
        markup = markup.replace('href="/"', 'href="./"')

        # Say outright that there is no API, rather than letting each page
        # discover it by requesting one and taking the 404. The probe works,
        # but it puts failed requests in the console of a build whose whole
        # claim is that it makes no requests.
        markup = markup.replace(
            "<body>",
            "<body>\n<script>window.VOCALYZE_STATIC = true;</script>",
            1,
        )

        page.write_text(markup, encoding="utf-8")

        # A link out to a repository, an issue tracker or a docs file is not
        # part of the product. The audience for this build is being shown a
        # working system, not its source.
        for pattern in ("github.com", "github.io", "githubusercontent"):
            if pattern in markup:
                raise SystemExit(f"{page.name} links to {pattern}; the published build must not")

        leftover = re.findall(r'(?:href|src)="/(?!/)[^"]*"', markup)
        if leftover:
            raise SystemExit(f"{page.name} has absolute paths that would 404 on a project page: {sorted(set(leftover))}")

    # Jekyll would otherwise try to process this directory and drop anything
    # it does not recognise.
    (PAGES / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Built {PAGES} for GitHub Pages ({sum(1 for _ in PAGES.rglob('*'))} entries).")


def main() -> None:
    build_mirror()
    if "--pages" in sys.argv:
        build_pages()


if __name__ == "__main__":
    main()
