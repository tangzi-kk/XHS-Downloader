"""Asynchronous Coze workflow dispatch for short-lived webhook callers."""

from __future__ import annotations

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
_logger = logging.getLogger("coze_async")
_running_tasks: set[str] = set()
_running_tasks_lock = Lock()


def _task_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


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
    timeout_seconds = int(
        os.getenv("COZE_WORKFLOW_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
    )
    record_id = str(payload.get("parameters", {}).get("record_id") or "")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                COZE_WORKFLOW_RUN_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()

            try:
                result = response.json()
            except ValueError:
                result = {}

            business_code = result.get("code") if isinstance(result, dict) else None
            if business_code not in (None, 0):
                _logger.error(
                    "coze_task_failed task_id=%s record_id=%s code=%s",
                    task_id,
                    record_id,
                    business_code,
                )
            else:
                _logger.info(
                    "coze_task_succeeded task_id=%s record_id=%s http_status=%s",
                    task_id,
                    record_id,
                    response.status_code,
                )
    except Exception as error:
        _logger.exception(
            "coze_task_exception task_id=%s record_id=%s error_type=%s error=%s",
            task_id,
            record_id,
            type(error).__name__,
            str(error)[:300],
        )
    finally:
        with _running_tasks_lock:
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

            with _running_tasks_lock:
                duplicate = task_id in _running_tasks
                if not duplicate:
                    _running_tasks.add(task_id)

            if not duplicate:
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

    xhs_cls.setup_routes = setup_routes
    xhs_cls._coze_async_route_installed = True
