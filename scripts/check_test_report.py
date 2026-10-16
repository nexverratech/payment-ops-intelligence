"""CI gate: read pytest's JUnit XML report and fail unless the run is complete.

    python scripts/check_test_report.py pytest-report.xml --allow-skip test_llm_live

"All tests passed" is not enough in CI: if the database were unreachable, the 50
integration tests would be SKIPPED, not failed, and the build would look green while
testing much less. This script fails the build on any failure or error, and on any
skipped test except those explicitly allowed (the live-LLM test: CI has no Ollama).
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def check(report: str, allowed: list[str], minimum: int) -> list[str]:
    root = ET.parse(report).getroot()
    cases = root.iter("testcase")
    problems, ran = [], 0
    for case in cases:
        ran += 1
        name = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None or case.find("error") is not None:
            problems.append(f"FAILED  {name}")
        elif case.find("skipped") is not None and not any(a in name for a in allowed):
            reason = case.find("skipped").get("message", "")
            problems.append(f"SKIPPED {name}  ({reason})")
    if ran < minimum:
        problems.append(f"only {ran} tests ran; expected at least {minimum}")
    print(f"{ran} tests in the report; {len(problems)} problem(s).")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--allow-skip", action="append", default=[], help="substring of a test allowed to skip")
    parser.add_argument("--minimum", type=int, default=400)
    args = parser.parse_args()
    problems = check(args.report, args.allow_skip, args.minimum)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
