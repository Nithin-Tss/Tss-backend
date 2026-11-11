"""
The assistant's model client. chat() uses OpenRouter when OPENROUTER_API_KEY is set (the same key and model as the
AI page builder), otherwise a local Ollama server (urllib only). ASSISTANT_PROVIDER=ollama forces Ollama.
Both return the Ollama message shape the agent uses: {"content", "tool_calls": [{"function": {"name", "arguments"}}]}.
"""
import ast
import json
import re
import socket
import urllib.error
import urllib.request

from django.conf import settings


class OllamaError(Exception):
    """A problem talking to the model, worded for the person using the assistant."""


# Never go through an HTTP proxy from the environment: the model is on this computer
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NOT_RUNNING = "Ollama isn't running on this computer. Start it (ollama serve) and try again."


_TOOL_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_FUNCTION = re.compile(r"<function=([A-Za-z_][A-Za-z0-9_]*)>")
_PARAMETER = re.compile(r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>(.*?)</parameter>", re.DOTALL)


_PYTHONIC = re.compile(r"<\|tool_call_start\|>(.*?)(?:<\|tool_call_end\|>|\Z)", re.DOTALL)


def _pythonic_calls(block):
    """[name(key=value, ...), ...] written as Python: parsed with ast, values only as literals."""
    try:
        tree = ast.parse(block.strip(), mode="eval").body
    except (SyntaxError, ValueError):
        return []
    nodes = tree.elts if isinstance(tree, (ast.List, ast.Tuple)) else [tree]
    calls = []
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        try:
            args = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg}
        except (ValueError, SyntaxError):
            continue
        calls.append({"function": {"name": node.func.id, "arguments": args}})
    return calls


def text_tool_calls(content):
    """
    Some models write a tool call as text (<tool_call><function=NAME><parameter=KEY>value</parameter>...) instead of
    a real tool call. Returns (calls, content_without_those_blocks) so the agent can run them and never show the markup.
    """
    calls = []
    for block in _PYTHONIC.findall(content or ""):
        calls.extend(_pythonic_calls(block))
    for block in _TOOL_BLOCK.findall(content or ""):
        name = _FUNCTION.search(block)
        if name:
            args = {k: v.strip("\n") for k, v in _PARAMETER.findall(block)}
            calls.append({"function": {"name": name.group(1), "arguments": args}})
            continue
        try:  # {"name": "...", "arguments": {...}}
            data = json.loads(block.strip())
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                args = data.get("arguments")
                calls.append({"function": {"name": data["name"], "arguments": args if isinstance(args, dict) else {}}})
        except ValueError:
            pass
    return calls, _PYTHONIC.sub("", _TOOL_BLOCK.sub("", content or "")).strip()


def provider():
    chosen = str(getattr(settings, "ASSISTANT_PROVIDER", "") or "").lower()
    if chosen in ("ollama", "openrouter"):
        return chosen
    return "openrouter" if getattr(settings, "OPENROUTER_API_KEY", "") else "ollama"


def chat(messages, tools=None):
    """One model call through the chosen provider; returns the model's message dict."""
    if provider() == "openrouter":
        return _openrouter_chat(messages, tools)
    return _ollama_chat(messages, tools)


def _to_openai(messages):
    """Ollama-shaped messages -> OpenAI-shaped: tool calls get ids, arguments become JSON text, results name the call."""
    out, pending, counter = [], [], 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for call in m["tool_calls"]:
                fn = (call or {}).get("function") or {}
                args = fn.get("arguments")
                counter += 1
                call_id = (call or {}).get("id") or f"call_{counter}"
                pending.append(call_id)
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": fn.get("name") or "",
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
                }})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "tool_call_id": pending.pop(0) if pending else "call_0", "content": m.get("content") or ""})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _openrouter_chat(messages, tools=None):
    from apps.ai_assistant import llm

    try:
        reply = llm.chat(_to_openai(messages), tools=tools, model=getattr(settings, "ASSISTANT_OPENROUTER_MODEL", "") or None)
    except llm.LLMError as error:
        raise OllamaError(str(error)) from error
    calls = []
    for call in getattr(reply, "tool_calls", None) or []:
        raw = call.function.arguments
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except ValueError:
            args = {}
        calls.append({"id": call.id, "function": {"name": call.function.name, "arguments": args if isinstance(args, dict) else {}}})
    content = getattr(reply, "content", None) or ""
    if not calls:
        calls, content = text_tool_calls(content)
        for n, call in enumerate(calls, 1):
            call["id"] = f"text_call_{n}"
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return message


def _ollama_chat(messages, tools=None):
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
    if not message.get("tool_calls"):
        calls, content = text_tool_calls(message.get("content"))
        if calls:
            message = {**message, "content": content, "tool_calls": calls}
    return message
