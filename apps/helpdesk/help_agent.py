"""The help assistant = the shared agent loop (apps/ai_assistant/agent.py) with this profile."""
from apps.ai_assistant import agent, llm

from . import help_tools
from .help_prompts import SYSTEM_PROMPT


def _fallback(changes):
    for change in changes:
        if change.get("ticket_number"):
            return f"I've raised ticket {change['ticket_number']}. A support agent will reply here and by email."
    return "I couldn't answer that. Could you tell me a bit more, or would you like me to raise a support ticket?"


HELP = agent.Profile(
    name="help_assistant",
    system_prompt=SYSTEM_PROMPT,
    tool_definitions=help_tools.tool_definitions,
    run_tool=help_tools.run_tool,
    model=llm.help_model_name,
    fallback=_fallback,
)


def run(ctx, history, message):
    return agent.run(ctx, history, message, profile=HELP)
