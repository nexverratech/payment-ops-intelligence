"""requirements.lock is read correctly whatever encoding Windows wrote it in (Phase 8 lesson)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_requirements", ROOT / "scripts" / "prepare_requirements.py")
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)

LOCK = ("fastapi==0.120.0\r\nuvicorn==0.38.0\r\npydantic==2.12.0\r\nSQLAlchemy==2.0.44\r\npsycopg==3.2.10\r\n"
        "pandas==2.3.3\r\nnumpy==2.3.3\r\nstreamlit==1.50.0\r\nlanggraph==1.0.2\r\npayops==0.1.0\r\npywin32==311\r\n")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8-sig", "utf-8"])
def test_every_windows_encoding_is_read(encoding):
    lines, missing = prep.prepare(prep.decode(LOCK.encode(encoding)))
    assert missing == [] and len(lines) == 9
    assert not any(l.lower().startswith(("payops", "pywin32")) for l in lines)


def test_an_unreadable_or_empty_lock_fails_instead_of_installing_nothing():
    _, missing = prep.prepare("")
    assert "fastapi" in missing and "langgraph" in missing


def test_the_script_writes_utf8(tmp_path):
    src, dst = tmp_path / "lock", tmp_path / "out.txt"
    src.write_bytes(LOCK.encode("utf-16"))
    assert prep.main([str(src), str(dst)]) == 0
    assert dst.read_text(encoding="utf-8").startswith("fastapi==")


@pytest.mark.skipif(not (ROOT / "requirements.lock").exists(), reason="no requirements.lock yet")
def test_the_real_lock_has_every_core_package():
    _, missing = prep.prepare(prep.decode((ROOT / "requirements.lock").read_bytes()))
    assert missing == [], f"requirements.lock lacks {missing}; regenerate it (README section 4)"
