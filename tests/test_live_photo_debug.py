import os
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from source.application import XHS
from source.expansion import Namespace
from source.module.settings import Settings


class TestLivePhotoDebug(unittest.TestCase):
    def setUp(self):
        os.environ["COZE_API_TOKEN"] = "test_debug_token"
        os.environ["XHS_COOKIE"] = "web_session=secret_cookie;"
        self.app = FastAPI()
        self.xhs = XHS(**Settings().default)
        self.xhs.setup_routes(self.app)
        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ.pop("COZE_API_TOKEN", None)
        os.environ.pop("XHS_COOKIE", None)

    def test_debug_route_returns_sanitized_live_photo_candidates(self):
        canonical_url = (
            "https://www.xiaohongshu.com/discovery/item/note123"
            "?xsec_token=secret_xsec&source=share"
        )
        raw_data = {
            "imageList": [
                {
                    "urlDefault": "https://ci.xiaohongshu.com/image-token?secret=1",
                    "stream": {
                        "h264": [
                            {
                                "masterUrl": (
                                    "https://sns-video-bd.xhscdn.com/live123.mp4"
                                    "?xsec_token=video_secret"
                                )
                            }
                        ]
                    },
                }
            ]
        }
        self.xhs.extract_links = AsyncMock(return_value=[canonical_url])
        self.xhs._get_html_data = AsyncMock(
            return_value=("note123", Namespace(raw_data))
        )

        response = self.client.post(
            "/xhs/debug/live-photo",
            json={"url": "https://xhslink.cn/o/test"},
            headers={"Authorization": "Bearer test_debug_token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["note_id"], "note123")
        self.assertEqual(body["image_count"], 1)
        self.assertEqual(body["video_candidate_count"], 1)
        self.assertEqual(
            body["canonical_url"],
            "https://www.xiaohongshu.com/discovery/item/note123",
        )
        self.assertEqual(
            body["candidate_urls"][0]["url"],
            "https://sns-video-bd.xhscdn.com/live123.mp4",
        )
        serialized = response.text
        self.assertNotIn("xsec_token", serialized)
        self.assertNotIn("secret_xsec", serialized)
        self.assertNotIn("video_secret", serialized)
        self.assertNotIn("secret_cookie", serialized)

    def test_debug_route_rejects_missing_authorization(self):
        response = self.client.post(
            "/xhs/debug/live-photo",
            json={"url": "https://xhslink.cn/o/test"},
        )
        self.assertEqual(response.status_code, 401)

    def test_debug_route_rejects_unrecognized_link(self):
        self.xhs.extract_links = AsyncMock(return_value=[])
        response = self.client.post(
            "/xhs/debug/live-photo",
            json={"url": "https://example.com/not-xhs"},
            headers={"Authorization": "Bearer test_debug_token"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
