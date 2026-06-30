import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from source.application.app import XHS
from source.application.video_worker import (
    VideoTaskWorker,
    SingleVideoProcessor,
    StaleVideoUrlError,
    aggregate_parent_tasks,
    enqueue_video_bundle,
    extract_origin_video_urls,
    normalize_feishu_text,
    parse_real_video_urls,
    retry_video_task,
)
from scripts.run_video_worker_once import parse_max_tasks


class FakeStore:
    def __init__(self):
        self.tasks = []
        self.parent_updates = []

    def existing_task_keys(self, parent):
        return {t["fields"]["任务键"] for t in self.tasks if t["fields"]["父素材记录ID"] == parent}

    def create_tasks(self, fields_list):
        for fields in fields_list:
            self.tasks.append({"record_id": f"task-{len(self.tasks) + 1}", "fields": dict(fields)})

    def update_parent(self, record_id, fields):
        self.parent_updates.append((record_id, dict(fields)))

    def recover_stale_tasks(self, stale_before_ms):
        return 0

    def claim_next_task(self, now_ms):
        candidates = [
            t for t in self.tasks
            if t["fields"]["状态"] == "PENDING"
            or (
                t["fields"]["状态"] == "RETRY_WAIT"
                and t["fields"].get("下次重试时间", 0) <= now_ms
            )
        ]
        if not candidates:
            return None
        task = sorted(candidates, key=lambda t: t["fields"]["视频序号"])[0]
        task["fields"].update({"状态": "RUNNING", "锁定时间": now_ms})
        return {"record_id": task["record_id"], "fields": dict(task["fields"])}

    def update_task(self, record_id, fields):
        task = next(t for t in self.tasks if t["record_id"] == record_id)
        task["fields"].update(fields)

    def get_task(self, task_record_id):
        task = next((t for t in self.tasks if t["record_id"] == task_record_id), None)
        if task is None:
            return None
        return {"record_id": task["record_id"], "fields": dict(task["fields"])}

    def list_parent_tasks(self, parent):
        return [t for t in self.tasks if t["fields"]["父素材记录ID"] == parent]


class ParseTests(unittest.TestCase):
    def test_single_url(self):
        self.assertEqual(parse_real_video_urls("url1"), ["url1"])

    def test_newlines_keep_order(self):
        self.assertEqual(parse_real_video_urls("url1\r\nurl2\nurl3"), ["url1", "url2", "url3"])

    def test_percent_newline(self):
        self.assertEqual(parse_real_video_urls("url1%0Aurl2"), ["url1", "url2"])

    def test_json_array_and_deduplication(self):
        self.assertEqual(parse_real_video_urls('["url1", "url2", "url1"]'), ["url1", "url2"])

    def test_feishu_text_segments_are_joined_in_order(self):
        self.assertEqual(
            normalize_feishu_text(
                [
                    {"text": "https://example.com/", "type": "text"},
                    {"text": "a.mp4", "type": "text"},
                ]
            ),
            "https://example.com/a.mp4",
        )

    def test_feishu_text_segments_parse_as_url(self):
        self.assertEqual(
            parse_real_video_urls(
                [{"text": "https://example.com/a.mp4", "type": "text"}]
            ),
            ["https://example.com/a.mp4"],
        )

    def test_refresh_extracts_multiple_origin_videos_without_backup_urls(self):
        namespace = SimpleNamespace(
            data=SimpleNamespace(
                clips=[
                    SimpleNamespace(originVideoKey="video/one"),
                    SimpleNamespace(originVideoKey="video/two", backupUrls=["backup-a", "backup-b"]),
                ]
            )
        )
        self.assertEqual(
            extract_origin_video_urls(namespace),
            [
                "https://sns-video-bd.xhscdn.com/video/one",
                "https://sns-video-bd.xhscdn.com/video/two",
            ],
        )


class EnqueueTests(unittest.TestCase):
    def test_single_url_and_old_payload_without_note_url(self):
        store = FakeStore()
        result = enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        self.assertEqual((result["total_count"], result["created_count"]), (1, 1))
        self.assertEqual(store.tasks[0]["fields"]["原始笔记链接"], "")

    def test_three_urls_use_indexes_1_2_3(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4\nhttps://cdn.example.com/3.mp4")
        self.assertEqual([t["fields"]["视频序号"] for t in store.tasks], [1, 2, 3])

    def test_duplicate_trigger_creates_nothing_twice(self):
        store = FakeStore()
        urls = "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4"
        enqueue_video_bundle(store, "rec-1", urls)
        result = enqueue_video_bundle(store, "rec-1", urls)
        self.assertEqual(len(store.tasks), 2)
        self.assertEqual((result["created_count"], result["existing_count"]), (0, 2))

    def test_duplicate_trigger_preserves_completed_parent_status(self):
        store = FakeStore()
        url = "https://cdn.example.com/1.mp4"
        enqueue_video_bundle(store, "rec-1", url)
        store.tasks[0]["fields"].update(
            {"状态": "SUCCEEDED", "视频文件Token": "video-1", "封面文件Token": "cover-1"}
        )
        enqueue_video_bundle(store, "rec-1", url)
        self.assertEqual(store.parent_updates[-1][1]["视频处理状态"], "VIDEO_COMPLETE")
        self.assertEqual(store.parent_updates[-1][1]["视频处理进度"], "1 / 1")

    def test_concurrent_duplicate_trigger_is_serialized_in_one_api_process(self):
        store = FakeStore()
        url = "https://cdn.example.com/1.mp4"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: enqueue_video_bundle(store, "rec-1", url), range(2)))
        self.assertEqual(len(store.tasks), 1)
        self.assertEqual(sorted(result["created_count"] for result in results), [0, 1])

    def test_existing_feishu_payload_is_accepted_by_route(self):
        store = FakeStore()
        api = FastAPI()
        XHS.setup_routes(object.__new__(XHS), api)
        with patch("source.application.app.FeishuVideoTaskStore", return_value=store):
            response = TestClient(api).post(
                "/feishu_upload_video_bundle",
                json={
                    "video_url": "https://cdn.example.com/1.mp4",
                    "record_id": "rec-1",
                    "cover_field": "视频封面",
                    "video_field": "原视频",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "queued")


class AggregateTests(unittest.TestCase):
    def test_tokens_remain_paired_and_sorted(self):
        fields = aggregate_parent_tasks([
            {"fields": {"视频序号": 2, "状态": "SUCCEEDED", "视频文件Token": "v2", "封面文件Token": "c2"}},
            {"fields": {"视频序号": 1, "状态": "SUCCEEDED", "视频文件Token": "v1", "封面文件Token": "c1"}},
        ])
        self.assertEqual(fields["原视频"], [{"file_token": "v1"}, {"file_token": "v2"}])
        self.assertEqual(fields["视频封面"], [{"file_token": "c1"}, {"file_token": "c2"}])
        self.assertEqual(fields["视频处理进度"], "2 / 2")

    def test_feishu_text_tokens_are_normalized_for_parent_attachments(self):
        fields = aggregate_parent_tasks([
            {
                "fields": {
                    "视频序号": 1,
                    "状态": "SUCCEEDED",
                    "视频文件Token": [{"text": "v1", "type": "text"}],
                    "封面文件Token": [{"text": "c1", "type": "text"}],
                }
            }
        ])
        self.assertEqual(fields["原视频"], [{"file_token": "v1"}])
        self.assertEqual(fields["视频封面"], [{"file_token": "c1"}])
        self.assertEqual(fields["视频处理进度"], "1 / 1")

    def test_new_enqueue_parent_is_awaiting_not_processing(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        self.assertEqual(store.parent_updates[-1][1]["视频处理状态"], "AWAITING")

    def test_pending_without_running_is_awaiting(self):
        fields = aggregate_parent_tasks([
            {"fields": {"视频序号": 1, "状态": "PENDING"}},
            {"fields": {"视频序号": 2, "状态": "PENDING"}},
        ])
        self.assertEqual(fields["视频处理状态"], "AWAITING")

    def test_running_present_is_processing(self):
        fields = aggregate_parent_tasks([
            {"fields": {"视频序号": 1, "状态": "RUNNING"}},
            {"fields": {"视频序号": 2, "状态": "PENDING"}},
        ])
        self.assertEqual(fields["视频处理状态"], "PROCESSING")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_processor_downloads_plain_url_from_feishu_text_field(self):
        store = FakeStore()
        captured = []

        class Helpers:
            @staticmethod
            def infer_media_content_type(sample, declared_type):
                return "video/mp4"

            @staticmethod
            def is_video_media(filename, content_type):
                return True

        processor = SingleVideoProcessor(store, Helpers())

        def download(url, destination):
            captured.append(url)
            destination.write_bytes(b"video-bytes")
            return "video/mp4", "11"

        processor._download = download
        processor._cover = lambda video_path, cover_path: cover_path.write_bytes(b"cover")
        processor._upload = lambda path, filename, content_type: f"token-{filename}"

        await processor(
            {
                "record_id": "task-1",
                "fields": {
                    "视频直链": [
                        {"text": "https://example.com/a.mp4", "type": "text"}
                    ],
                    "视频序号": 1,
                },
            }
        )

        self.assertEqual(captured, ["https://example.com/a.mp4"])

    async def test_feishu_text_parent_id_is_used_for_parent_update(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        store.tasks[0]["fields"]["父素材记录ID"] = [
            {"text": "rec-1", "type": "text"}
        ]

        async def process(task):
            return "v1", "c1"

        worker = VideoTaskWorker(store, process, dispatch_interval_seconds=0)
        await worker.run_once()
        self.assertEqual(store.parent_updates[-1][0], "rec-1")

    async def test_feishu_text_note_url_is_used_for_refresh(self):
        seen = []
        store = FakeStore()

        async def refresh(note_url):
            seen.append(note_url)
            return ["https://example.com/fresh.mp4"]

        processor = SingleVideoProcessor(store, object(), refresh)
        store.tasks.append({"record_id": "task-1", "fields": {}})
        task = {
            "record_id": "task-1",
            "fields": {
                "原始笔记链接": [
                    {"text": "https://www.xiaohongshu.com/explore/1", "type": "text"}
                ],
                "视频序号": 1,
            },
        }

        self.assertEqual(await processor._refresh(task), "https://example.com/fresh.mp4")
        self.assertEqual(seen, ["https://www.xiaohongshu.com/explore/1"])

    async def test_refresh_exception_becomes_manual_refresh_candidate(self):
        async def broken_refresh(note_url):
            raise RuntimeError("解析服务不可用")

        processor = SingleVideoProcessor(FakeStore(), object(), broken_refresh)
        task = {
            "record_id": "task-1",
            "fields": {"原始笔记链接": "https://www.xiaohongshu.com/explore/1", "视频序号": 1},
        }
        with self.assertRaises(StaleVideoUrlError):
            await processor._refresh(task)

    async def test_failure_does_not_block_next_task(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4")
        seen = []

        async def process(task):
            index = task["fields"]["视频序号"]
            seen.append(index)
            if index == 1:
                raise RuntimeError("失败")
            return "v2", "c2"

        worker = VideoTaskWorker(store, process)
        await worker.run_once()
        await worker.run_once()
        self.assertEqual(seen, [1, 2])
        self.assertEqual([t["fields"]["状态"] for t in store.tasks], ["RETRY_WAIT", "SUCCEEDED"])

    async def test_concurrent_calls_still_process_one_at_a_time(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4\nhttps://cdn.example.com/3.mp4")
        active = maximum = 0

        async def process(task):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            index = task["fields"]["视频序号"]
            return f"v{index}", f"c{index}"

        workers = [VideoTaskWorker(store, process) for _ in range(3)]
        await asyncio.gather(*(worker.run_once() for worker in workers))
        self.assertEqual(maximum, 1)

    async def test_failure_below_max_retries_stays_retry_wait(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")

        async def process(task):
            raise RuntimeError("普通异常")

        worker = VideoTaskWorker(
            store, process, max_retries=3, retry_base_seconds=0, dispatch_interval_seconds=0,
        )
        await worker.run_once()
        self.assertEqual(store.tasks[0]["fields"]["状态"], "RETRY_WAIT")
        self.assertEqual(store.tasks[0]["fields"]["重试次数"], 1)

    async def test_failure_at_max_retries_becomes_failed(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        # 预置重试次数使其已达上限的前一次
        store.tasks[0]["fields"]["重试次数"] = 2
        store.tasks[0]["fields"]["状态"] = "RETRY_WAIT"
        store.tasks[0]["fields"]["下次重试时间"] = 0

        async def process(task):
            raise RuntimeError("普通异常")

        worker = VideoTaskWorker(
            store, process, max_retries=3, retry_base_seconds=0, dispatch_interval_seconds=0,
        )
        await worker.run_once()
        self.assertEqual(store.tasks[0]["fields"]["状态"], "FAILED")
        self.assertEqual(store.tasks[0]["fields"]["重试次数"], 3)

    async def test_run_until_idle_respects_max_tasks(self):
        store = FakeStore()
        enqueue_video_bundle(
            store,
            "rec-1",
            "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4\nhttps://cdn.example.com/3.mp4",
        )

        async def process(task):
            index = task["fields"]["视频序号"]
            return f"v{index}", f"c{index}"

        worker = VideoTaskWorker(store, process, dispatch_interval_seconds=0)
        processed = await worker.run_until_idle(max_tasks=2)
        self.assertEqual(processed, 2)
        self.assertEqual(
            [t["fields"]["状态"] for t in store.tasks],
            ["SUCCEEDED", "SUCCEEDED", "PENDING"],
        )

    async def test_run_until_idle_continues_after_failure(self):
        store = FakeStore()
        enqueue_video_bundle(
            store,
            "rec-1",
            "https://cdn.example.com/1.mp4\nhttps://cdn.example.com/2.mp4",
        )
        seen = []

        async def process(task):
            index = task["fields"]["视频序号"]
            seen.append(index)
            if index == 1:
                raise RuntimeError("失败")
            return "v2", "c2"

        worker = VideoTaskWorker(store, process, dispatch_interval_seconds=0)
        processed = await worker.run_until_idle(max_tasks=2)
        self.assertEqual(processed, 2)
        self.assertEqual(seen, [1, 2])
        self.assertEqual([t["fields"]["状态"] for t in store.tasks], ["RETRY_WAIT", "SUCCEEDED"])

    def test_parse_max_tasks_defaults_to_24_for_invalid_values(self):
        self.assertEqual(parse_max_tasks(None), 24)
        self.assertEqual(parse_max_tasks(""), 24)
        self.assertEqual(parse_max_tasks("abc"), 24)
        self.assertEqual(parse_max_tasks("0"), 24)
        self.assertEqual(parse_max_tasks("25"), 24)
        self.assertEqual(parse_max_tasks("12"), 12)


class RetryVideoTaskTests(unittest.TestCase):
    def test_retry_uses_real_record_id_not_field(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        store.tasks[0]["fields"].update({"状态": "FAILED", "最后错误": "测试失败"})
        result = retry_video_task(store, "task-1")
        self.assertTrue(result["success"])
        self.assertEqual(result["previous_status"], "FAILED")
        self.assertEqual(result["new_status"], "PENDING")
        self.assertEqual(store.tasks[0]["fields"]["状态"], "PENDING")

    def test_retry_missing_task_raises_clear_error(self):
        store = FakeStore()
        with self.assertRaises(ValueError):
            retry_video_task(store, "nonexistent-id")

    def test_retry_rejects_non_failed_status(self):
        store = FakeStore()
        enqueue_video_bundle(store, "rec-1", "https://cdn.example.com/1.mp4")
        # 默认状态 PENDING 不可重试
        with self.assertRaises(ValueError):
            retry_video_task(store, "task-1")


if __name__ == "__main__":
    unittest.main()
