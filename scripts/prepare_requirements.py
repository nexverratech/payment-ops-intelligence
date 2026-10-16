"""Turn requirements.lock into a clean, UTF-8 requirements file for Docker and CI.

    python scripts/prepare_requirements.py requirements.lock /tmp/requirements.txt

Why this exists: `pip freeze > requirements.lock` in Windows PowerShell 5.1 writes UTF-16.
Plain-text tools then read it as binary: `grep` printed nothing and still "succeeded", pip
installed an empty list and also "succeeded", and the Docker image ended up with no packages
(found on the first real build, Phase 8). This script:

1. reads UTF-16 (either byte order), UTF-8 with or without BOM;
2. drops the project itself (payops: installed from source) and Windows-only packages;
3. FAILS if core packages are missing, so an empty or broken lock can never pass silently.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WINDOWS_ONLY = {"pywin32", "pywinpty", "pypiwin32"}
CORE = {"fastapi", "uvicorn", "pydantic", "sqlalchemy", "psycopg", "pandas", "numpy", "streamlit", "langgraph"}
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig")
    if len(raw) > 1 and raw[1:2] == b"\x00":          # UTF-16 LE without a BOM
        return raw.decode("utf-16-le")
    return raw.decode("utf-8")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def prepare(text: str) -> tuple[list[str], list[str]]:
    """Returns (requirement lines to install, missing core packages)."""
    keep, names = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAME.match(line)
        if not m:
            continue
        name = normalise(m.group(1))
        if name == "payops" or name in WINDOWS_ONLY:
            continue
        keep.append(line)
        names.add(name)
    return keep, sorted(CORE - names)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    lines, missing = prepare(decode(Path(argv[0]).read_bytes()))
    if missing:
        print(f"{argv[0]}: core packages missing: {', '.join(missing)}. Regenerate it on the machine where the "
              "tests pass:  pip freeze --exclude-editable | Out-File -Encoding ascii requirements.lock")
        return 1
    Path(argv[1]).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} pinned packages -> {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
