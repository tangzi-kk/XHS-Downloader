import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from source.application import XHS
from source.application.coze_async import _running_tasks, _task_states
from source.module.settings import Settings


class FakeAsyncClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        if self.error is not None:
            raise self.error
        return self.response


class TestCozeAsync(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.xhs = XHS(**Settings().default)
        self.xhs.setup_routes(self.app)
        self.client = TestClient(self.app)
        _running_tasks.clear()
        _task_states.clear()
        self.old_token = os.environ.get("COZE_API_TOKEN")
        os.environ["COZE_API_TOKEN"] = "test_coze_token"

    def tearDown(self):
        if self.old_token is None:
            os.environ.pop("COZE_API_TOKEN", None)
        else:
            os.environ["COZE_API_TOKEN"] = self.old_token
        _running_tasks.clear()
        _task_states.clear()

    @property
    def payload(self):
        return {
            "workflow_id": "7665335986419941386",
            "parameters": {
                "record_id": "rec_test",
                "url": "https://xhslink.cn/o/test",
                "note_id": "note_test",
                "title": "title_test",
            },
        }

    def post_enqueue(self, payload=None):
        return self.client.post(
            "/coze/workflow/enqueue",
            json=payload or self.payload,
            headers={"Authorization": "Bearer test_coze_token"},
        )

    @patch(
        "source.application.coze_async._run_coze_workflow",
        new_callable=AsyncMock,
    )
    def test_enqueue_returns_immediately_and_preserves_payload_contract(self, mock_run):
        response = self.post_enqueue()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertFalse(body["duplicate"])
        self.assertTrue(body["task_id"])
        mock_run.assert_awaited_once()
        self.assertEqual(mock_run.await_args.args[0], self.payload)
        self.assertEqual(mock_run.await_args.args[1], "test_coze_token")
        self.assertEqual(mock_run.await_args.args[2], body["task_id"])
        self.assertEqual(_task_states[body["task_id"]]["status"], "queued")

    def test_task_transitions_queued_running_succeeded_and_logs_are_visible(self):
        coze_response = httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "Success",
                "execute_id": "execute-test-1",
                "debug_url": "https://www.coze.cn/work_flow?execute_id=execute-test-1",
                "data": "{\"ok\":true}",
            },
        )
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            return_value=FakeAsyncClient(response=coze_response),
        ):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                response = self.post_enqueue()

        task_id = response.json()["task_id"]
        state = _task_states[task_id]
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["workflow_id"], self.payload["workflow_id"])
        self.assertEqual(state["record_id"], "rec_test")
        self.assertEqual(state["http_status"], 200)
        self.assertEqual(state["coze_code"], 0)
        self.assertEqual(state["coze_message"], "Success")
        self.assertEqual(state["execution_id"], "execute-test-1")
        self.assertIsNotNone(state["created_at"])
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["finished_at"])
        logs = "\n".join(captured.output)
        self.assertIn("coze_task_queued", logs)
        self.assertIn("coze_task_started", logs)
        self.assertIn("coze_task_succeeded", logs)
        self.assertIn(task_id, logs)
        self.assertIn("rec_test", logs)
        self.assertIn("7665335986419941386", logs)
        self.assertNotIn("test_coze_token", logs)

    def test_status_query_without_token_is_rejected(self):
        with patch(
            "source.application.coze_async._run_coze_workflow",
            new_callable=AsyncMock,
        ):
            enqueue_response = self.post_enqueue()

        task_id = enqueue_response.json()["task_id"]
        response = self.client.get(f"/coze/workflow/status/{task_id}")

        self.assertEqual(response.status_code, 401)

    def test_status_query_with_invalid_token_is_rejected(self):
        with patch(
            "source.application.coze_async._run_coze_workflow",
            new_callable=AsyncMock,
        ):
            enqueue_response = self.post_enqueue()

        task_id = enqueue_response.json()["task_id"]
        response = self.client.get(
            f"/coze/workflow/status/{task_id}",
            headers={"Authorization": "Bearer wrong_token"},
        )

        self.assertEqual(response.status_code, 401)

    def test_status_query_with_valid_token_returns_state(self):
        with patch(
            "source.application.coze_async._run_coze_workflow",
            new_callable=AsyncMock,
        ):
            enqueue_response = self.post_enqueue()

        task_id = enqueue_response.json()["task_id"]
        response = self.client.get(
            f"/coze/workflow/status/{task_id}",
            headers={"Authorization": "Bearer test_coze_token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["task_id"], task_id)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["workflow_id"], self.payload["workflow_id"])
        self.assertEqual(body["record_id"], "rec_test")
        self.assertIn("created_at", body)
        self.assertNotIn("test_coze_token", response.text)

    def test_status_query_with_valid_token_returns_404_for_unknown_task(self):
        response = self.client.get(
            "/coze/workflow/status/not-a-real-task",
            headers={"Authorization": "Bearer test_coze_token"},
        )
        self.assertEqual(response.status_code, 404)

    def test_http_error_is_failed_and_response_fields_are_saved(self):
        coze_response = httpx.Response(
            400,
            json={
                "code": 4000,
                "msg": "invalid workflow input",
                "execute_id": "execute-http-error",
                "debug_url": "https://www.coze.cn/debug/http-error",
            },
        )
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            return_value=FakeAsyncClient(response=coze_response),
        ):
            response = self.post_enqueue()

        state = _task_states[response.json()["task_id"]]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["http_status"], 400)
        self.assertEqual(state["coze_code"], 4000)
        self.assertEqual(state["coze_message"], "invalid workflow input")
        self.assertEqual(state["execution_id"], "execute-http-error")
        self.assertEqual(state["error_type"], "http_error")
        self.assertIn("code", state["coze_response_fields"])

    def test_business_code_nonzero_is_failed_even_when_http_is_200(self):
        coze_response = httpx.Response(
            200,
            json={"code": 12345, "msg": "workflow rejected"},
        )
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            return_value=FakeAsyncClient(response=coze_response),
        ):
            response = self.post_enqueue()

        state = _task_states[response.json()["task_id"]]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["http_status"], 200)
        self.assertEqual(state["coze_code"], 12345)
        self.assertEqual(state["coze_message"], "workflow rejected")
        self.assertEqual(state["error_type"], "coze_business_error")

    def test_network_exception_is_exception_and_token_is_redacted(self):
        network_error = RuntimeError("request failed with test_coze_token")
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            return_value=FakeAsyncClient(error=network_error),
        ):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                response = self.post_enqueue()

        task_id = response.json()["task_id"]
        state = _task_states[task_id]
        self.assertEqual(state["status"], "exception")
        self.assertEqual(state["error_type"], "RuntimeError")
        self.assertNotIn("test_coze_token", state["error_message"])
        self.assertIn("[REDACTED]", state["error_message"])
        self.assertNotIn("test_coze_token", "\n".join(captured.output))

    def test_invalid_json_response_is_failed(self):
        coze_response = httpx.Response(
            200,
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            return_value=FakeAsyncClient(response=coze_response),
        ):
            response = self.post_enqueue()

        state = _task_states[response.json()["task_id"]]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["http_status"], 200)
        self.assertEqual(state["error_type"], "invalid_json")
        self.assertIn("not valid JSON", state["error_message"])

    @patch(
        "source.application.coze_async._run_coze_workflow",
        new_callable=AsyncMock,
    )
    def test_running_or_queued_duplicate_does_not_schedule_again(self, mock_run):
        first = self.post_enqueue()
        queued_duplicate = self.post_enqueue()
        _task_states[first.json()["task_id"]]["status"] = "running"
        running_duplicate = self.post_enqueue()

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(queued_duplicate.json()["duplicate"])
        self.assertTrue(running_duplicate.json()["duplicate"])
        self.assertEqual(
            first.json()["task_id"], queued_duplicate.json()["task_id"]
        )
        self.assertEqual(
            first.json()["task_id"], running_duplicate.json()["task_id"]
        )
        mock_run.assert_awaited_once()

    def test_failed_task_can_be_triggered_again(self):
        failed_response = httpx.Response(
            400,
            json={"code": 4001, "msg": "temporary failure"},
        )
        success_response = httpx.Response(
            200,
            json={"code": 0, "msg": "Success", "execute_id": "execute-retry"},
        )
        with patch(
            "source.application.coze_async.httpx.AsyncClient",
            side_effect=[
                FakeAsyncClient(response=failed_response),
                FakeAsyncClient(response=success_response),
            ],
        ):
            first = self.post_enqueue()
            second = self.post_enqueue()

        self.assertFalse(first.json()["duplicate"])
        self.assertFalse(second.json()["duplicate"])
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])
        state = _task_states[second.json()["task_id"]]
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["execution_id"], "execute-retry")

    def test_invalid_authorization_is_rejected(self):
        response = self.client.post(
            "/coze/workflow/enqueue",
            json=self.payload,
            headers={"Authorization": "Bearer wrong_token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_payload_is_rejected(self):
        response = self.client.post(
            "/coze/workflow/enqueue",
            json={"workflow_id": "", "parameters": []},
            headers={"Authorization": "Bearer test_coze_token"},
        )
        self.assertEqual(response.status_code, 400)

    def test_state_serialization_does_not_include_request_token(self):
        with patch(
            "source.application.coze_async._run_coze_workflow",
            new_callable=AsyncMock,
        ):
            response = self.post_enqueue()

        task_id = response.json()["task_id"]
        serialized = json.dumps(_task_states[task_id], ensure_ascii=False)
        self.assertNotIn("test_coze_token", serialized)


if __name__ == "__main__":
    unittest.main()
