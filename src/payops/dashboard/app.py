"""Payment Operations dashboard (Streamlit).

Run (with the API already running):  streamlit run src/payops/dashboard/app.py

Streamlit re-runs this whole file from top to bottom every time you change a
filter. API calls are cached for 60 seconds so that stays fast.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import streamlit as st

from payops.analytics.metrics import last_complete_week
from payops.config import get_settings
from payops.dashboard import charts
from payops.dashboard.api_client import ApiClient, ApiError, Filters

UNIT_LABEL = {"hour": "hour", "3h": "3-hour block", "day": "day"}
EXAMPLE_QUESTION = (
    "Why did the payment failure rate increase last week? Identify the main contributing provider, "
    "estimate the impact, and prepare an investigation report."
)

st.set_page_config(page_title="Payment Operations Intelligence", page_icon="📉", layout="wide")


# ---- API access (cached) ---------------------------------------------------------

@st.cache_resource
def api() -> ApiClient:
    s = get_settings()
    return ApiClient(s.api_base_url, s.api_timeout_seconds)


@st.cache_data(ttl=60, show_spinner=False)
def fetch(endpoint: str, *args) -> dict:
    return getattr(api(), endpoint)(*args)


def call(endpoint: str, *args) -> dict:
    """Fetch, or show the problem on screen and stop this run."""
    try:
        return fetch(endpoint, *args)
    except ApiError as exc:
        st.error(str(exc))
        st.stop()


def figure_value(kpis: dict, name: str) -> float | None:
    return next((f["value"] for f in kpis["figures"] if f["name"] == name), None)


def utc(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


# ---- Sidebar: status and filters ---------------------------------------------------

meta = call("meta")
window_start = pd.Timestamp(meta["window_start"]).to_pydatetime()
window_end = pd.Timestamp(meta["window_end"]).to_pydatetime()
default_start, default_end = last_complete_week(window_end)

with st.sidebar:
    st.header("Filters")
    picked = st.date_input(
        "Date range (UTC)",
        value=(default_start.date(), (default_end - timedelta(days=1)).date()),
        min_value=window_start.date(),
        max_value=(window_end - timedelta(days=1)).date(),
    )
    if not isinstance(picked, (list, tuple)) or len(picked) != 2:
        st.info("Pick an end date to complete the range.")
        st.stop()
    names = {p["provider_code"]: p["display_name"] for p in meta["providers"]}
    providers = st.multiselect("Providers", list(names), format_func=lambda c: f"{names[c]} ({c})")
    methods = st.multiselect("Payment methods", meta["payment_methods"])
    reasons = st.multiselect("Failure reasons", meta["failure_reasons"],
                             help="Counts only failures with these reasons. Attempts still include all payments.")
    span_days = (picked[1] - picked[0]).days + 1
    unit = st.radio("Time buckets", ["hour", "3h", "day"], index=0 if span_days <= 3 else (1 if span_days <= 14 else 2),
                    horizontal=True, help="Hourly rates at night rest on few payments and look noisy; 3-hour blocks smooth that.")

    st.divider()
    health = call("health")
    st.caption("System status")
    st.markdown(
        f"- Database: **{health['components']['database']['status']}**\n"
        f"- Local LLM: **{health['components']['llm']['status']}** (mode: {health['llm_mode']})\n"
        f"- API version {health['version']}"
    )
    st.caption(f"Synthetic data · dataset {meta['dataset_id']}")

filters = Filters(
    start=utc(picked[0]), end=utc(picked[1]) + timedelta(days=1),
    providers=tuple(providers), methods=tuple(methods), reasons=tuple(reasons),
)

# ---- Header and KPI tiles ---------------------------------------------------------

st.title("Payment Operations Intelligence")
st.caption(f"{picked[0]:%d %b %Y} – {picked[1]:%d %b %Y} (UTC), compared with the {span_days} days before. "
           "All data is synthetic.")

kpis = call("kpis", filters)
k = {f["name"]: f["value"] for f in kpis["figures"]}
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Payment attempts", f"{k['attempts']:,.0f}")
c2.metric("Succeeded", f"{k['succeeded']:,.0f}")
c3.metric("Failed", f"{k['failures']:,.0f}", delta=f"{k['failures'] - k['previous_failures']:+,.0f} vs previous",
          delta_color="inverse")
if "failure_rate" in k:
    delta = f"{k['failure_rate'] - k['previous_failure_rate']:+.2f} pts" if "previous_failure_rate" in k else None
    c4.metric("Failure rate", f"{k['failure_rate']:.2f}%", delta=delta, delta_color="inverse")
else:
    c4.metric("Failure rate", "n/a")
c5.metric("Affected transaction value", f"${k['affected_transaction_value']:,.0f}",
          help="Total amount of payments that failed. This is transaction value, not revenue or loss: "
               "many customers retry, and the company earns only a fee per payment.")

# ---- Tabs ------------------------------------------------------------------------------

tab_trend, tab_prov, tab_reason, tab_incident, tab_invest = st.tabs(
    ["Trends", "Providers", "Failure reasons", "Incidents & timeline", "Investigate"])

series = call("timeseries", filters, unit, True)["rows"]
events = call("events", filters)["rows"]

with tab_trend:
    if not series:
        st.info("No payments match these filters.")
    else:
        st.plotly_chart(charts.failure_rate_trend(series, events, title=f"Failure rate per {UNIT_LABEL[unit]}, by provider"),
                        width="stretch")
        st.caption("Dotted lines: our deployments. Shaded spans: provider status notices. "
                   "Lines appearing together in time are a correlation, not proof of cause. "
                   f"Points based on fewer than {charts.MIN_POINT_ATTEMPTS} payments are left as gaps (see the table view).")
        st.plotly_chart(charts.volume_trend(series, title=f"Payment attempts per {UNIT_LABEL[unit]}"), width="stretch")
        with st.expander("Table view"):
            st.dataframe(pd.DataFrame(series), width="stretch", hide_index=True)

with tab_prov:
    rows = call("breakdown", filters, "provider")["rows"]
    if rows:
        st.plotly_chart(charts.provider_excess_bar(rows), width="stretch")
        table = pd.DataFrame(rows)[["provider_code", "attempts_current", "failures_current", "rate_baseline",
                                    "rate_current", "change_pct_points", "excess_failures", "contribution_share",
                                    "p_value", "significant"]]
        st.dataframe(
            table, width="stretch", hide_index=True,
            column_config={
                "provider_code": "Provider", "attempts_current": "Attempts", "failures_current": "Failures",
                "rate_baseline": st.column_config.NumberColumn("Rate before", format="percent"),
                "rate_current": st.column_config.NumberColumn("Rate now", format="percent"),
                "change_pct_points": st.column_config.NumberColumn("Change (pts)", format="%+.2f"),
                "excess_failures": st.column_config.NumberColumn("Excess failures", format="%+.1f"),
                "contribution_share": st.column_config.NumberColumn("Share of rise", format="percent"),
                "p_value": st.column_config.NumberColumn("p-value", format="%.4f"),
                "significant": "Significant (p < 0.01)",
            },
        )
        st.caption("Excess failures = failures now − attempts now × previous failure rate. "
                   "A significance test says a change is larger than random variation, not why it happened.")
    else:
        st.info("No payments match these filters.")

with tab_reason:
    rows = call("failure_reasons", filters)["rows"]
    if rows:
        st.plotly_chart(charts.reasons_bar(rows), width="stretch")
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("No failures match these filters.")

with tab_incident:
    found = call("anomalies", filters)["rows"]
    st.subheader(f"Detected anomaly windows: {len(found)}")
    st.caption("Hours where a provider failed far more often than in its own previous week (z ≥ 4), "
               "merged into windows. See docs/analytics_methods.md.")
    for w in found:
        with st.container(border=True):
            st.markdown(f"**{w['segment']}** · {w['start'][:16].replace('T', ' ')} → {w['end'][:16].replace('T', ' ')} UTC")
            a, b, c, d = st.columns(4)
            a.metric("Failure rate before", f"{w['baseline_rate']:.2%}")
            b.metric("During window", f"{w['window_rate']:.2%}")
            c.metric("Excess failures", f"{w['excess_failures']:,.0f}")
            d.metric("Worst hour", f"{w['peak_hour_rate']:.1%}")
            for e in w["related_events"]:
                st.markdown(f"- `{e['event_id']}` {e['description']}: {e['relationship']}")
    st.subheader("Timeline: deployments and provider notices")
    st.dataframe(pd.DataFrame(events), width="stretch", hide_index=True)

with tab_invest:
    st.subheader("Ask an investigation question")
    st.caption("The agents compute every number with SQL and tested code. The local LLM only reads your question "
               "and writes the report from the computed findings; the Verification agent checks every sentence. "
               "If the LLM is off or its report fails verification, you get a template report, labelled as such.")
    question = st.text_area("Question", value=EXAMPLE_QUESTION, height=100, max_chars=2000)
    use_llm = st.toggle("Use the local LLM", value=health["llm_mode"] == "ollama",
                        help="Off: deterministic plan (last complete week) and a template report. Faster.")
    if st.button("Run investigation", type="primary"):
        with st.spinner("Agents are investigating" + (" and the local LLM is writing the report (can take a minute)..." if use_llm else "...")):
            try:
                result = api().investigate(question, use_llm=use_llm)
            except ApiError as exc:
                st.error(str(exc))
                result = None
        if result:
            st.session_state.setdefault("history", []).insert(0, result)

    history = st.session_state.get("history", [])
    if history:
        latest = history[0]
        r = latest["report"]
        (st.success if latest["mode"] == "llm" else st.warning)(latest["mode_reason"])
        a, b, c, d = st.columns(4)
        a.metric("Status", latest["status"])
        b.metric("Time", f"{latest['duration_ms'] / 1000:.1f} s")
        c.metric("Tool calls", len(r.get("tool_calls", [])))
        d.metric("LLM calls", len(r.get("llm_calls", [])))
        if r.get("plan"):
            st.caption("Plan: " + r["plan"]["interpretation"])

        if latest["mode"] == "llm" and r.get("narrative"):
            n = r["narrative"]
            cite = lambda s: f"{s['text']} " + " ".join(f"`{c}`" for c in s["cites"])
            st.markdown("#### Summary\n" + "\n".join(f"- {cite(s)}" for s in n["summary"]))
            for sec in n["sections"]:
                if sec["sentences"]:
                    st.markdown(f"#### {sec['title']}\n" + "\n".join(f"- {cite(s)}" for s in sec["sentences"]))
            if n["next_steps"]:
                st.markdown("#### Next steps\n" + "\n".join(f"- {cite(s)}" for s in n["next_steps"]))
            st.markdown("#### What remains uncertain\n" + "\n".join(f"- {u}" for u in r["uncertainties"]))
            st.caption("Grey tags are the IDs of the findings each sentence is based on; see 'Findings and evidence'.")
        else:
            st.code(latest["text"], language=None)

        v = r.get("verification")
        if v:
            with st.expander(f"Verification: {v['sentences_kept']} of {v['sentences_total']} LLM sentences kept, "
                             f"{sum(c['status'] == 'fail' for c in v['checks'])} checks failed"):
                st.dataframe(pd.DataFrame(v["checks"]), width="stretch", hide_index=True)
                for x in v["removed"]:
                    st.markdown(f"- Removed: _{x['text']}_ ({'; '.join(x['reasons'])})")
        with st.expander("Findings and evidence"):
            st.dataframe(pd.DataFrame([{"id": f["finding_id"], "agent": f["agent"], "kind": f["kind"], "statement": f["statement"],
                                        "tool calls": ", ".join(f["evidence"]["tool_call_ids"]),
                                        "sample transactions": ", ".join(f["evidence"]["sample_transaction_ids"])}
                                       for f in r["findings"]]), width="stretch", hide_index=True)
        with st.expander("Hypotheses (ranked by evidence, none proven)"):
            for h in r["hypotheses"]:
                st.markdown(f"**{h['rank']}. {h['title']}**: score {h['score']}, {h['status']}")
                st.markdown("\n".join([f"- ✅ {s}" for s in h["supporting"]] + [f"- ❌ {s}" for s in h["contradicting"]]
                                      + [f"- 🔎 Next: {s}" for s in h["next_checks"]]))
        with st.expander("Agent execution trace"):
            st.dataframe(pd.DataFrame([{"step": t["agent"], "status": t["status"], "ms": t["duration_ms"],
                                        "tool calls": ", ".join(t["tool_call_ids"]), "message": t["message"]}
                                       for t in r["trace"]]), width="stretch", hide_index=True)
            if r.get("llm_calls"):
                st.dataframe(pd.DataFrame(r["llm_calls"]), width="stretch", hide_index=True)
            st.dataframe(pd.DataFrame(r["tool_calls"]), width="stretch", hide_index=True)
        st.markdown("**This browser session's investigations** (saved history arrives in Phase 6)")
        st.dataframe(pd.DataFrame([{"started_at": h["started_at"], "id": h["investigation_id"], "mode": h["mode"],
                                    "status": h["status"], "duration_ms": h["duration_ms"], "question": h["question"][:80]}
                                   for h in history]), width="stretch", hide_index=True)
