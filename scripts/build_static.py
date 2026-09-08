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

    index = PAGES / "index.html"
    markup = index.read_text(encoding="utf-8")

    # /static/x -> ./x, so the page works at a repository subpath.
    markup = re.sub(r'(href|src)="/static/', r'\1="./', markup)

    # `/docs`, `/v1/health` and the rest are the running service's routes.
    # On a static host they are 404s, and a nav link that 404s reads as a
    # broken site rather than as a build without a backend.
    markup = markup.replace(
        '<a href="/docs">API</a>\n    <span class="mode-badge"',
        '<a href="https://github.com/imzezsv-dot/vocalyze" target="_blank" rel="noopener">Source</a>\n    <span class="mode-badge"',
    )
    markup = re.sub(
        r'<nav>\s*<a href="/docs">API</a>\s*<a href="/v1/health">Health</a>\s*'
        r'<a href="/v1/capabilities">Capabilities</a>\s*</nav>',
        '<nav>\n      <a href="https://github.com/imzezsv-dot/vocalyze">Source</a>\n'
        '      <a href="https://github.com/imzezsv-dot/vocalyze/blob/main/docs/API.md">API</a>\n'
        '      <a href="https://github.com/imzezsv-dot/vocalyze/blob/main/docs/PRIVACY.md">Privacy</a>\n'
        '    </nav>',
        markup,
    )
    markup = markup.replace('href="/v1/privacy/policy"', 'href="https://github.com/imzezsv-dot/vocalyze/blob/main/docs/PRIVACY.md"')
    markup = markup.replace('href="/v1/privacy/audit/verify"', 'href="https://github.com/imzezsv-dot/vocalyze/blob/main/docs/PRIVACY.md#pr-6--every-action-is-recorded-in-a-tamper-evident-trail"')
    markup = markup.replace('<a class="wordmark" href="/">', '<a class="wordmark" href="./">')

    index.write_text(markup, encoding="utf-8")

    # Jekyll would otherwise try to process this directory and drop anything
    # it does not recognise.
    (PAGES / ".nojekyll").write_text("", encoding="utf-8")

    leftover = re.findall(r'(?:href|src)="/(?!/)[^"]*"', markup)
    if leftover:
        raise SystemExit(f"absolute paths would 404 on a project page: {sorted(set(leftover))}")

    print(f"Built {PAGES} for GitHub Pages ({sum(1 for _ in PAGES.rglob('*'))} entries).")


def main() -> None:
    build_mirror()
    if "--pages" in sys.argv:
        build_pages()


if __name__ == "__main__":
    main()
