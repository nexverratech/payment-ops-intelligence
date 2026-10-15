"""Turn the assembled investigation into a readable text report WITHOUT an LLM.

Every sentence comes from the agents' code-built findings. In Phase 5b this
becomes the clearly labelled fallback used when the local LLM is unavailable.
"""

from __future__ import annotations

LABEL = "TEMPLATE REPORT (assembled by code, no LLM)"


def render(result: dict) -> str:
    plan = result.get("plan") or {}
    out = [LABEL, "=" * len(LABEL)]
    if plan:
        out += [f"Question: {plan['question']}",
                f"Scope:    {plan['current_start'][:10]} to {plan['current_end'][:10]} (end exclusive) vs the week before",
                f"Plan:     {plan['interpretation']}"]
    out += [f"Status:   {result['status']}", "", "SUMMARY", f"  {result['headline']}", "", "FINDINGS"]
    for f in result["findings"]:
        if f["kind"] == "hypothesis":
            continue
        cites = ", ".join(f["evidence"]["tool_call_ids"]) or "none"
        out.append(f"  [{f['finding_id']}] {f['statement']}")
        out.append(f"        evidence: tool calls {cites}"
                   + (f"; transactions {', '.join(f['evidence']['sample_transaction_ids'])}" if f["evidence"]["sample_transaction_ids"] else ""))
    if result.get("hypotheses"):
        out += ["", "HYPOTHESES (ranked by evidence, none proven)"]
        for h in result["hypotheses"]:
            out.append(f"  {h['rank']}. {h['title']}: score {h['score']}, {h['status']}")
            out += [f"       + {s}" for s in h["supporting"]]
            out += [f"       - {s}" for s in h["contradicting"]]
            out += [f"       next: {s}" for s in h["next_checks"][:2]]
    out += ["", "WHAT REMAINS UNCERTAIN"] + [f"  - {u}" for u in result["uncertainties"]]
    if result.get("errors"):
        out += ["", "ERRORS"] + [f"  - {e['agent']}: {e['error_type']}: {e['message']}" for e in result["errors"]]
    return "\n".join(out)


def render_trace(result: dict) -> str:
    rows = ["AGENT TRACE", f"  {'step':<12} {'status':<8} {'ms':>6}  tools"]
    calls = {c["call_id"]: c for c in result.get("tool_calls", [])}
    for t in result.get("trace", []):
        tools = ", ".join(f"{calls[i]['tool']}" for i in t["tool_call_ids"] if i in calls)
        status = t["status"] + ("*" if t.get("from_checkpoint") else "")
        rows.append(f"  {t['agent']:<12} {status:<8} {t['duration_ms']:>6}  {tools}{'  (' + t['message'] + ')' if t['message'] else ''}")
    for c in result.get("llm_calls", []):
        rows.append(f"  llm:{c['purpose']:<8} {c['status']:<8} {c['duration_ms']:>6}  {c['model']} (attempt {c['attempt']}, "
                    f"{c.get('prompt_tokens') or 0} prompt / {c.get('output_tokens') or 0} output tokens)")
    rows.append(f"  total {result.get('duration_ms', 0)} ms, {len(calls)} tool calls, {len(result.get('llm_calls', []))} LLM calls")
    if any(t.get("from_checkpoint") for t in result.get("trace", [])):
        rows.append("  * reused from a checkpoint of an earlier run (time shown is the original run's)")
    return "\n".join(rows)
