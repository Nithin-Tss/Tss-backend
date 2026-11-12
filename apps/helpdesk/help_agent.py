"""The help assistant = the shared agent loop (apps/ai_assistant/agent.py) with this profile."""
from apps.ai_assistant import agent, llm

from . import help_tools
from .help_prompts import SYSTEM_PROMPT


def _fallback(changes):
    for change in changes:
        if change.get("ticket_number"):
            return "I've passed this chat to our team. One of our executives will contact you."
    return "I couldn't answer that. Could you tell me a bit more, or I can pass this chat to one of our executives."


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
