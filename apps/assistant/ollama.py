"""A small client for a local Ollama server (urllib only, no new dependency)."""
import json
import socket
import urllib.error
import urllib.request

from django.conf import settings


class OllamaError(Exception):
    """A problem talking to the model, worded for the person using the assistant."""


# Never go through an HTTP proxy from the environment: the model is on this computer
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NOT_RUNNING = "Ollama isn't running on this computer. Start it (ollama serve) and try again."


def chat(messages, tools=None):
    """POST /api/chat (no streaming) and return the model's message dict."""
    model = settings.ASSISTANT_OLLAMA_MODEL
    timeout = settings.ASSISTANT_OLLAMA_TIMEOUT
    options = {"temperature": 0.1, "num_ctx": getattr(settings, "ASSISTANT_OLLAMA_NUM_CTX", 8192)}
    payload = {"model": model, "messages": messages, "stream": False, "options": options}
    if tools:
        payload["tools"] = tools
    request = urllib.request.Request(
        settings.ASSISTANT_OLLAMA_URL.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if error.code == 404 or "not found" in detail.lower():
            raise OllamaError(f"The model {model} isn't installed. Run: ollama pull {model}") from error
        raise OllamaError(f"Ollama returned an error ({error.code}). {detail}".strip()) from error
    except (TimeoutError, socket.timeout) as error:
        raise OllamaError(f"The model took longer than {timeout} seconds to answer. Try a shorter question.") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise OllamaError(f"The model took longer than {timeout} seconds to answer.") from error
        raise OllamaError(_NOT_RUNNING) from error
    except OSError as error:
        raise OllamaError(_NOT_RUNNING) from error
    except ValueError as error:
        raise OllamaError("Ollama sent a reply that could not be read.") from error

    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict):
        raise OllamaError("Ollama sent an empty reply.")
    return message
