import unittest
import os
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from source.application.app import XHS
from source.module.settings import Settings

class TestCookieSecurity(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.xhs = XHS(**Settings().default)
        self.xhs.setup_routes(self.app)
        self.client = TestClient(self.app)

    @patch("source.application.app.XHS._get_html_data", new_callable=AsyncMock)
    def test_detail_endpoint_sanitizes_cookie_in_response(self, mock_get_html):
        mock_get_html.return_value = ("1234567890", {})
        payload = {
            "url": "https://www.xiaohongshu.com/explore/1234567890",
            "download": False,
            "skip": False,
            "cookie": "web_session=secret_token_12345;"
        }
        response = self.client.post("/xhs/detail", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("params", data)
        self.assertIsNone(data["params"].get("cookie"))
        mock_get_html.assert_called_once()
        # Verify effective_cookie passed to _get_html_data is the request cookie
        call_args = mock_get_html.call_args[0]
        self.assertEqual(call_args[2], "web_session=secret_token_12345;")

    @patch("source.application.app.XHS._get_html_data", new_callable=AsyncMock)
    def test_env_cookie_fallback(self, mock_get_html):
        mock_get_html.return_value = ("1234567890", {})
        os.environ["XHS_COOKIE"] = "web_session=env_secret_token;"
        try:
            payload = {
                "url": "https://www.xiaohongshu.com/explore/1234567890",
                "download": False,
                "skip": False
            }
            response = self.client.post("/xhs/detail", json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("params", data)
            self.assertIsNone(data["params"].get("cookie"))
            mock_get_html.assert_called_once()
            # Verify effective_cookie passed to _get_html_data is the env cookie
            call_args = mock_get_html.call_args[0]
            self.assertEqual(call_args[2], "web_session=env_secret_token;")
        finally:
            os.environ.pop("XHS_COOKIE", None)

    def test_update_cookie_valid_ascii(self):
        headers = self.xhs.html.update_cookie("web_session=valid_token_123;")
        self.assertEqual(headers.get("Cookie"), "web_session=valid_token_123;")

    def test_update_cookie_control_char_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.xhs.html.update_cookie("web_session=bad\r\ntoken;")
        self.assertIn("包含控制字符", str(ctx.exception))

    def test_update_cookie_non_ascii_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.xhs.html.update_cookie("web_session=中文token;")
        self.assertIn("包含非 ASCII 字符", str(ctx.exception))
if __name__ == "__main__":
    unittest.main()
