"""SQL building rules that can be checked without a database."""

from datetime import datetime, timezone

import pytest

from payops.analytics import queries
from payops.analytics.models import AnalysisFilter

F = AnalysisFilter(
    start=datetime(2026, 9, 21, tzinfo=timezone.utc),
    end=datetime(2026, 9, 28, tzinfo=timezone.utc),
    provider_codes=("KESTREL",),
    payment_methods=("CARD",),
    failure_reasons=("TIMEOUT",),
)


def test_values_are_bound_parameters_never_pasted_into_sql():
    where, failed, params, binds = queries._filter_sql(F)
    sql = where + " " + failed
    for value in ("KESTREL", "CARD", "TIMEOUT", "2026"):
        assert value not in sql
    assert ":providers" in sql and ":methods" in sql and ":reasons" in sql
    assert params["providers"] == ["KESTREL"]
    assert len(binds) == 3


def test_unfiltered_query_has_only_the_time_range():
    plain = AnalysisFilter(start=F.start, end=F.end)
    where, failed, params, binds = queries._filter_sql(plain)
    assert where == "created_at >= :start AND created_at < :end"
    assert failed == "status = 'FAILED'"
    assert binds == []


def test_only_whitelisted_groupings_are_allowed():
    with pytest.raises(KeyError):
        queries.breakdown(None, F, by="customer_id; DROP TABLE payments.transactions")


def test_only_whitelisted_time_units_are_allowed():
    with pytest.raises(KeyError):
        queries.time_series(None, F, unit="minute", by_provider=False)
