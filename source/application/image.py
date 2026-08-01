from source.expansion import Namespace

from .request import Html

__all__ = ["Image"]


class Image:
    @classmethod
    def get_image_link(cls, data: Namespace, format_: str) -> tuple[list, list]:
        images = data.safe_extract("imageList", [])
        live_link = cls.__get_live_link(images)
        if not any(
            token_list := [
                cls.__extract_image_token(Namespace.object_extract(i, "urlDefault"))
                for i in images
            ]
        ):
            token_list = [
                cls.__extract_image_token(Namespace.object_extract(i, "url"))
                for i in images
            ]
        match format_:
            case "png" | "webp" | "jpeg" | "heic" | "avif":
                return [
                    Html.format_url(
                        cls.__generate_fixed_link(
                            i,
                            format_,
                        )
                    )
                    for i in token_list
                ], live_link
            case "auto":
                return [
                    Html.format_url(cls.__generate_auto_link(i)) for i in token_list
                ], live_link
            case _:
                raise ValueError

    @staticmethod
    def __generate_auto_link(token: str) -> str:
        return f"https://sns-img-bd.xhscdn.com/{token}"

    @staticmethod
    def __generate_fixed_link(
        token: str,
        format_: str,
    ) -> str:
        return f"https://ci.xiaohongshu.com/{token}?imageView2/format/{format_}"

    @staticmethod
    def __extract_image_token(url: str) -> str:
        return "/".join(url.split("/")[5:]).split("!")[0]

    @classmethod
    def __get_live_link(cls, items: list) -> list:
        return [cls.__extract_live_link(item) for item in items]

    @classmethod
    def __extract_live_link(cls, item) -> str | None:
        preferred_paths = (
            # Current XHS live-photo fields observed in production.
            "stream.EF4[0].masterUrl",
            "stream.EF4[0].backupUrls[0]",
            # Backward-compatible fields used by earlier page payloads.
            "stream.h264[0].masterUrl",
            "stream.h264[0].backupUrls[0]",
            "stream.h265[0].masterUrl",
            "stream.h265[0].backupUrls[0]",
        )

        for path in preferred_paths:
            if link := cls.__format_live_link(
                Namespace.object_extract(item, path)
            ):
                return link

        stream = Namespace.object_extract(item, "stream")
        try:
            stream_variants = vars(stream).values()
        except TypeError:
            return None

        # Unknown stream keys are inspected as a final compatibility fallback.
        for variants in stream_variants:
            if not isinstance(variants, list):
                continue
            for variant in variants:
                for path in ("masterUrl", "backupUrls[0]"):
                    if link := cls.__format_live_link(
                        Namespace.object_extract(variant, path)
                    ):
                        return link

        return None

    @staticmethod
    def __format_live_link(value) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return Html.format_url(value) or None
