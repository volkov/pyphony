"""JSON-RPC message builders and parsers for the agent app-server protocol."""

from __future__ import annotations

import json
import threading

_counter_lock = threading.Lock()
_message_id = 0


def _next_id() -> int:
    global _message_id
    with _counter_lock:
        _message_id += 1
        return _message_id


def reset_id_counter() -> None:
    """Reset the message ID counter (for testing)."""
    global _message_id
    with _counter_lock:
        _message_id = 0


def build_initialize_request() -> dict:
    return {
        "id": _next_id(),
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "symphony", "version": "1.0"},
            "capabilities": {},
        },
    }


def build_initialized_notification() -> dict:
    return {
        "method": "initialized",
        "params": {},
    }


def build_thread_start_request(
    approval_policy: str | None = None,
    sandbox: str | None = None,
    cwd: str = "",
) -> dict:
    params: dict = {}
    if approval_policy is not None:
        params["approvalPolicy"] = approval_policy
    if sandbox is not None:
        params["sandbox"] = sandbox
    if cwd:
        params["cwd"] = cwd
    return {
        "id": _next_id(),
        "method": "thread/start",
        "params": params,
    }


def build_turn_start_request(
    thread_id: str,
    prompt_text: str,
    cwd: str = "",
    title: str = "",
    approval_policy: str | None = None,
    sandbox_policy: str | None = None,
) -> dict:
    params: dict = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt_text}],
    }
    if cwd:
        params["cwd"] = cwd
    if title:
        params["title"] = title
    if approval_policy is not None:
        params["approvalPolicy"] = approval_policy
    if sandbox_policy is not None:
        params["sandboxPolicy"] = {"type": sandbox_policy}
    return {
        "id": _next_id(),
        "method": "turn/start",
        "params": params,
    }


def parse_response(line: str) -> dict | None:
    """Parse a JSON line. Return None if not valid JSON."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_thread_id(response: dict) -> str | None:
    """Extract thread ID from a thread/start response."""
    try:
        return response["result"]["thread"]["id"]
    except (KeyError, TypeError):
        return None


def extract_turn_id(response: dict) -> str | None:
    """Extract turn ID from a turn/start response."""
    try:
        return response["result"]["turn"]["id"]
    except (KeyError, TypeError):
        return None


def is_turn_completed(msg: dict) -> bool:
    return msg.get("method") == "turn/completed"


def is_turn_failed(msg: dict) -> bool:
    return msg.get("method") == "turn/failed"


def is_turn_cancelled(msg: dict) -> bool:
    return msg.get("method") == "turn/cancelled"


def is_user_input_required(msg: dict) -> bool:
    method = msg.get("method", "")
    if method == "item/tool/requestUserInput":
        return True
    # Check for turn-level flags indicating user input is required
    params = msg.get("params", {})
    if isinstance(params, dict) and params.get("userInputRequired"):
        return True
    return False


def build_approval_response(request_id: int | str, approved: bool = True) -> dict:
    return {
        "id": request_id,
        "result": {"approved": approved},
    }


def build_tool_error_response(
    request_id: int | str, error: str = "unsupported_tool_call"
) -> dict:
    return {
        "id": request_id,
        "result": {"success": False, "error": error},
    }
