import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from source.application import XHS
from source.application.coze_async import _running_tasks
from source.module.settings import Settings


class TestCozeAsync(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.xhs = XHS(**Settings().default)
        self.xhs.setup_routes(self.app)
        self.client = TestClient(self.app)
        _running_tasks.clear()
        os.environ["COZE_API_TOKEN"] = "test_coze_token"

    def tearDown(self):
        os.environ.pop("COZE_API_TOKEN", None)
        _running_tasks.clear()

    @patch(
        "source.application.coze_async._run_coze_workflow",
        new_callable=AsyncMock,
    )
    def test_enqueue_returns_immediately_and_forwards_payload(self, mock_run):
        payload = {
            "workflow_id": "7665335986419941386",
            "parameters": {
                "record_id": "rec_test",
                "url": "https://xhslink.cn/o/test",
                "note_id": "note_test",
                "title": "title_test",
            },
        }

        response = self.client.post(
            "/coze/workflow/enqueue",
            json=payload,
            headers={"Authorization": "Bearer test_coze_token"},
        )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertFalse(body["duplicate"])
        self.assertTrue(body["task_id"])
        mock_run.assert_awaited_once()
        self.assertEqual(mock_run.await_args.args[0], payload)
        self.assertEqual(mock_run.await_args.args[1], "test_coze_token")

    def test_enqueue_rejects_invalid_authorization(self):
        response = self.client.post(
            "/coze/workflow/enqueue",
            json={
                "workflow_id": "7665335986419941386",
                "parameters": {},
            },
            headers={"Authorization": "Bearer wrong_token"},
        )

        self.assertEqual(response.status_code, 401)

    def test_enqueue_rejects_invalid_payload(self):
        response = self.client.post(
            "/coze/workflow/enqueue",
            json={"workflow_id": "", "parameters": []},
            headers={"Authorization": "Bearer test_coze_token"},
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
