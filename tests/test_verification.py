"""Verification of LLM sentences: citations, numbers, causal language."""

import pytest

from payops.agents.schemas import Sentence
from payops.agents.verification_agent import check_sentence, extract_numbers

SOURCES = {
    "TX-1": "The failure rate was 3.85% against 2.88% in the previous period (+0.98 percentage points). z = 7.75 3.855",
    "TX-2": "KESTREL explains 98% of the extra failures: its failure rate went from 2.94% to 7.27% (+446 failures).",
    "IM-1": "[measured] 536 payments failed at KESTREL between 23 Sep 14:00 and 25 Sep 01:00 UTC, worth 47,233.12 USD.",
    "H-PROVIDER": "Provider-side problem at KESTREL rank 1 score 6",
}


def s(text, *cites):
    return check_sentence(Sentence(text=text, cites=list(cites)), SOURCES)


def test_numbers_ignore_ids_dates_and_times():
    nums = [v for v, _ in extract_numbers("DEP-0026 at 14:00 on 2026-09-23 changed 3.85% and 47,233.12 (TXN-00012345)")]
    assert nums == [3.85, 47233.12]


def test_a_faithful_sentence_passes():
    assert s("The failure rate rose from 2.88% to 3.85% (z = 7.75).", "TX-1") == []


def test_rounded_copy_of_a_number_passes():
    assert s("The failure rate reached about 3.9%.", "TX-1") == []


def test_an_invented_number_is_caught():
    assert any("9999" in r for r in s("About 9,999 payments were affected.", "TX-1"))


def test_a_number_from_an_uncited_finding_is_caught():
    # 536 is real, but it is in IM-1, which this sentence does not cite.
    assert any("536" in r for r in s("536 payments failed.", "TX-1"))


def test_a_sentence_without_citation_is_removed():
    assert "cites no finding" in s("The outage cost the company money.")


def test_unknown_ids_are_caught():
    assert any("unknown IDs" in r for r in s("The failure rate was 3.85%.", "TX-99"))


@pytest.mark.parametrize("text", [
    "The checkout deployment caused the timeouts.",
    "The root cause was the Kestrel API.",
    "Failures increased due to the new release.",
])
def test_unhedged_causal_claims_are_caught(text):
    assert "states a cause as fact" in s(text, "TX-2")


@pytest.mark.parametrize("text", [
    "The leading hypothesis (not confirmed) is that a provider-side problem caused the timeouts.",
    "The deployment may have contributed, but the evidence suggests otherwise.",
])
def test_hedged_explanations_pass(text):
    assert s(text, "H-PROVIDER") == []


def test_claims_of_proof_are_caught():
    assert "claims something is proven or confirmed" in s("This proves the provider failed.", "H-PROVIDER")


def test_small_counts_and_years_are_not_checked():
    assert s("In 2026, only 1 of 4 providers got worse.", "TX-2") == []
