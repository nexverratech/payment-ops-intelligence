"""The CI gate that refuses a run where tests were skipped instead of run."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_test_report.py"
spec = importlib.util.spec_from_file_location("check_test_report", SCRIPT)
check_test_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_test_report)


def report(tmp_path, cases):
    body = "".join(f'<testcase classname="{c}" name="{n}">{inner}</testcase>' for c, n, inner in cases)
    path = tmp_path / "r.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>', encoding="utf-8")
    return str(path)


def test_a_complete_run_passes(tmp_path):
    r = report(tmp_path, [("tests.test_a", "test_x", ""),
                          ("tests.integration.test_llm_live", "test_live", '<skipped message="no Ollama"/>')])
    assert check_test_report.check(r, ["test_llm_live"], minimum=2) == []


def test_skipped_database_tests_fail_the_build(tmp_path):
    r = report(tmp_path, [("tests.integration.test_loaded_data", "test_rows", '<skipped message="PostgreSQL is not reachable"/>')])
    assert any("SKIPPED" in p and "not reachable" in p for p in check_test_report.check(r, ["test_llm_live"], minimum=1))


def test_failures_and_too_few_tests_fail_the_build(tmp_path):
    r = report(tmp_path, [("tests.test_a", "test_x", '<failure message="boom"/>')])
    problems = check_test_report.check(r, [], minimum=400)
    assert any(p.startswith("FAILED") for p in problems) and any("only 1 tests" in p for p in problems)
