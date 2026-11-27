"""Runs the offline (no-LLM) eval suites as part of the normal test run, so
a change that makes the plain code save wrong data fails CI."""

from __future__ import annotations

from pathlib import Path

from evals.providers import CountingProvider, NoLLMProvider
from evals.run import load_scenarios, run_parser_scenario, run_reconcile_scenario


async def test_parser_eval_never_saves_wrong_data():
    provider = CountingProvider(NoLLMProvider())
    results = [await run_parser_scenario(s, provider) for s in load_scenarios("parser")]
    unsafe = [r.scenario_id for r in results if r.unsafe]
    assert unsafe == []


async def test_reconcile_eval_without_llm_never_saves_wrong_data(tmp_path: Path):
    provider = CountingProvider(NoLLMProvider())
    results = [await run_reconcile_scenario(s, provider, "pipeline", tmp_path) for s in load_scenarios("reconcile")]
    unsafe = [r.scenario_id for r in results if r.unsafe]
    assert unsafe == []
