"""Deterministic analytics: every number the application reports is computed here.

Design rule: the LLM never calculates. Agents call these functions and only
explain their results.

Two layers:
* queries.py  - SQL against the read-only account; returns small tables (DataFrames).
* everything else - pure pandas/numpy functions on those tables, so each
  calculation can be tested with hand-made inputs and hand-computed answers.
"""
