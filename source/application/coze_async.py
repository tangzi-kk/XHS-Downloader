"""Asynchronous Coze workflow dispatch for short-lived webhook callers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from threading import Lock
from typing import Any

import httpx
from fastapi import BackgroundTasks, Body, Header, HTTPException

COZE_WORKFLOW_RUN_URL = "https://api.coze.cn/v1/workflow/run"
_DEFAULT_TIMEOUT_SECONDS = 300
_ACTIVE_TASK_STATUSES = {"queued", "running"}

# Uvicorn's error logger is configured by the server and emits WARNING even
# when the application logger's INFO level is not visible in Render logs.
_logger = logging.getLogger("uvicorn.error")
_running_tasks: set[str] = set()
_task_states: dict[str, dict[str, Any]] = {}
_task_states_lock = Lock()


def _task_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _redact_text(value: Any, token: str = "") -> str | None:
    text = _coerce_text(value)
    if text is None:
        return None
    if token:
        text = text.replace(token, "[REDACTED]")
    return text[:300]


def _safe_log_value(value: Any) -> str:
    text = _redact_text(value) or "-"
    return text.replace("\r", "\\r").replace("\n", "\\n")[:240]


def _log_task_event(event: str, state: dict[str, Any]) -> None:
    fields = [
        f"task_id={_safe_log_value(state.get('task_id'))}",
        f"record_id={_safe_log_value(state.get('record_id'))}",
        f"workflow_id={_safe_log_value(state.get('workflow_id'))}",
        f"status={_safe_log_value(state.get('status'))}",
    ]
    if event not in {"coze_task_queued", "coze_task_duplicate"}:
        fields.extend(
            [
                f"http_status={_safe_log_value(state.get('http_status'))}",
                f"coze_code={_safe_log_value(state.get('coze_code'))}",
                f"coze_message={_safe_log_value(state.get('coze_message'))}",
                f"execution_id={_safe_log_value(state.get('execution_id'))}",
                f"error_type={_safe_log_value(state.get('error_type'))}",
                f"error_message={_safe_log_value(state.get('error_message'))}",
            ]
        )
    _logger.warning("%s %s", event, " ".join(fields))


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    if isinstance(result.get("coze_response_fields"), list):
        result["coze_response_fields"] = list(result["coze_response_fields"])
    return result


def _get_task_state(task_id: str) -> dict[str, Any] | None:
    with _task_states_lock:
        state = _task_states.get(task_id)
        return _snapshot(state) if state is not None else None


def _new_task_state(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    return {
        "task_id": task_id,
        "status": "queued",
        "created_at": _utc_now(),
        "started_at": None,
        "finished_at": None,
        "workflow_id": payload["workflow_id"],
        "record_id": _coerce_text(parameters.get("record_id")) or "",
        "http_status": None,
        "coze_code": None,
        "coze_message": None,
        "execution_id": None,
        "debug_url": None,
        "error_type": None,
        "error_message": None,
        "coze_response_fields": None,
    }


def _register_task(
    task_id: str,
    payload: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    with _task_states_lock:
        existing = _task_states.get(task_id)
        if existing and existing.get("status") in _ACTIVE_TASK_STATUSES:
            return True, _snapshot(existing)

        state = _new_task_state(task_id, payload)
        _task_states[task_id] = state
        _running_tasks.add(task_id)
        return False, _snapshot(state)


def _mark_task_started(task_id: str) -> dict[str, Any] | None:
    should_log = False
    with _task_states_lock:
        state = _task_states.get(task_id)
        if state is None:
            return None
        if state.get("status") == "queued":
            state["status"] = "running"
            state["started_at"] = _utc_now()
            should_log = True
        snapshot = _snapshot(state)
    if should_log:
        _log_task_event("coze_task_started", snapshot)
    return snapshot


def _finish_task(
    task_id: str,
    status: str,
    **updates: Any,
) -> dict[str, Any] | None:
    with _task_states_lock:
        state = _task_states.get(task_id)
        if state is None:
            return None
        state.update(updates)
        state["status"] = status
        state["finished_at"] = _utc_now()
        _running_tasks.discard(task_id)
        snapshot = _snapshot(state)

    event = {
        "succeeded": "coze_task_succeeded",
        "failed": "coze_task_failed",
        "exception": "coze_task_exception",
    }[status]
    _log_task_event(event, snapshot)
    return snapshot


def _interpret_response(
    response: httpx.Response,
    token: str,
) -> tuple[str, dict[str, Any]]:
    http_status = response.status_code
    try:
        result = response.json()
    except ValueError:
        if 200 <= http_status < 300:
            error_message = "Coze response body is not valid JSON"
            error_type = "invalid_json"
        else:
            error_message = (
                f"Coze returned HTTP {http_status} with an invalid JSON response body"
            )
            error_type = "http_error"
        return "failed", {
            "http_status": http_status,
            "coze_code": None,
            "coze_message": None,
            "execution_id": None,
            "debug_url": None,
            "error_type": error_type,
            "error_message": error_message,
            "coze_response_fields": [],
        }

    if not isinstance(result, dict):
        return "failed", {
            "http_status": http_status,
            "coze_code": None,
            "coze_message": None,
            "execution_id": None,
            "debug_url": None,
            "error_type": "invalid_response",
            "error_message": "Coze response JSON must be an object",
            "coze_response_fields": [],
        }

    code = result.get("code")
    message = result.get("msg")
    if message is None:
        message = result.get("message")
    coze_message = _redact_text(message, token)
    execute_id = result.get("execute_id")
    if execute_id is None:
        execute_id = result.get("execution_id")
    response_fields = sorted(str(key) for key in result)
    common = {
        "http_status": http_status,
        "coze_code": code,
        "coze_message": coze_message,
        "execution_id": _coerce_text(execute_id),
        "debug_url": _coerce_text(result.get("debug_url")),
        "coze_response_fields": response_fields,
    }

    if not 200 <= http_status < 300:
        return "failed", {
            **common,
            "error_type": "http_error",
            "error_message": coze_message or f"Coze returned HTTP {http_status}",
        }

    if code is None:
        return "failed", {
            **common,
            "error_type": "invalid_response",
            "error_message": coze_message or "Coze response did not include code",
        }

    if code not in (0, "0"):
        return "failed", {
            **common,
            "error_type": "coze_business_error",
            "error_message": coze_message or f"Coze returned business code {code}",
        }

    return "succeeded", {
        **common,
        "error_type": None,
        "error_message": None,
    }


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")

    workflow_id = str(payload.get("workflow_id") or "").strip()
    parameters = payload.get("parameters")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required")
    if not isinstance(parameters, dict):
        raise HTTPException(status_code=400, detail="parameters must be an object")

    return {
        "workflow_id": workflow_id,
        "parameters": parameters,
    }


def _read_token() -> str:
    token = os.getenv("COZE_API_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="Missing COZE_API_TOKEN")
    return token


def _authorize(authorization: str | None, token: str) -> None:
    expected = f"Bearer {token}"
    supplied = (authorization or "").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _run_coze_workflow(
    payload: dict[str, Any],
    token: str,
    task_id: str,
) -> None:
    _mark_task_started(task_id)
    try:
        timeout_seconds = int(
            os.getenv("COZE_WORKFLOW_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
        )
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                COZE_WORKFLOW_RUN_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        status, updates = _interpret_response(response, token)
        _finish_task(task_id, status, **updates)
    except Exception as error:
        _finish_task(
            task_id,
            "exception",
            error_type=type(error).__name__,
            error_message=_redact_text(error, token),
        )
    finally:
        with _task_states_lock:
            _running_tasks.discard(task_id)


def install_coze_async_route(xhs_cls) -> None:
    """Attach a fast enqueue endpoint without changing the large app module."""
    if getattr(xhs_cls, "_coze_async_route_installed", False):
        return

    original_setup_routes = xhs_cls.setup_routes

    def setup_routes(self, server):
        original_setup_routes(self, server)

        @server.post("/coze/workflow/enqueue", status_code=202, tags=["API"])
        async def coze_workflow_enqueue(
            background_tasks: BackgroundTasks,
            payload: dict = Body(...),
            authorization: str | None = Header(default=None),
        ):
            token = _read_token()
            _authorize(authorization, token)
            clean_payload = _validate_payload(payload)
            task_id = _task_id(clean_payload)
            duplicate, state = _register_task(task_id, clean_payload)

            if duplicate:
                _log_task_event("coze_task_duplicate", state)
            else:
                _log_task_event("coze_task_queued", state)
                background_tasks.add_task(
                    _run_coze_workflow,
                    clean_payload,
                    token,
                    task_id,
                )

            return {
                "status": "queued",
                "task_id": task_id,
                "duplicate": duplicate,
            }

        @server.get("/coze/workflow/status/{task_id}", tags=["API"])
        async def coze_workflow_status(task_id: str):
            state = _get_task_state(task_id)
            if state is None:
                raise HTTPException(status_code=404, detail="task not found")
            return state

    xhs_cls.setup_routes = setup_routes
    xhs_cls._coze_async_route_installed = True
