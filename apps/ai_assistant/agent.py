"""The agent loop: model <-> tools until the model answers in plain text (or a cap is hit)."""
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import llm, tools
from .blocks import tool_definitions
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8   # model calls per merchant message
MAX_RETRIES = 3      # failed tool calls (bad arguments, unknown tool...) the model may correct before we stop
MAX_TOOL_RESULT_CHARS = 12000
RETRY_KINDS = ("validation", "malformed", "unknown")


def _assistant_message(message, calls):
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"}}
            for call in calls
        ],
    }


def _fallback(changes):
    if changes:
        return "I made the changes above as drafts. Preview them, then publish when you're happy."
    return "I wasn't able to do that. Could you tell me a bit more about what you'd like?"


@dataclass(frozen=True)
class Profile:
    """
    Everything that makes one assistant different from another; the loop itself is shared.
    `tool_definitions` and `model` are callables so settings and schemas are read at call time, not import time.
    `run_tool(ctx, name, raw_arguments)` returns (result, error_kind) like tools.run_tool.
    `model` returns the model id to use, or None for the default (OPENROUTER_MODEL).
    """

    name: str
    system_prompt: str
    tool_definitions: Callable
    run_tool: Callable
    model: Callable = lambda: None
    fallback: Callable = _fallback


PAGE_BUILDER = Profile(
    name="page_builder", system_prompt=SYSTEM_PROMPT, tool_definitions=tool_definitions, run_tool=tools.run_tool,
)


def _chat(profile, messages, definitions):
    model = profile.model()
    if model:
        return llm.chat(messages, definitions, model=model)
    return llm.chat(messages, definitions)


def run(ctx, history, message, profile=PAGE_BUILDER):
    """
    history: earlier turns as [{"role": "user"|"assistant", "content": ...}].
    Returns the final text reply; the changes made are in ctx.changes.
    Raises llm.LLMError only when the model could not be reached and nothing was changed.
    """
    model = profile.model() or llm.model_name()
    messages = [{"role": "system", "content": profile.system_prompt}, *history, {"role": "user", "content": message}]
    definitions = profile.tool_definitions()
    failures = calls_made = 0
    reply = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        try:
            answer = _chat(profile, messages, definitions)
        except llm.LLMError as error:
            if not ctx.changes:
                raise
            reply = f"{error} What I did before that is shown above."
            break
        calls = list(getattr(answer, "tool_calls", None) or [])
        if not calls:
            reply = (answer.content or "").strip()
            break
        messages.append(_assistant_message(answer, calls))
        for call in calls:
            calls_made += 1
            result, kind = profile.run_tool(ctx, call.function.name, call.function.arguments)
            if kind in RETRY_KINDS:
                failures += 1
                # No arguments or merchant content in the log: just enough to compare models
                logger.warning(
                    "ai_assistant: profile=%s model=%s tool=%s failure=%s attempt=%d iteration=%d",
                    profile.name, model, call.function.name, kind, failures, iteration,
                )
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS],
            })
        if failures > MAX_RETRIES:
            reply = "I couldn't get that to work after a few tries. Try rephrasing, or ask for something simpler."
            break
    else:
        reply = "That took more steps than I'm allowed to take at once. Ask me to continue and I'll pick up from here."

    logger.info(
        "ai_assistant: profile=%s model=%s iterations=%d tool_calls=%d failures=%d changes=%d",
        profile.name, model, iteration, calls_made, failures, len(ctx.changes),
    )
    return reply or profile.fallback(ctx.changes)
