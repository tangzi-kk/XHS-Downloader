"""Mac mini 本地常驻视频 Worker。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

os.environ.setdefault("VIDEO_TASK_POLL_SECONDS", "5")
os.environ.setdefault("VIDEO_DISPATCH_INTERVAL_SECONDS", "0")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from source import Settings, XHS
from source.application.video_worker import (
    FeishuVideoTaskStore,
    SingleVideoProcessor,
    VideoTaskWorker,
    extract_origin_video_urls,
    parse_real_video_urls,
)
from source.application.worker_control import GitHubWorkerControl, WorkerControlError

logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(event: str, **extra: object) -> None:
    payload = {
        "event": event,
        "worker": "mac-mini-video-worker",
        "host": socket.gethostname(),
    }
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


async def build_worker() -> VideoTaskWorker:
    xhs_context = XHS(**Settings().run())
    xhs = await xhs_context.__aenter__()

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

    store = FeishuVideoTaskStore(token_provider=xhs.get_tenant_access_token)
    processor = SingleVideoProcessor(store, xhs, refresh_video_urls)
    worker = VideoTaskWorker(store, processor)
    worker._xhs_context = xhs_context  # type: ignore[attr-defined]
    return worker


async def main() -> None:
    poll_seconds = int(os.getenv("VIDEO_TASK_POLL_SECONDS", "5"))
    heartbeat_interval = int(os.getenv("VIDEO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "60"))
    last_heartbeat = 0.0
    log_event("worker_start")

    control: GitHubWorkerControl | None
    try:
        control = GitHubWorkerControl.from_env(owner=f"macmini-{socket.gethostname()}")
    except WorkerControlError as error:
        control = None
        log_event("heartbeat_failed", error=str(error))

    worker = await build_worker()
    try:
        while True:
            now = time.monotonic()
            if control and now - last_heartbeat >= heartbeat_interval:
                try:
                    control.update_heartbeat()
                    last_heartbeat = now
                    log_event("heartbeat_updated")
                except Exception as error:
                    log_event("heartbeat_failed", error=type(error).__name__)

            if control:
                try:
                    if control.fallback_lock_active():
                        log_event("worker_paused_for_github_fallback")
                        await asyncio.sleep(poll_seconds)
                        continue
                except Exception as error:
                    log_event("heartbeat_failed", error=type(error).__name__)

            processed = await worker.run_until_idle(max_tasks=1)
            if not processed:
                await asyncio.sleep(poll_seconds)
    finally:
        xhs_context = getattr(worker, "_xhs_context", None)
        if xhs_context is not None:
            await xhs_context.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
