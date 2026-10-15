"""Run, list, show and resume investigations from the command line.

    python -m payops.agents.run "What went wrong with Kestrel in the week of 21 September 2026?"
    python -m payops.agents.run "Why did failures rise last week?" --no-llm
    python -m payops.agents.run --list
    python -m payops.agents.run --show <id>
    python -m payops.agents.run --resume <id>

Investigations are saved when APP_DB_PASSWORD is set (see .env.example and
`python -m payops.db.setup_history`). Press Ctrl+C during a run to stop it; it is
recorded as "interrupted" and can be resumed.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from payops.agents.graph import resume_investigation, run_investigation
from payops.agents.template_report import render_trace
from payops.config import get_settings
from payops.db.connection import get_history_engine, get_readonly_engine
from payops.history.store import HistoryError, InvestigationStore, PostgresStore
from payops.llm.client import OllamaClient
from payops.logging_setup import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the multi-agent payment failure investigation.")
    parser.add_argument("question", nargs="?", help="The investigation question, in quotes")
    parser.add_argument("--as-of", help="UTC time, e.g. 2026-09-29T00:00:00Z (default: end of the loaded data)")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--no-llm", action="store_true", help="don't use the local LLM (template report)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list saved investigations, newest first")
    group.add_argument("--show", metavar="ID", help="print a saved investigation's report")
    group.add_argument("--resume", metavar="ID", help="continue a partial or interrupted investigation")
    args = parser.parse_args(argv)
    if not (args.question or args.list or args.show or args.resume):
        parser.error("give a question, or --list, --show ID or --resume ID")

    settings = get_settings()
    configure_logging("WARNING")
    store: InvestigationStore | None = PostgresStore(get_history_engine(settings)) if settings.history_enabled else None
    if store is None and (args.list or args.show or args.resume):
        print("Investigation history is not configured: set APP_DB_PASSWORD in .env and run "
              "python -m payops.db.setup_history")
        return 1

    for value in (args.show, args.resume):
        if value is not None:
            try:
                uuid.UUID(value)
            except ValueError:
                print(f"'{value}' is not an investigation id (expected a UUID; see --list).")
                return 1
    try:
        if args.list:
            return _list(store, settings.stale_after_seconds)
        if args.show:
            record = store.get(args.show)  # type: ignore[union-attr]
            if record.result is None:
                print(f"{record.investigation_id} is {record.status}; it has no finished result yet.")
                return 1
            _print(record.result, args.json)
            return 0
        if args.resume:
            store.mark_stale_interrupted(settings.stale_after_seconds)  # type: ignore[union-attr]
            print(f"Resuming {args.resume} from its checkpoints...\n")
            result = resume_investigation(get_readonly_engine(settings), store, args.resume,  # type: ignore[arg-type]
                                          None if args.no_llm else (lambda: OllamaClient.from_settings(settings)),
                                          timeout_seconds=settings.investigation_timeout_seconds)
            _print(result, args.json)
            return 0 if result["status"] == "complete" else 2
    except HistoryError as exc:
        print(exc)
        return 1

    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")).astimezone(timezone.utc) if args.as_of else None
    llm = None if args.no_llm else OllamaClient.from_settings(settings)
    investigation_id = str(uuid.uuid4())
    if llm and not args.json:
        print(f"Running with the local LLM {settings.ollama_model}; writing the report can take a minute...\n")
    try:
        result = run_investigation(get_readonly_engine(settings), args.question, as_of, investigation_id, llm,
                                   store=store, timeout_seconds=settings.investigation_timeout_seconds)
    except KeyboardInterrupt:
        print("\nStopped." + (f" Resume with: python -m payops.agents.run --resume {investigation_id}" if store else ""))
        return 130
    _print(result, args.json)
    return 0 if result["status"] == "complete" else 2


def _print(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(result["report_text"] + "\n\n" + render_trace(result))
    if result.get("reused_steps"):
        print(f"  run {result.get('run')}: reused from checkpoints: {', '.join(result['reused_steps'])}")
    saved = result.get("persisted")
    print(f"\nInvestigation {result['investigation_id']}: " + ("saved to history." if saved else "not saved."))


def _list(store: InvestigationStore, stale_after: float) -> int:
    marked = store.mark_stale_interrupted(stale_after)
    rows = store.list(limit=20)
    if marked:
        print(f"({marked} run(s) with no heartbeat were marked interrupted.)\n")
    if not rows:
        print("No saved investigations yet.")
        return 0
    print(f"{'created (UTC)':<17} {'status':<12} {'mode':<9} {'runs':>4}  {'id':<36}  question")
    for r in rows:
        print(f"{r.created_at:%Y-%m-%d %H:%M} {r.status:<12} {r.mode or '-':<9} {r.runs:>4}  {r.investigation_id}  "
              f"{r.question[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
