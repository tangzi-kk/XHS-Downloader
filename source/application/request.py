from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from httpx import HTTPError
from httpx import get

from ..module import ERROR, Manager, logging, retry, sleep_time
from ..translation import _

if TYPE_CHECKING:
    from ..module import Manager

__all__ = ["Html"]


class Html:
    def __init__(
        self,
        manager: "Manager",
    ):
        self.print = manager.print
        self.retry = manager.retry
        self.client = manager.request_client
        self.headers = manager.blank_headers
        self.timeout = manager.timeout

    @retry
    async def request_url(
        self,
        url: str,
        content=True,
        cookie: str = None,
        proxy: str = None,
        **kwargs,
    ) -> str:
        if not url.startswith("http"):
            url = f"https://{url}"
        headers = self.update_cookie(
            cookie,
        )
        try:
            match bool(proxy):
                case False:
                    response = await self.__request_url_get(
                        url,
                        headers,
                        **kwargs,
                    )
                    await sleep_time()
                    response.raise_for_status()
                    return (
                        response.text
                        if content
                        else self.__extract_canonical_url(response)
                    )
                case True:
                    response = await self.__request_url_get_proxy(
                        url,
                        headers,
                        proxy,
                        **kwargs,
                    )
                    await sleep_time()
                    response.raise_for_status()
                    return (
                        response.text
                        if content
                        else self.__extract_canonical_url(response)
                    )
                case _:
                    raise ValueError
        except HTTPError as error:
            logging(
                self.print,
                _("网络异常，{0} 请求失败: {1}").format(url, repr(error)),
                ERROR,
            )
            return ""

    @staticmethod
    def __extract_canonical_url(response) -> str:
        candidates = []
        for item in (*response.history, response):
            current_url = str(item.url)
            candidates.append(current_url)
            if location := item.headers.get("location"):
                candidates.append(urljoin(current_url, location))

        for candidate in candidates:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            path = parsed.path
            if host in {
                "xiaohongshu.com",
                "www.xiaohongshu.com",
                "rednote.com",
                "www.rednote.com",
            } and (
                path.startswith("/discovery/item/")
                or path.startswith("/explore/")
                or path.startswith("/user/profile/")
            ):
                return candidate

        return str(response.url)

    @staticmethod
    def format_url(url: str) -> str:
        return bytes(url, "utf-8").decode("unicode_escape")

    def update_cookie(
        self,
        cookie: str = None,
    ) -> dict:
        if not cookie:
            return self.headers.copy()

        if any(ord(char) < 32 or ord(char) == 127 for char in cookie):
            raise ValueError("XHS_COOKIE 包含控制字符，请重新从浏览器请求头复制")

        try:
            cookie.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("XHS_COOKIE 包含非 ASCII 字符，请重新获取") from error

        return self.headers | {"Cookie": cookie}

    async def __request_url_head(
        self,
        url: str,
        headers: dict,
        **kwargs,
    ):
        return await self.client.head(
            url,
            headers=headers,
            **kwargs,
        )

    async def __request_url_head_proxy(
        self,
        url: str,
        headers: dict,
        proxy: str,
        **kwargs,
    ):
        return await self.client.head(
            url,
            headers=headers,
            proxy=proxy,
            follow_redirects=True,
            verify=False,
            timeout=self.timeout,
            **kwargs,
        )

    async def __request_url_get(
        self,
        url: str,
        headers: dict,
        **kwargs,
    ):
        return await self.client.get(
            url,
            headers=headers,
            **kwargs,
        )

    async def __request_url_get_proxy(
        self,
        url: str,
        headers: dict,
        proxy: str,
        **kwargs,
    ):
        return get(
            url,
            headers=headers,
            proxy=proxy,
            follow_redirects=True,
            verify=False,
            timeout=self.timeout,
            **kwargs,
        )
