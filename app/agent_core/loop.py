"""The agent loop.

    goal → model → tool calls → (policy check) → tool results → model → ... → answer

Stops when the model answers without calling a tool, or when a limit is hit
(steps, tokens). Every model reply and tool call is saved as an AgentStep,
so any run can be replayed and debugged later.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core import policy
from app.agent_core.prompts import PROMPT_VERSION
from app.agent_core.tools import Tool, ToolContext
from app.config import settings
from app.llm.base import ChatMessage, LLMDecisionError, LLMProvider, ToolCall
from app.models import AgentRun, AgentStep
from app.timeutil import local_today

logger = logging.getLogger(__name__)

# How much of a tool's args/result to keep in the trace.
_TRACE_TEXT_LIMIT = 2000


@dataclass
class AgentResult:
    run_id: UUID | None
    # 'done' | 'failed' | 'stopped' | 'over_daily_limit'
    status: str
    text: str


def _short_json(value: object) -> str:
    return json.dumps(value, default=str)[:_TRACE_TEXT_LIMIT]


async def _runs_today(session: AsyncSession, user_id: UUID) -> int:
    count_query = (
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.user_id == user_id, AgentRun.run_date == local_today())
    )
    return (await session.execute(count_query)).scalar_one()


async def run_agent(
    *,
    session: AsyncSession,
    provider: LLMProvider,
    ctx: ToolContext,
    trigger: str,
    system: str,
    goal: str,
    tools: list[Tool],
) -> AgentResult:
    """Run the agent until it answers or hits a limit."""
    if await _runs_today(session, ctx.user_id) >= settings.agent_max_runs_per_day:
        logger.warning("user=%s: daily agent run limit reached", ctx.user_id)
        return AgentResult(run_id=None, status="over_daily_limit", text="")

    run = AgentRun(
        user_id=ctx.user_id,
        trigger=trigger,
        goal=goal[:_TRACE_TEXT_LIMIT],
        status="running",
        steps=0,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        model=settings.llm_model,
        prompt_version=PROMPT_VERSION,
        run_date=local_today(),
    )
    session.add(run)
    await session.commit()
    run_id = run.id  # keep a plain copy — a rollback in a tool expires the ORM object

    tools_by_name = {tool.name: tool for tool in tools}
    tool_specs = [tool.spec() for tool in tools]
    messages = [ChatMessage(role="user", text=goal)]
    steps: list[AgentStep] = []
    input_tokens = 0
    output_tokens = 0
    run_started = time.perf_counter()
    status = "stopped"  # changed below if the run ends normally
    final_text = ""

    for _ in range(settings.agent_max_steps):
        started = time.perf_counter()
        try:
            turn = await provider.chat_with_tools(system, messages, tool_specs)
        except LLMDecisionError as exc:
            logger.warning("run=%s: model call failed: %s", run_id, exc)
            status = "failed"
            break
        except Exception:
            # Any other SDK/network error: same outcome, never crash the caller.
            logger.exception("run=%s: unexpected error calling the model", run_id)
            status = "failed"
            break
        input_tokens += turn.input_tokens
        output_tokens += turn.output_tokens
        steps.append(
            AgentStep(
                run_id=run_id,
                step_no=len(steps) + 1,
                kind="model",
                result=_short_json({"text": turn.text, "tool_calls": [c.name for c in turn.tool_calls]}),
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        messages.append(ChatMessage(role="assistant", turn=turn))

        if not turn.tool_calls:
            status = "done"
            final_text = turn.text
            break

        for call in turn.tool_calls:
            started = time.perf_counter()
            result, verdict = await _run_tool_call(call, tools_by_name, ctx)
            logger.info("run=%s: tool %s -> %s", run_id, call.name, verdict)
            steps.append(
                AgentStep(
                    run_id=run_id,
                    step_no=len(steps) + 1,
                    kind="tool",
                    tool_name=call.name[:60],  # fits the column even if the model invents a long name
                    args=_short_json(call.args),
                    result=_short_json(result),
                    verdict=verdict,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            messages.append(ChatMessage(role="tool", tool_call=call, result=result))

        if input_tokens + output_tokens > settings.agent_max_tokens_per_run:
            logger.warning("run=%s: token limit reached", run_id)
            break

    # Save the trace.
    saved_run = await session.get(AgentRun, run_id)
    assert saved_run is not None
    saved_run.status = status
    saved_run.final_text = final_text
    saved_run.steps = len(steps)
    saved_run.input_tokens = input_tokens
    saved_run.output_tokens = output_tokens
    saved_run.latency_ms = int((time.perf_counter() - run_started) * 1000)
    session.add_all(steps)
    await session.commit()
    logger.info(
        "run=%s: %s after %d steps (%d in / %d out tokens)", run_id, status, len(steps), input_tokens, output_tokens
    )
    return AgentResult(run_id=run_id, status=status, text=final_text)


async def _run_tool_call(call: ToolCall, tools_by_name: dict[str, Tool], ctx: ToolContext) -> tuple[dict, str]:
    """Run one tool call. Returns (result for the model, verdict for the trace).
    Never raises: every problem goes back to the model as an error result."""
    tool = tools_by_name.get(call.name)
    if tool is None:
        return {"error": f"there is no tool called {call.name}"}, "deny"

    try:
        args = tool.args_model.model_validate(call.args)
    except ValidationError as exc:
        return {"error": f"invalid arguments: {exc.errors()[:3]}"}, "deny"

    try:
        if tool.kind == "write":
            verdict = await policy.check(tool.name, args, ctx)
            if verdict.decision == "deny":
                return {"error": f"not allowed: {verdict.reason}"}, "deny"
            if verdict.decision == "ask":
                if tool.ask_instead is None:
                    return {"error": f"needs the user's approval: {verdict.reason}"}, "ask"
                return await tool.ask_instead(ctx, args, verdict.reason), "ask"
        return await tool.run(ctx, args), "allow"
    except Exception:
        logger.exception("tool %s failed", call.name)
        # A failed write leaves the session unusable until rolled back. The
        # rollback expires every loaded object, so reload the one we use.
        await ctx.session.rollback()
        if ctx.transaction is not None:
            await ctx.session.refresh(ctx.transaction)
        return {"error": "the tool failed — try another way or ask the user"}, "error"
