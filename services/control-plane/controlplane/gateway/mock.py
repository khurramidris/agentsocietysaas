"""Deterministic mock LLM speaking the OpenAI and Anthropic wire formats.

Used for tests, CI and keyless demos (``CP_ENABLE_MOCK_PROVIDER``). Replies are
derived from the request so the platform can be exercised end to end:

* when tools are offered, it calls a terminal tool (``finish`` & co.) with
  arguments synthesised from the tool's JSON schema - enough for agentsociety2's
  PersonAgent tool loop to complete steps and answer questions;
* agentsociety2's CodeGenRouter prompt gets valid environment code that ends with
  ``results['status'] = 'success'``;
* the society helper's XML protocol gets a ``<plan>``/``<result>`` document;
* Anthropic requests containing ``[mock:bash] <command>`` and offering the ``Bash``
  tool get a ``tool_use`` call, so Claude Code's agent loop (tool execution inside
  the sandbox) is exercised; the follow-up summarises the tool result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

TERMINAL_TOOL_NAMES = ("finish", "final_answer", "answer", "done", "submit", "respond", "complete")
MOCK_BASH = re.compile(r"\[mock:bash\]\s*(.+)", re.IGNORECASE)

CODEGEN_REPLY = (
    'print("The environment processed the request successfully (mock model).")\n'
    'results["status"] = "success"'
)


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("text", "input_text"):
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(_content_text(block.get("content")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# --------------------------------------------------------------- JSON schema


def synthesize(schema: dict[str, Any] | None, root: dict[str, Any] | None = None, name: str = "", depth: int = 0) -> Any:
    """Smallest plausible instance of a JSON schema (required properties only)."""
    if not isinstance(schema, dict) or depth > 8:
        return None
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        target = (root.get("$defs") or root.get("definitions") or {}).get(ref)
        return synthesize(target, root, name, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema and schema["default"] not in (None, [], {}):
        return schema["default"]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            options = [o for o in schema[key] if isinstance(o, dict) and o.get("type") != "null"]
            if options:
                return synthesize(options[0], root, name, depth + 1)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required")
        keys = required if required is not None else list(props)
        return {k: synthesize(props.get(k, {}), root, k, depth + 1) for k in keys}
    if typ == "array":
        count = max(int(schema.get("minItems") or 0), 1)
        return [synthesize(schema.get("items") or {"type": "string"}, root, name, depth + 1) for _ in range(count)]
    if typ == "integer":
        lo = schema.get("minimum", schema.get("exclusiveMinimum", 0))
        return int(lo) + (1 if "exclusiveMinimum" in schema else 0)
    if typ == "number":
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2
        return float(lo) if lo is not None else 0.5
    if typ == "boolean":
        return False
    text = f"Mock {name.replace('_', ' ') or 'value'} (deterministic test model)."
    min_len = int(schema.get("minLength") or 0)
    if len(text) < min_len:
        text = text + " " + "x" * (min_len - len(text))
    max_len = schema.get("maxLength")
    return text[: int(max_len)] if max_len else text


# ----------------------------------------------------------------- reply logic


def _plain_reply(all_text: str, last_user: str) -> str:
    if "# Code Generation Task" in all_text:
        return CODEGEN_REPLY
    if "Now create the plan as XML" in all_text or ("<plan>" in all_text and "<direct_answer>" in all_text):
        return (
            "<plan>\n  <direct_answer>true</direct_answer>\n"
            f"  <answer>Mock answer {_digest(last_user)}: the simulated population responded as expected.</answer>\n"
            "</plan>"
        )
    if "Generate the final answer as XML" in all_text or "<result>" in all_text:
        return f"<result>\n  <answer>Mock answer {_digest(last_user)}.</answer>\n</result>"
    return f"Mock response {_digest(last_user)}: configure a real model provider to get real answers."


def _pick_terminal_tool(tool_names: list[str]) -> str | None:
    for preferred in TERMINAL_TOOL_NAMES:
        if preferred in tool_names:
            return preferred
    return None


# ----------------------------------------------------------------- OpenAI


def openai_chat(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return (chat.completion response, usage) for an OpenAI-format request."""
    messages = body.get("messages") or []
    all_text = "\n".join(_content_text(m.get("content")) for m in messages if isinstance(m, dict))
    last_user = next((_content_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    tools = [t.get("function", {}) for t in (body.get("tools") or []) if isinstance(t, dict)]
    by_name = {t.get("name"): t for t in tools if t.get("name")}
    message: dict[str, Any] = {"role": "assistant", "content": None}
    finish_reason = "stop"
    terminal = _pick_terminal_tool(list(by_name))
    if terminal:
        args = synthesize(by_name[terminal].get("parameters") or {"type": "object", "properties": {}})
        message["tool_calls"] = [
            {"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function", "function": {"name": terminal, "arguments": json.dumps(args)}}
        ]
        finish_reason = "tool_calls"
        out_text = json.dumps(args)
    else:
        rf = body.get("response_format") or {}
        if rf.get("type") == "json_schema":
            out_text = json.dumps(synthesize((rf.get("json_schema") or {}).get("schema")))
        elif rf.get("type") == "json_object":
            out_text = "{}"
        else:
            out_text = _plain_reply(all_text, last_user)
        message["content"] = out_text
    usage = {"prompt_tokens": estimate_tokens(all_text), "completion_tokens": estimate_tokens(out_text)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    response = {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }
    return response, usage


async def openai_chat_stream(response: dict[str, Any], usage: dict[str, int]) -> AsyncIterator[bytes]:
    """Stream a response built by :func:`openai_chat` as chat.completion.chunk events."""
    choice = response["choices"][0]
    base = {"id": response["id"], "object": "chat.completion.chunk", "created": response["created"], "model": response["model"]}
    delta: dict[str, Any] = {"role": "assistant"}
    if choice["message"].get("tool_calls"):
        delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(choice["message"]["tool_calls"])]
    else:
        delta["content"] = choice["message"]["content"]
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n".encode()
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': choice['finish_reason']}]})}\n\n".encode()
    yield f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n".encode()
    yield b"data: [DONE]\n\n"


def embeddings(body: dict[str, Any], dims: int = 1024) -> tuple[dict[str, Any], dict[str, int]]:
    inputs = body.get("input")
    items = inputs if isinstance(inputs, list) else [inputs]
    dims = int(body.get("dimensions") or dims)
    data = []
    total = 0
    for i, item in enumerate(items):
        text = item if isinstance(item, str) else json.dumps(item)
        total += estimate_tokens(text)
        seed = hashlib.sha256(text.encode()).digest()
        raw = [((seed[j % len(seed)] + j * 31) % 255) / 127.5 - 1.0 for j in range(dims)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        data.append({"object": "embedding", "index": i, "embedding": [v / norm for v in raw]})
    usage = {"prompt_tokens": total, "total_tokens": total}
    return {"object": "list", "data": data, "model": body.get("model", "mock-embedding"), "usage": usage}, usage


# ---------------------------------------------------------------- Anthropic


def anthropic_message(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    system = _content_text(body.get("system"))
    all_text = system + "\n" + "\n".join(_content_text(m.get("content")) for m in messages if isinstance(m, dict))
    # Claude Code appends mid-conversation ``system`` messages after the user turn.
    turns = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    last = turns[-1] if turns else {}
    last_content = last.get("content")
    tool_names = [t.get("name") for t in (body.get("tools") or []) if isinstance(t, dict)]
    content: list[dict[str, Any]]
    stop_reason = "end_turn"
    tool_results = [b for b in last_content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(last_content, list) else []
    last_text = _content_text(last_content)
    match = MOCK_BASH.search(last_text) if last.get("role") == "user" and not tool_results else None
    if tool_results:
        result_text = _content_text(tool_results[0].get("content"))
        content = [{"type": "text", "text": f"Mock agent: the command finished. Output:\n{result_text[:2000]}"}]
    elif match and "Bash" in tool_names:
        command = match.group(1).strip().splitlines()[0]
        content = [
            {"type": "text", "text": "Running the requested command."},
            {"type": "tool_use", "id": f"toolu_mock_{uuid.uuid4().hex[:12]}", "name": "Bash", "input": {"command": command, "description": "Run requested command"}},
        ]
        stop_reason = "tool_use"
    else:
        content = [{"type": "text", "text": _plain_reply(all_text, last_text)}]
    out_text = json.dumps(content)
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "mock"),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": estimate_tokens(all_text),
            "output_tokens": estimate_tokens(out_text),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def anthropic_message_stream(msg: dict[str, Any]) -> AsyncIterator[bytes]:
    """Stream a message built by :func:`anthropic_message` as Messages API SSE events."""
    usage = msg["usage"]
    start = {**msg, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 1}}
    yield _sse("message_start", {"type": "message_start", "message": start})
    for index, block in enumerate(msg["content"]):
        if block["type"] == "text":
            yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            text = block["text"]
            for i in range(0, len(text), 40):
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text[i : i + 40]}})
        else:
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}},
            )
            yield _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}},
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    yield _sse(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": usage["output_tokens"]}},
    )
    yield _sse("message_stop", {"type": "message_stop"})


def anthropic_count_tokens(body: dict[str, Any]) -> dict[str, int]:
    text = _content_text(body.get("system")) + "\n".join(_content_text(m.get("content")) for m in body.get("messages") or [])
    return {"input_tokens": estimate_tokens(text + json.dumps(body.get("tools") or []))}
