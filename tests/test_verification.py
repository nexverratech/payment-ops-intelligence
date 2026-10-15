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


# ---- Regression cases: sentences qwen3:4b wrote on the real dataset (15 Oct 2026) ------------
# All four are true; the first version of the checker removed them. Each case below pins the fix.

from payops.agents.verification_agent import _sources, known_dates  # noqa: E402

REAL_RESULT = {
    "plan": {"baseline_start": "2026-09-14T00:00:00+00:00", "current_end": "2026-09-28T00:00:00+00:00"},
    "findings": [
        {"finding_id": "TX-1", "kind": "observation", "figures": [],
         "statement": "The failure rate was 7.27% against 2.94% in the previous period (+4.33 percentage points). "
                      "This is a statistically significant change (z = 14.04)."},
        {"finding_id": "TX-2", "kind": "observation", "figures": [],
         "statement": "KESTREL explains 100% of the extra failures: its failure rate went from 2.94% to 7.27%."},
        {"finding_id": "PT-1", "kind": "observation", "figures": [],
         "statement": "KESTREL: failure rate 2.73% → 23.07% from 23 Sep 14:00 to 25 Sep 01:00 UTC; "
                      "87% of failures in the window were TIMEOUT."},
        {"finding_id": "PT-2", "kind": "observation", "figures": [],
         "statement": "INC-0003: Notice posted 3.0 h after the anomaly began, by the same provider."},
        {"finding_id": "PT-3", "kind": "hypothesis", "figures": [],
         "statement": "Leading hypothesis (not confirmed): Provider-side problem at KESTREL (evidence score 6)."},
        {"finding_id": "FC-1", "kind": "forecast", "figures": [],
         "statement": "About 1,460 payment attempts are expected on 2026-09-29."},
    ],
    "hypotheses": [{"hypothesis_id": "H-PROVIDER", "rank": 1, "score": 6, "title": "Provider-side problem at KESTREL",
                    "supporting": ["87% of failures in the window are TIMEOUT, a technical failure type."],
                    "contradicting": [], "next_checks": ["Ask KESTREL for their incident report and exact timeline."]}],
}
REAL_SOURCES = _sources(REAL_RESULT)
REAL_DATES = known_dates(REAL_RESULT, REAL_SOURCES)


def real(text, *cites):
    return check_sentence(Sentence(text=text, cites=list(cites)), REAL_SOURCES, REAL_DATES)


def test_the_investigated_week_written_as_a_date_passes():
    assert real("The failure rate for KESTREL increased from 2.94% to 7.27% in the week of 21 September 2026, "
                "a statistically significant change (z = 14.04).", "TX-1", "TX-2") == []


@pytest.mark.parametrize("text", ["Failures peaked on 23 Sep.", "From September 23rd to Sept 25, 2026, failures rose.",
                                  "Volume is expected on 29 September."])
def test_dates_from_the_findings_or_scope_pass(text):
    assert real(text, "PT-1", "FC-1") == []


def test_a_date_outside_the_period_and_findings_is_caught():
    assert any("3 October" in r for r in real("Failures returned to normal on 3 October.", "PT-1"))


def test_a_date_does_not_hide_an_invented_number():
    assert any("9999" in r for r in real("On 21 September, 9,999 payments failed.", "TX-1"))


def test_lowercase_may_is_a_word_not_a_month():
    assert real("The other 3 may also be affected; this is a hypothesis.", "H-PROVIDER") == []


def test_citing_the_leading_hypothesis_finding_covers_the_hypothesis_evidence():
    assert real("The leading hypothesis (not confirmed) is a provider-side problem at KESTREL, supported by 87% of "
                "failures being TIMEOUT errors and KESTREL publishing a notice 3 hours after the anomaly began.",
                "PT-3", "PT-2") == []


def test_other_findings_still_do_not_lend_their_numbers():
    assert any("87" in r for r in real("87% of failures were TIMEOUT.", "TX-1"))


@pytest.mark.parametrize("text", [
    "Ask KESTREL for their incident report and exact timeline to confirm the provider-side problem.",
    "The provider-side problem is not yet confirmed.",
    "This should be confirmed with the provider before closing the incident.",
])
def test_intending_to_confirm_is_not_claiming_proof(text):
    assert real(text, "H-PROVIDER") == []


@pytest.mark.parametrize("text", ["KESTREL confirmed the outage.", "The timeline proves it was KESTREL.",
                                  "This was definitely a provider problem."])
def test_claims_of_proof_are_still_caught(text):
    assert "claims something is proven or confirmed" in real(text, "H-PROVIDER")
