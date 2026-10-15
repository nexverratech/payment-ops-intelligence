"""Run an investigation from the command line.

    python -m payops.agents.run "Why did the failure rate increase last week?"
    python -m payops.agents.run "..." --as-of 2026-09-29T00:00:00Z --json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from payops.agents.graph import run_investigation
from payops.agents.template_report import render_trace
from payops.config import get_settings
from payops.db.connection import get_readonly_engine
from payops.llm.client import OllamaClient
from payops.logging_setup import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the multi-agent payment failure investigation.")
    parser.add_argument("question", help="The investigation question, in quotes")
    parser.add_argument("--as-of", help="UTC time, e.g. 2026-09-29T00:00:00Z (default: end of the loaded data)")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--no-llm", action="store_true", help="don't use the local LLM (template report)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING")
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")).astimezone(timezone.utc) if args.as_of else None
    llm = None if args.no_llm else OllamaClient.from_settings(settings)
    if llm:
        print(f"Running with the local LLM {settings.ollama_model}; writing the report can take a minute...\n")
    result = run_investigation(get_readonly_engine(settings), args.question, as_of, llm=llm)
    print(json.dumps(result, indent=2, default=str) if args.json else result["report_text"] + "\n\n" + render_trace(result))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
