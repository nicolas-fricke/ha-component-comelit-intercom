#!/usr/bin/env python3
"""Build the HACS release ZIP asset."""

from __future__ import annotations

import argparse
import subprocess
import zipfile
from pathlib import Path

DOMAIN = "comelit_intercom"
INTEGRATION_ROOT = Path("custom_components") / DOMAIN
DEFAULT_OUTPUT = Path(f"{DOMAIN}.zip")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"ZIP output path. Defaults to {DEFAULT_OUTPUT}",
    )
    return parser.parse_args()


def tracked_integration_files() -> list[Path]:
    """Return tracked integration files to include in the release asset."""
    output = subprocess.check_output(
        ["git", "ls-files", str(INTEGRATION_ROOT)],
        text=True,
    )
    return sorted(Path(line) for line in output.splitlines() if Path(line).is_file())


def build_zip(output: Path) -> None:
    """Create a HACS-compatible ZIP with integration files at the archive root."""
    output.parent.mkdir(parents=True, exist_ok=True)
    files = tracked_integration_files()
    if not files:
        raise SystemExit(f"No tracked files found under {INTEGRATION_ROOT}")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(INTEGRATION_ROOT).as_posix())


def main() -> int:
    """Run the packager."""
    build_zip(parse_args().output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
