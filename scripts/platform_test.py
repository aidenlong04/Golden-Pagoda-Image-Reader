#!/usr/bin/env python3
"""Quick platform-detection probe.

Usage:
    python scripts/platform_test.py path/to/screenshot.png [more.png ...]

Prints the detected platform plus a per-platform score breakdown so you
can triage real screenshots that the production bot misclassifies.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import detect_platform  # noqa: E402


def probe(path: Path) -> None:
    img = Image.open(path)
    platform, scores = detect_platform(img)
    print(f"\n=== {path} ===")
    print(f"  detected: {platform}")
    for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        print(f"    {name:12s} {score:.3f}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    for arg in argv[1:]:
        probe(Path(arg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
