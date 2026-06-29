"""飞书视频持久化任务队列与单并发 Worker。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

logger = logging.getLogger("video_worker")

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
UPLOAD_ALL_LIMIT = 20 * 1024 * 1024
VIDEO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
}
_ENQUEUE_LOCK = threading.Lock()
_PROCESS_WORKER_LOCK = asyncio.Lock()

# ── 可配置常量 ──────────────────────────────────────────────
VIDEO_DISPATCH_INTERVAL_SECONDS = int(
    os.getenv("VIDEO_DISPATCH_INTERVAL_SECONDS", "15")
)
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = int(
    os.getenv("VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "120")
)
VIDEO_UPLOAD_TIMEOUT_SECONDS = int(
    os.getenv("VIDEO_UPLOAD_TIMEOUT_SECONDS", "180")
)
VIDEO_MAX_RETRIES = int(os.getenv("VIDEO_MAX_RETRIES", "8"))
VIDEO_RETRY_BASE_SECONDS = int(os.getenv("VIDEO_RETRY_BASE_SECONDS", "60"))
VIDEO_STALE_RUNNING_SECONDS = int(
    os.getenv("VIDEO_STALE_RUNNING_SECONDS", "900")
)


def _worker_id() -> str:
    """生成当前 Worker 标识，用于飞书记录排查。"""
    host = os.getenv("RENDER_INSTANCE_ID") or socket.gethostname() or "unknown"
    pid = os.getpid()
    return f"{host[:32]}-{pid}"


def _log_event(
    event: str,
    note_id: str = "",
    batch_id: str = "",
    task_id: str = "",
    video_index: int = 0,
    status_before: str = "",
    status_after: str = "",
    retry_count: int = 0,
    error_type: str = "",
    error_message: str = "",
    duration_ms: int = 0,
    **extra: Any,
) -> None:
    """结构化日志，不输出完整 token 或密钥。"""
    payload: dict[str, Any] = {
        "event": event,
        "worker_id": _worker_id(),
    }
    if note_id:
        payload["note_id"] = note_id
    if batch_id:
        payload["batch_id"] = batch_id
    if task_id:
        payload["task_id"] = task_id
    if video_index:
        payload["video_index"] = video_index
    if status_before:
        payload["status_before"] = status_before
    if status_after:
        payload["status_after"] = status_after
    if retry_count:
        payload["retry_count"] = retry_count
    if error_type:
        payload["error_type"] = error_type
    if error_message:
        payload["error_message"] = error_message[:500]
    if duration_ms:
        payload["duration_ms"] = duration_ms
    payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


def parse_real_video_urls(value: Any) -> list[str]:
    """解析一篇笔记的真实视频 URL，去重并保持出现顺序。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                items = [text]
            else:
                items = decoded if isinstance(decoded, list) else [text]
        else:
            items = [text]
    else:
        items = [str(value)]

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, (list, tuple)):
            candidates = parse_real_video_urls(item)
        else:
            text = re.sub(r"%0A", "\n", str(item), flags=re.IGNORECASE)
            candidates = [part.strip() for part in text.splitlines()]
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
    return result


def make_task_key(record_id: str, video_index: int, video_url: str) -> str:
    raw = f"{record_id.strip()}\n{video_index}\n{video_url.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_origin_video_urls(namespace: Any) -> list[str]:
    """从笔记原始结构提取各真实视频的 originVideoKey，不展开备用清晰度。"""
    keys: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "originVideoKey" and value:
                    keys.append(str(value))
                else:
                    visit(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                visit(value)
        elif hasattr(node, "__dict__"):
            visit(vars(node))

    visit(getattr(namespace, "data", namespace))
    urls = []
    for key in keys:
        url = key if key.startswith(("http://", "https://")) else (
            f"https://sns-video-bd.xhscdn.com/{key.lstrip('/')}"
        )
        if url not in urls:
            urls.append(url)
    return urls


def _make_batch_id(note_id: str) -> str:
    """生成幂等的批次 ID，基于时间戳和笔记标识。"""
    ts = int(time.time())
    short = hashlib.sha256(note_id.encode()).hexdigest()[:8] if note_id else "unknown"
    return f"batch_{ts}_{short}"


def enqueue_video_bundle(
    store: Any,
    record_id: str,
    video_url: Any,
    note_url: str | None = None,
) -> dict[str, Any]:
    with _ENQUEUE_LOCK:
        return _enqueue_video_bundle(store, record_id, video_url, note_url)


def _enqueue_video_bundle(
    store: Any,
    record_id: str,
    video_url: Any,
    note_url: str | None = None,
) -> dict[str, Any]:
    record_id = str(record_id or "").strip()
    if not record_id:
        raise ValueError("record_id is required")
    urls = parse_real_video_urls(video_url)
    if not urls:
        raise ValueError("video_url does not contain a usable URL")
    if any(urlparse(url).scheme not in {"http", "https"} for url in urls):
        raise ValueError("video_url 仅支持 http/https URL")

    note_url = str(note_url or "").strip()
    batch_id = _make_batch_id(record_id)
    existing = store.existing_task_keys(record_id)
    pending = []
    existing_count = 0
    for index, url in enumerate(urls, start=1):
        task_key = make_task_key(record_id, index, url)
        if task_key in existing:
            existing_count += 1
            continue
        pending.append(
            {
                "任务键": task_key,
                "父素材记录ID": record_id,
                "原始笔记链接": note_url,
                "视频序号": index,
                "视频直链": url,
                "状态": "PENDING",
                "重试次数": 0,
            }
        )
    if pending:
        store.create_tasks(pending)
        _log_event(
            "tasks_created",
            note_id=record_id,
            batch_id=batch_id,
            **{"total_count": len(urls), "created_count": len(pending),
               "existing_count": existing_count},
        )
    if pending:
        parent_fields = {
            "视频处理状态": "PROCESSING",
            "视频处理进度": f"0 / {len(urls)}",
            "视频总数": len(urls),
            "视频成功数": 0,
            "视频失败数": 0,
            "视频任务批次ID": batch_id,
        }
    else:
        parent_fields = aggregate_parent_tasks(store.list_parent_tasks(record_id))
    store.update_parent(record_id, parent_fields)
    return {
        "success": True,
        "status": "queued",
        "record_id": record_id,
        "batch_id": batch_id,
        "total_count": len(urls),
        "created_count": len(pending),
        "existing_count": existing_count,
    }


def _field(task: dict[str, Any], name: str, default: Any = None) -> Any:
    return (task.get("fields") or {}).get(name, default)


def aggregate_parent_tasks(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(tasks, key=lambda task: int(_field(task, "视频序号", 0) or 0))
    completed = [
        task
        for task in ordered
        if _field(task, "状态") == "SUCCEEDED"
        and _field(task, "视频文件Token")
        and _field(task, "封面文件Token")
    ]
    failed = [
        task
        for task in ordered
        if _field(task, "状态") in {"FAILED"}
    ]
    statuses = [_field(task, "状态") for task in ordered]

    total = len(ordered)
    succeeded = len(completed)
    failed_count = len(failed)

    if total == 0:
        parent_status = "NO_VIDEO"
    elif any(status in {"PENDING", "RUNNING", "RETRY_WAIT"} for status in statuses):
        parent_status = "PROCESSING"
    elif succeeded == total:
        parent_status = "VIDEO_COMPLETE"
    elif failed_count > 0 and succeeded > 0:
        parent_status = "PARTIAL_FAILED"
    elif succeeded == 0 and failed_count > 0:
        parent_status = "PARTIAL_FAILED"
    else:
        parent_status = "AWAITING"

    errors: list[str] = []
    summary_parts: list[str] = []
    for task in ordered:
        status = _field(task, "状态")
        index = int(_field(task, "视频序号", 0) or 0)
        retry = int(_field(task, "重试次数", 0) or 0)
        if status in {"RETRY_WAIT", "FAILED"}:
            error = str(_field(task, "最后错误", "未提供错误详情")).strip()
            suffix = "已进入待重试" if status == "RETRY_WAIT" else "已标记为失败"
            detail = f"第 {index} 个视频（重试 {retry} 次）：{error}；{suffix}"
            errors.append(detail)
            short = str(_field(task, "最后错误", "未知错误")).strip()[:80]
            summary_parts.append(f"#{index}: {short}")

    return {
        "原视频": [
            {"file_token": _field(task, "视频文件Token")} for task in completed
        ],
        "视频封面": [
            {"file_token": _field(task, "封面文件Token")} for task in completed
        ],
        "视频处理状态": parent_status,
        "视频处理进度": f"{succeeded} / {total}",
        "视频总数": total,
        "视频成功数": succeeded,
        "视频失败数": failed_count,
        "视频失败详情": "\n".join(errors),
        "视频失败摘要": "\n".join(summary_parts),
    }


class FeishuAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class StaleVideoUrlError(RuntimeError):
    requires_manual_refresh = True


class FeishuClient:
    def __init__(self, token_provider: Callable[[], str] | None = None):
        self.token_provider = token_provider
        self._token = ""
        self._token_expires_at = 0.0

    def token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        if self.token_provider:
            self._token = self.token_provider()
            self._token_expires_at = time.monotonic() + 6000
            return self._token
        app_id = os.getenv("FEISHU_APP_ID")
        app_secret = os.getenv("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            raise FeishuAPIError("Missing FEISHU_APP_ID or FEISHU_APP_SECRET")
        response = requests.post(
            f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=30,
        )
        data = self._decode(response)
        self._token = str(data.get("tenant_access_token") or "")
        if not self._token:
            raise FeishuAPIError(f"tenant_access_token missing: {data}")
        self._token_expires_at = time.monotonic() + 6000
        return self._token

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as error:
            raise FeishuAPIError(
                f"飞书返回非 JSON：{response.text[:1000]}", response.status_code
            ) from error
        if not response.ok or data.get("code") not in (None, 0):
            raise FeishuAPIError(
                f"飞书请求失败 HTTP {response.status_code}: {data}",
                response.status_code,
            )
        return data

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token()}"
        response = requests.request(
            method,
            f"{FEISHU_BASE_URL}{path}",
            headers=headers,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        return self._decode(response)


class FeishuVideoTaskStore:
    def __init__(self, token_provider: Callable[[], str] | None = None):
        self.app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN", "").strip()
        self.parent_table_id = os.getenv("FEISHU_BITABLE_TABLE_ID", "").strip()
        self.task_table_id = os.getenv("FEISHU_VIDEO_TASK_TABLE_ID", "").strip()
        missing = [
            name
            for name, value in (
                ("FEISHU_BITABLE_APP_TOKEN", self.app_token),
                ("FEISHU_BITABLE_TABLE_ID", self.parent_table_id),
                ("FEISHU_VIDEO_TASK_TABLE_ID", self.task_table_id),
            )
            if not value
        ]
        if missing:
            raise FeishuAPIError(f"Missing {', '.join(missing)}")
        self.client = FeishuClient(token_provider)

    def _records_path(self, table_id: str) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"

    def _search(self, conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.client.request(
                "POST",
                f"{self._records_path(self.task_table_id)}/search",
                params=params,
                json={
                    "automatic_fields": True,
                    "filter": {"conjunction": "and", "conditions": conditions},
                },
            ).get("data", {})
            result.extend(data.get("items") or [])
            if not data.get("has_more"):
                return result
            page_token = data.get("page_token") or ""

    @staticmethod
    def _condition(field_name: str, value: Any) -> dict[str, Any]:
        return {"field_name": field_name, "operator": "is", "value": [str(value)]}

    def existing_task_keys(self, parent_record_id: str) -> set[str]:
        tasks = self._search([self._condition("父素材记录ID", parent_record_id)])
        return {str(_field(task, "任务键", "")) for task in tasks}

    def create_tasks(self, task_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        created = []
        for start in range(0, len(task_fields), 500):
            data = self.client.request(
                "POST",
                f"{self._records_path(self.task_table_id)}/batch_create",
                json={"records": [{"fields": fields} for fields in task_fields[start:start + 500]]},
            ).get("data", {})
            created.extend(data.get("records") or [])
        return created

    def update_parent(self, record_id: str, fields: dict[str, Any]) -> None:
        self.client.request(
            "PUT",
            f"{self._records_path(self.parent_table_id)}/{record_id}",
            json={"fields": fields},
        )

    def update_task(self, task_record_id: str, fields: dict[str, Any]) -> None:
        self.client.request(
            "PUT",
            f"{self._records_path(self.task_table_id)}/{task_record_id}",
            json={"fields": fields},
        )

    def list_parent_tasks(self, parent_record_id: str) -> list[dict[str, Any]]:
        return self._search([self._condition("父素材记录ID", parent_record_id)])

    @staticmethod
    def _milliseconds(value: Any) -> int:
        if isinstance(value, dict):
            value = value.get("timestamp") or value.get("value") or 0
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def recover_stale_tasks(self, stale_before_ms: int) -> int:
        tasks = self._search([self._condition("状态", "RUNNING")])
        stale = [
            task
            for task in tasks
            if self._milliseconds(_field(task, "锁定时间")) <= stale_before_ms
        ]
        for task in stale:
            self.update_task(
                task["record_id"],
                {
                    "状态": "RETRY_WAIT",
                    "下次重试时间": int(time.time() * 1000),
                    "最后错误": "Worker 锁超过 15 分钟，已自动恢复",
                },
            )
        return len(stale)

    def claim_next_task(self, now_ms: int) -> dict[str, Any] | None:
        candidates = self._search([self._condition("状态", "PENDING")])
        retry_tasks = self._search([self._condition("状态", "RETRY_WAIT")])
        candidates.extend(
            task
            for task in retry_tasks
            if self._milliseconds(_field(task, "下次重试时间")) <= now_ms
        )
        if not candidates:
            return None
        task = min(
            candidates,
            key=lambda item: (
                self._milliseconds(_field(item, "创建时间")),
                int(_field(item, "视频序号", 0) or 0),
                item.get("record_id", ""),
            ),
        )
        self.update_task(task["record_id"], {"状态": "RUNNING", "锁定时间": now_ms})
        task = {**task, "fields": {**(task.get("fields") or {})}}
        task["fields"].update({"状态": "RUNNING", "锁定时间": now_ms})
        return task


class VideoTaskWorker:
    def __init__(
        self,
        store: Any,
        processor: Callable[[dict[str, Any]], Any],
        poll_seconds: int | None = None,
        stale_seconds: int | None = None,
        dispatch_interval_seconds: int | None = None,
        max_retries: int | None = None,
        retry_base_seconds: int | None = None,
    ):
        self.store = store
        self.processor = processor
        self.poll_seconds = poll_seconds or int(os.getenv("VIDEO_TASK_POLL_SECONDS", "10"))
        self.stale_seconds = stale_seconds or VIDEO_STALE_RUNNING_SECONDS
        self.dispatch_interval = (
            dispatch_interval_seconds
            if dispatch_interval_seconds is not None
            else VIDEO_DISPATCH_INTERVAL_SECONDS
        )
        self.max_retries = max_retries if max_retries is not None else VIDEO_MAX_RETRIES
        self.retry_base = retry_base_seconds if retry_base_seconds is not None else VIDEO_RETRY_BASE_SECONDS
        self.worker_id = _worker_id()
        self._lock = _PROCESS_WORKER_LOCK

    def _retry_delay(self, retry_count: int) -> int:
        """指数退避 + 少量抖动。"""
        delay = min(self.retry_base * (2 ** (retry_count - 1)), 21600)
        jitter = int(hashlib.sha256(f"{retry_count}{time.time()}".encode()).hexdigest()[:4], 16) % max(delay // 10, 1)
        return delay + jitter

    async def run_once(self) -> bool:
        async with self._lock:
            now_ms = int(time.time() * 1000)
            recovered = self.store.recover_stale_tasks(
                now_ms - self.stale_seconds * 1000
            )
            if recovered:
                _log_event("stale_tasks_recovered", **{"recovered_count": recovered})
            task = self.store.claim_next_task(now_ms)
            if not task:
                return False
            task_id = task["record_id"]
            parent_id = str(_field(task, "父素材记录ID", ""))
            video_index = int(_field(task, "视频序号", 0) or 0)
            status_before = str(_field(task, "状态", ""))
            retry_count_before = int(_field(task, "重试次数", 0) or 0)
            start_ms = int(time.time() * 1000)
            _log_event(
                "task_start",
                task_id=task_id,
                note_id=parent_id,
                video_index=video_index,
                status_before=status_before,
                retry_count=retry_count_before,
                **{"worker_id": self.worker_id},
            )
            try:
                result = self.processor(task)
                if inspect.isawaitable(result):
                    result = await result
                video_token, cover_token = result
                duration = int(time.time() * 1000) - start_ms
                self.store.update_task(
                    task_id,
                    {
                        "状态": "SUCCEEDED",
                        "视频文件Token": video_token,
                        "封面文件Token": cover_token,
                        "最后错误": "",
                    },
                )
                _log_event(
                    "task_success",
                    task_id=task_id,
                    note_id=parent_id,
                    video_index=video_index,
                    status_after="SUCCEEDED",
                    duration_ms=duration,
                    retry_count=retry_count_before,
                )
            except Exception as error:
                duration = int(time.time() * 1000) - start_ms
                retry_count = retry_count_before + 1
                manual = bool(
                    getattr(error, "requires_manual_refresh", False)
                ) and retry_count >= VIDEO_MAX_RETRIES
                delay = self._retry_delay(retry_count)
                detail = (
                    f"{type(error).__name__}: {error}\n"
                    f"{traceback.format_exc(limit=8)}"
                )[-5000:]
                new_status = "FAILED" if manual else "RETRY_WAIT"
                self.store.update_task(
                    task_id,
                    {
                        "状态": new_status,
                        "重试次数": retry_count,
                        "下次重试时间": int((time.time() + delay) * 1000),
                        "最后错误": detail,
                    },
                )
                _log_event(
                    "task_failed",
                    task_id=task_id,
                    note_id=parent_id,
                    video_index=video_index,
                    status_before=status_before,
                    status_after=new_status,
                    retry_count=retry_count,
                    error_type=type(error).__name__,
                    error_message=str(error)[:500],
                    duration_ms=duration,
                    **{"next_retry_delay_seconds": delay},
                )
            finally:
                if parent_id:
                    try:
                        tasks = self.store.list_parent_tasks(parent_id)
                        self.store.update_parent(
                            parent_id,
                            aggregate_parent_tasks(tasks),
                        )
                    except Exception as aggregate_error:
                        _log_event(
                            "aggregate_failed",
                            task_id=task_id,
                            note_id=parent_id,
                            error_type=type(aggregate_error).__name__,
                            error_message=str(aggregate_error)[:500],
                        )
            # 处理完一个任务后，等待可配置的间隔
            await asyncio.sleep(self.dispatch_interval)
            return True

    async def run_until_idle(self) -> None:
        while await self.run_once():
            pass

    async def run_forever(self) -> None:
        _log_event("worker_start", **{"worker_id": self.worker_id})
        while True:
            try:
                processed = await self.run_once()
            except Exception:
                traceback.print_exc()
                processed = False
            if not processed:
                await asyncio.sleep(self.poll_seconds)


class SingleVideoProcessor:
    def __init__(
        self,
        store: FeishuVideoTaskStore,
        media_helpers: Any,
        refresh_video_urls: Callable[[str], Any] | None = None,
    ):
        self.store = store
        self.media_helpers = media_helpers
        self.refresh_video_urls = refresh_video_urls

    async def _refresh(self, task: dict[str, Any]) -> str:
        note_url = str(_field(task, "原始笔记链接", "")).strip()
        if not note_url or not self.refresh_video_urls:
            raise StaleVideoUrlError("视频直链返回 401/403，且没有可用的原始笔记链接")
        try:
            refreshed = self.refresh_video_urls(note_url)
            if inspect.isawaitable(refreshed):
                refreshed = await refreshed
        except Exception as error:
            raise StaleVideoUrlError(f"重新解析原始笔记失败：{error}") from error
        urls = parse_real_video_urls(refreshed)
        index = int(_field(task, "视频序号", 0) or 0)
        if index < 1 or index > len(urls):
            raise StaleVideoUrlError(f"重新解析后找不到第 {index} 个视频直链")
        url = urls[index - 1]
        self.store.update_task(task["record_id"], {"视频直链": url})
        return url

    def _download(self, url: str, destination: Path) -> tuple[str, str]:
        read_timeout = int(os.getenv("VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "180"))
        total_timeout = int(os.getenv("VIDEO_DOWNLOAD_MAX_SECONDS", "600"))
        deadline = time.monotonic() + total_timeout
        maximum = int(os.getenv("MAX_VIDEO_UPLOAD_BYTES", "0") or 0)
        with requests.get(
            url,
            headers=VIDEO_HEADERS,
            stream=True,
            timeout=(15, min(read_timeout, 30, total_timeout)),
        ) as response:
            if response.status_code in {401, 403}:
                error = StaleVideoUrlError(f"视频下载 HTTP {response.status_code}")
                error.status_code = response.status_code
                raise error
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if maximum and declared > maximum:
                raise RuntimeError(f"视频大小 {declared} 超过 MAX_VIDEO_UPLOAD_BYTES={maximum}")
            size = 0
            with destination.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"视频下载超过总时限 {total_timeout} 秒")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if maximum and size > maximum:
                        raise RuntimeError(f"视频大小超过 MAX_VIDEO_UPLOAD_BYTES={maximum}")
                    output.write(chunk)
            if size < 12:
                raise RuntimeError("下载结果过小，不是有效视频")
            return response.headers.get("Content-Type", ""), str(size)

    @staticmethod
    def _cover(video_path: Path, cover_path: Path) -> None:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.3", "-i", str(video_path), "-frames:v", "1",
                "-q:v", "2", str(cover_path),
            ],
            capture_output=True,
            timeout=int(os.getenv("VIDEO_FFMPEG_TIMEOUT_SECONDS", "60")),
        )
        if result.returncode or not cover_path.exists():
            raise RuntimeError(
                f"ffmpeg 视频校验或封面提取失败：{result.stderr.decode(errors='ignore')[-1000:]}"
            )

    def _multipart_upload(
        self, path: Path, filename: str, content_type: str, parent_type: str
    ) -> str:
        size = path.stat().st_size
        client = self.store.client
        prepared = client.request(
            "POST",
            "/drive/v1/medias/upload_prepare",
            json={
                "file_name": filename,
                "parent_type": parent_type,
                "parent_node": self.store.app_token,
                "size": size,
                "extra": json.dumps({"drive_route_token": self.store.app_token}),
            },
            timeout=60,
        ).get("data", {})
        upload_id = prepared.get("upload_id")
        block_size = int(prepared.get("block_size") or 4 * 1024 * 1024)
        block_num = int(prepared.get("block_num") or 0)
        if not upload_id or not block_num:
            raise FeishuAPIError(f"分片上传初始化结果无效：{prepared}")
        with path.open("rb") as source:
            for sequence in range(block_num):
                chunk = source.read(block_size)
                client.request(
                    "POST",
                    "/drive/v1/medias/upload_part",
                    data={"upload_id": upload_id, "seq": sequence, "size": len(chunk)},
                    files={"file": (filename, chunk, content_type)},
                    timeout=120,
                )
        finished = client.request(
            "POST",
            "/drive/v1/medias/upload_finish",
            json={"upload_id": upload_id, "block_num": block_num},
            timeout=60,
        ).get("data", {})
        token = finished.get("file_token")
        if not token:
            raise FeishuAPIError(f"分片上传完成但缺少 file_token：{finished}")
        return str(token)

    def _upload(self, path: Path, filename: str, content_type: str) -> str:
        size = path.stat().st_size
        if size <= UPLOAD_ALL_LIMIT:
            return self.media_helpers.upload_image_to_feishu(
                image_bytes=path.read_bytes(),
                filename=filename,
                content_type=content_type,
                tenant_access_token=self.store.client.token(),
                app_token=self.store.app_token,
            )
        parent_type = "bitable_image" if content_type.startswith("image/") else "bitable_file"
        return self._multipart_upload(path, filename, content_type, parent_type)

    async def __call__(self, task: dict[str, Any]) -> tuple[str, str]:
        existing_video_token = str(_field(task, "视频文件Token", "")).strip()
        existing_cover_token = str(_field(task, "封面文件Token", "")).strip()
        if existing_video_token and existing_cover_token:
            return existing_video_token, existing_cover_token
        url = str(_field(task, "视频直链", "")).strip()
        index = int(_field(task, "视频序号", 0) or 0)
        if not url:
            raise RuntimeError("任务没有视频直链")
        with tempfile.TemporaryDirectory(prefix="xhs-video-") as temp_dir:
            parsed_name = unquote(Path(urlparse(url).path).name) or f"video-{index}.mp4"
            video_path = Path(temp_dir) / parsed_name
            try:
                declared_type, _ = self._download(url, video_path)
            except StaleVideoUrlError:
                url = await self._refresh(task)
                declared_type, _ = self._download(url, video_path)
            sample = video_path.read_bytes()[:64]
            content_type = self.media_helpers.infer_media_content_type(sample, declared_type)
            if not self.media_helpers.is_video_media(parsed_name, content_type):
                raise RuntimeError(f"下载内容不是视频：{content_type}")
            cover_path = Path(temp_dir) / f"cover-{index}.jpg"
            self._cover(video_path, cover_path)
            video_token = self._upload(video_path, parsed_name, content_type)
            cover_token = self._upload(cover_path, cover_path.name, "image/jpeg")
            return video_token, cover_token


async def run_video_worker(xhs: Any) -> None:
    store = FeishuVideoTaskStore(token_provider=xhs.get_tenant_access_token)

    async def refresh_video_urls(note_url: str) -> list[str]:
        links = await xhs.extract_links(note_url)
        if not links:
            return []
        _, namespace = await xhs._get_html_data(links[0], True)
        if isinstance(namespace, dict):
            return []
        return extract_origin_video_urls(namespace) or parse_real_video_urls(
            xhs.video.deal_video_link(namespace, xhs.manager.video_preference)
        )

    processor = SingleVideoProcessor(store, xhs, refresh_video_urls)
    await VideoTaskWorker(store, processor).run_forever()


def get_video_job_status(
    store: FeishuVideoTaskStore,
    parent_record_id: str,
) -> dict[str, Any]:
    """查询一条笔记的全部视频任务状态。

    返回每个任务的序号、状态、重试次数、错误原因、最后更新时间，
    以及汇总的统计信息。
    """
    parent_record_id = str(parent_record_id or "").strip()
    if not parent_record_id:
        raise ValueError("parent_record_id is required")

    tasks = store.list_parent_tasks(parent_record_id)
    ordered = sorted(
        tasks,
        key=lambda task: int(_field(task, "视频序号", 0) or 0),
    )

    status_counts: dict[str, int] = {}
    task_details: list[dict[str, Any]] = []
    for task in ordered:
        status = str(_field(task, "状态", "未知"))
        status_counts[status] = status_counts.get(status, 0) + 1
        detail = {
            "task_id": task.get("record_id", ""),
            "video_index": int(_field(task, "视频序号", 0) or 0),
            "status": status,
            "retry_count": int(_field(task, "重试次数", 0) or 0),
            "error": str(_field(task, "最后错误", "")).strip()[:200] or None,
            "video_url": str(_field(task, "视频直链", "")).strip() or None,
            "video_file_token": str(_field(task, "视频文件Token", "")).strip() or None,
            "cover_file_token": str(_field(task, "封面文件Token", "")).strip() or None,
            "can_retry": status in {"RETRY_WAIT", "FAILED"},
        }
        task_details.append(detail)

    total = len(ordered)
    succeeded = status_counts.get("SUCCEEDED", 0)
    all_done = total > 0 and succeeded == total

    return {
        "parent_record_id": parent_record_id,
        "video_total": total,
        "status_counts": status_counts,
        "all_completed": all_done,
        "tasks": task_details,
    }


def retry_video_task(
    store: FeishuVideoTaskStore,
    task_record_id: str,
) -> dict[str, Any]:
    """手动重试一个失败或等待重试的视频任务。

    仅 FAILED / RETRY_WAIT 状态可重试。
    重置为 PENDING，保留历史错误和已上传附件。
    """
    task_record_id = str(task_record_id or "").strip()
    if not task_record_id:
        raise ValueError("task_record_id is required")

    # 查单个任务记录
    tasks = store._search(
        [FeishuVideoTaskStore._condition("记录 ID", task_record_id)]
    )
    if not tasks:
        raise ValueError(f"任务不存在：{task_record_id}")

    task = tasks[0]
    current_status = str(_field(task, "状态", ""))

    if current_status not in {"RETRY_WAIT", "FAILED"}:
        raise ValueError(
            f"任务状态为「{current_status}」，仅「RETRY_WAIT」或「FAILED」可手动重试"
        )

    parent_id = str(_field(task, "父素材记录ID", ""))
    _log_event(
        "task_manual_retry",
        task_id=task_record_id,
        note_id=parent_id,
        status_before=current_status,
        status_after="PENDING",
    )

    store.update_task(
        task_record_id,
        {
            "状态": "PENDING",
            "下次重试时间": 0,
            "最后错误": f"（手动重试，原状态：{current_status}）"
            + str(_field(task, "最后错误", ""))[-4000:],
        },
    )

    return {
        "success": True,
        "task_id": task_record_id,
        "parent_record_id": parent_id,
        "previous_status": current_status,
        "new_status": "PENDING",
    }


__all__ = [
    "FeishuVideoTaskStore",
    "SingleVideoProcessor",
    "StaleVideoUrlError",
    "VideoTaskWorker",
    "aggregate_parent_tasks",
    "enqueue_video_bundle",
    "extract_origin_video_urls",
    "get_video_job_status",
    "make_task_key",
    "parse_real_video_urls",
    "retry_video_task",
    "run_video_worker",
]
