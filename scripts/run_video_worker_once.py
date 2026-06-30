"""GitHub Actions 用：Mac mini 失联时串行批量处理飞书视频任务。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让脚本能找到项目根目录里的 source 文件夹。
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


def parse_max_tasks(value: str | None) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return 24
    if parsed < 1 or parsed > 24:
        return 24
    return parsed


async def main() -> None:
    max_tasks = parse_max_tasks(os.getenv("VIDEO_WORKER_MAX_TASKS"))
    heartbeat_skipped = False
    fallback_acquired = False
    owner = os.getenv("GITHUB_RUN_ID") or "github-actions"

    try:
        control = GitHubWorkerControl.from_env(owner=owner)
        decision = control.acquire_fallback_if_needed(
            heartbeat_max_age_seconds=int(
                os.getenv("VIDEO_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "600")
            ),
            lock_ttl_seconds=int(
                os.getenv("VIDEO_WORKER_FALLBACK_LOCK_TTL_SECONDS", "900")
            ),
        )
        heartbeat_skipped = decision.heartbeat_fresh
        fallback_acquired = decision.lock_acquired
        if not decision.should_run:
            print(f"Mac mini 心跳正常跳过: {heartbeat_skipped}")
            print("本轮实际处理视频数: 0")
            print(f"本轮拿到 GitHub 兜底执行权: {fallback_acquired}")
            print(f"跳过原因: {decision.reason}")
            return
    except WorkerControlError as error:
        print(f"GitHub 兜底控制面不可用，停止执行: {error}")
        raise SystemExit(2) from error

    print(f"VIDEO_WORKER_MAX_TASKS={max_tasks}，本轮最多顺序处理 {max_tasks} 条。")
    async with XHS(**Settings().run()) as xhs:
        store = FeishuVideoTaskStore(
            token_provider=xhs.get_tenant_access_token
        )

        async def refresh_video_urls(note_url: str) -> list[str]:
            """视频直链过期时，重新从原小红书笔记取新地址。"""
            links = await xhs.extract_links(note_url)

            if not links:
                return []

            _, namespace = await xhs._get_html_data(links[0], True)

            if isinstance(namespace, dict):
                return []

            return extract_origin_video_urls(
                namespace
            ) or parse_real_video_urls(
                xhs.video.deal_video_link(
                    namespace,
                    xhs.manager.video_preference,
                )
            )

        processor = SingleVideoProcessor(
            store,
            xhs,
            refresh_video_urls,
        )

        worker = VideoTaskWorker(store, processor)

        processed = await worker.run_until_idle(max_tasks=max_tasks)

        print(f"Mac mini 心跳正常跳过: {heartbeat_skipped}")
        print(f"本轮实际处理视频数: {processed}")
        print(f"本轮拿到 GitHub 兜底执行权: {fallback_acquired}")


if __name__ == "__main__":
    asyncio.run(main())
