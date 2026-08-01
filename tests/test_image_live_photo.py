import unittest

from source.application.image import Image
from source.expansion import Namespace


class TestImageLivePhoto(unittest.TestCase):
    def test_current_and_legacy_stream_fields(self):
        data = Namespace(
            {
                "imageList": [
                    {
                        "urlDefault": "https://example.com/a/b/image-one",
                        "stream": {
                            "EF4": [
                                {
                                    "masterUrl": "https://example.com/live-one.mp4"
                                }
                            ]
                        },
                    },
                    {
                        "urlDefault": "https://example.com/a/b/image-two",
                        "stream": {
                            "h264": [
                                {
                                    "masterUrl": "https://example.com/live-two.mp4"
                                }
                            ]
                        },
                    },
                    {
                        "urlDefault": "https://example.com/a/b/image-three"
                    },
                ]
            }
        )

        _, live_links = Image.get_image_link(data, "jpeg")

        self.assertEqual(
            live_links,
            [
                "https://example.com/live-one.mp4",
                "https://example.com/live-two.mp4",
                None,
            ],
        )

    def test_backup_and_unknown_stream_key_fallbacks(self):
        data = Namespace(
            {
                "imageList": [
                    {
                        "urlDefault": "https://example.com/a/b/image-one",
                        "stream": {
                            "EF4": [
                                {
                                    "backupUrls": [
                                        "https://example.com/backup.mp4"
                                    ]
                                }
                            ]
                        },
                    },
                    {
                        "urlDefault": "https://example.com/a/b/image-two",
                        "stream": {
                            "EF9": [
                                {
                                    "masterUrl": "https://example.com/future.mp4"
                                }
                            ]
                        },
                    },
                ]
            }
        )

        _, live_links = Image.get_image_link(data, "jpeg")

        self.assertEqual(
            live_links,
            [
                "https://example.com/backup.mp4",
                "https://example.com/future.mp4",
            ],
        )


if __name__ == "__main__":
    unittest.main()
