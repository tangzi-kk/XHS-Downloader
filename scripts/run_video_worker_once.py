"""GitHub Actions 用：每次只串行处理 1 个飞书视频任务后退出。"""

from __future__ import annotations

import asyncio
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


async def main() -> None:
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

        # 关键：只领取并处理 1 条任务，然后退出。
        processed = await worker.run_once()

        if processed:
            print("本轮已处理 1 条视频任务，正常退出。")
        else:
            print("当前没有待处理视频任务，正常退出。")


if __name__ == "__main__":
    asyncio.run(main())
