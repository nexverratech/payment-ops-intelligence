"""Multi-agent investigation workflow (LangGraph).

Each agent is a plain Python function: (state, tools) -> partial state update.
graph.py wires them into a LangGraph workflow. Agents never touch the database
directly: they call permission-checked tools (tools.py), which call the tested
Phase 3 analytics, and every call is recorded as evidence.
"""
