#!/usr/bin/env python3
"""Mirror app/web/ into public/ so Vercel's edge CDN can serve the static
assets (fonts, CSS, JS) even though the HTML itself comes from the
FastAPI app in api/index.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "web"
DST = ROOT / "public"


def main() -> None:
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    print(f"Mirrored {SRC} → {DST} ({sum(1 for _ in DST.rglob('*'))} entries).")


if __name__ == "__main__":
    main()
