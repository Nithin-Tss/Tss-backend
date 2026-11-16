"""The LLM client: the openai package pointed at OpenRouter. Errors become LLMError with a message fit for the merchant."""
import logging

import openai
from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Something went wrong talking to the model. str(error) is safe to show to the merchant."""


def model_name():
    return settings.OPENROUTER_MODEL


def help_model_name():
    """The help assistant's model: HELP_ASSISTANT_MODEL, else OPENROUTER_MODEL."""
    return getattr(settings, "HELP_ASSISTANT_MODEL", "") or settings.OPENROUTER_MODEL


def _client():
    key = settings.OPENROUTER_API_KEY
    if not key:
        raise LLMError("The AI assistant isn't set up on this server yet.")
    # max_retries=0: the agent loop decides about retries, and the request must stay inside the web timeout
    return openai.OpenAI(
        api_key=key, base_url=settings.OPENROUTER_BASE_URL, timeout=settings.OPENROUTER_TIMEOUT, max_retries=0,
    )


def chat(messages, tools=None, model=None):
    """One model call. Returns the reply message (has .content and .tool_calls). `model` overrides OPENROUTER_MODEL."""
    client = _client()
    kwargs = {"model": model or model_name(), "messages": messages}
    if tools:
        kwargs["tools"] = tools
    try:
        response = client.chat.completions.create(**kwargs)
    except openai.RateLimitError as error:
        logger.warning("ai_assistant: model=%s rate limited (429)", model_name())
        raise LLMError("The AI service is busy right now. Please try again in a minute.") from error
    except openai.APITimeoutError as error:
        logger.warning("ai_assistant: model=%s timed out", model_name())
        raise LLMError("The AI service took too long to answer. Please try again.") from error
    except openai.AuthenticationError as error:
        logger.error("ai_assistant: model=%s rejected the API key (401)", model_name())
        raise LLMError("The AI assistant isn't set up correctly on this server.") from error
    except openai.APIConnectionError as error:
        logger.warning("ai_assistant: model=%s connection failed", model_name())
        raise LLMError("Couldn't reach the AI service. Please try again.") from error
    except openai.APIStatusError as error:
        logger.warning("ai_assistant: model=%s API error status=%s", model_name(), error.status_code)
        raise LLMError("The AI service returned an error. Please try again.") from error
    choices = getattr(response, "choices", None)
    if not choices:  # OpenRouter sometimes answers 200 with an error body and no choices
        logger.warning("ai_assistant: model=%s returned no choices", model_name())
        raise LLMError("The AI service sent an empty answer. Please try again.")
    return choices[0].message


def embedding_model_name():
    return getattr(settings, "EMBEDDING_MODEL", "openai/text-embedding-3-small")


def embed(texts, model=None):
    """Embed a list of texts. Returns one list of floats per text, in order."""
    if not texts:
        return []
    client = _client()
    try:
        response = client.embeddings.create(model=model or embedding_model_name(), input=list(texts))
    except openai.RateLimitError as error:
        raise LLMError("The AI service is busy right now. Please try again in a minute.") from error
    except openai.APITimeoutError as error:
        raise LLMError("The AI service took too long to answer. Please try again.") from error
    except openai.AuthenticationError as error:
        logger.error("ai_assistant: embeddings rejected the API key (401)")
        raise LLMError("The AI assistant isn't set up correctly on this server.") from error
    except openai.APIConnectionError as error:
        raise LLMError("Couldn't reach the AI service. Please try again.") from error
    except openai.APIStatusError as error:
        logger.warning("ai_assistant: embeddings API error status=%s", error.status_code)
        raise LLMError("The AI service returned an error. Please try again.") from error
    data = getattr(response, "data", None)
    if not data or len(data) != len(texts):
        raise LLMError("The AI service sent an empty answer. Please try again.")
    return [list(item.embedding) for item in sorted(data, key=lambda item: item.index)]
