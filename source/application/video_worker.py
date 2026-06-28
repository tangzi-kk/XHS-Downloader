"""飞书视频持久化任务队列与单并发 Worker。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests


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

    existing = store.existing_task_keys(record_id)
    note_url = str(note_url or "").strip()
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
                "状态": "待处理",
                "重试次数": 0,
            }
        )
    if pending:
        store.create_tasks(pending)
    if pending:
        parent_fields = {
            "视频处理状态": "处理中",
            "视频处理进度": f"0 / {len(urls)}",
        }
    else:
        parent_fields = aggregate_parent_tasks(store.list_parent_tasks(record_id))
    store.update_parent(record_id, parent_fields)
    return {
        "success": True,
        "status": "queued",
        "record_id": record_id,
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
        if _field(task, "状态") == "成功"
        and _field(task, "视频文件Token")
        and _field(task, "封面文件Token")
    ]
    statuses = [_field(task, "状态") for task in ordered]
    if any(status in {"待处理", "处理中", "待重试"} for status in statuses):
        parent_status = "处理中"
    elif ordered and len(completed) == len(ordered):
        parent_status = "完成"
    elif completed and "待人工刷新" in statuses:
        parent_status = "部分完成"
    else:
        parent_status = "视频待处理"

    errors = []
    for task in ordered:
        status = _field(task, "状态")
        if status not in {"待重试", "待人工刷新"}:
            continue
        index = int(_field(task, "视频序号", 0) or 0)
        error = str(_field(task, "最后错误", "未提供错误详情")).strip()
        suffix = "已进入待重试" if status == "待重试" else "等待人工刷新"
        errors.append(f"第 {index} 个视频：{error}；{suffix}")

    return {
        "原视频": [
            {"file_token": _field(task, "视频文件Token")} for task in completed
        ],
        "视频封面": [
            {"file_token": _field(task, "封面文件Token")} for task in completed
        ],
        "视频处理状态": parent_status,
        "视频处理进度": f"{len(completed)} / {len(ordered)}",
        "视频失败详情": "\n".join(errors),
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
        tasks = self._search([self._condition("状态", "处理中")])
        stale = [
            task
            for task in tasks
            if self._milliseconds(_field(task, "锁定时间")) <= stale_before_ms
        ]
        for task in stale:
            self.update_task(
                task["record_id"],
                {
                    "状态": "待重试",
                    "下次重试时间": int(time.time() * 1000),
                    "最后错误": "Worker 锁超过 15 分钟，已自动恢复",
                },
            )
        return len(stale)

    def claim_next_task(self, now_ms: int) -> dict[str, Any] | None:
        candidates = self._search([self._condition("状态", "待处理")])
        retry_tasks = self._search([self._condition("状态", "待重试")])
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
        self.update_task(task["record_id"], {"状态": "处理中", "锁定时间": now_ms})
        task = {**task, "fields": {**(task.get("fields") or {})}}
        task["fields"].update({"状态": "处理中", "锁定时间": now_ms})
        return task


class VideoTaskWorker:
    RETRY_DELAYS = (300, 1200, 3600)

    def __init__(
        self,
        store: Any,
        processor: Callable[[dict[str, Any]], Any],
        poll_seconds: int | None = None,
        stale_seconds: int | None = None,
    ):
        self.store = store
        self.processor = processor
        self.poll_seconds = poll_seconds or int(os.getenv("VIDEO_TASK_POLL_SECONDS", "10"))
        self.stale_seconds = stale_seconds or int(os.getenv("VIDEO_TASK_STALE_SECONDS", "900"))
        self._lock = _PROCESS_WORKER_LOCK

    async def run_once(self) -> bool:
        async with self._lock:
            now_ms = int(time.time() * 1000)
            self.store.recover_stale_tasks(now_ms - self.stale_seconds * 1000)
            task = self.store.claim_next_task(now_ms)
            if not task:
                return False
            task_id = task["record_id"]
            parent_id = str(_field(task, "父素材记录ID", ""))
            try:
                result = self.processor(task)
                if inspect.isawaitable(result):
                    result = await result
                video_token, cover_token = result
                self.store.update_task(
                    task_id,
                    {
                        "状态": "成功",
                        "视频文件Token": video_token,
                        "封面文件Token": cover_token,
                        "最后错误": "",
                    },
                )
            except Exception as error:
                retry_count = int(_field(task, "重试次数", 0) or 0) + 1
                manual_threshold = int(os.getenv("VIDEO_REFRESH_MAX_ATTEMPTS", "4"))
                manual = bool(getattr(error, "requires_manual_refresh", False)) and retry_count >= manual_threshold
                delay = self.RETRY_DELAYS[retry_count - 1] if retry_count <= 3 else 21600
                detail = f"{type(error).__name__}: {error}\n{traceback.format_exc(limit=8)}"[-5000:]
                self.store.update_task(
                    task_id,
                    {
                        "状态": "待人工刷新" if manual else "待重试",
                        "重试次数": retry_count,
                        "下次重试时间": int((time.time() + delay) * 1000),
                        "最后错误": detail,
                    },
                )
            finally:
                if parent_id:
                    try:
                        tasks = self.store.list_parent_tasks(parent_id)
                        self.store.update_parent(parent_id, aggregate_parent_tasks(tasks))
                    except Exception as aggregate_error:
                        retry_count = int(_field(task, "重试次数", 0) or 0) + 1
                        delay = self.RETRY_DELAYS[retry_count - 1] if retry_count <= 3 else 21600
                        self.store.update_task(
                            task_id,
                            {
                                "状态": "待重试",
                                "重试次数": retry_count,
                                "下次重试时间": int((time.time() + delay) * 1000),
                                "最后错误": f"父素材记录汇总失败：{aggregate_error}"[-5000:],
                            },
                        )
            return True

    async def run_until_idle(self) -> None:
        while await self.run_once():
            pass

    async def run_forever(self) -> None:
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


__all__ = [
    "FeishuVideoTaskStore",
    "SingleVideoProcessor",
    "StaleVideoUrlError",
    "VideoTaskWorker",
    "aggregate_parent_tasks",
    "enqueue_video_bundle",
    "extract_origin_video_urls",
    "make_task_key",
    "parse_real_video_urls",
    "run_video_worker",
]
