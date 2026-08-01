from re import compile

from .app import XHS
from .coze_async import install_coze_async_route

XHS.SHORT = compile(
    r"(?:https?://)?(?:www\.)?(?:xhslink|xhsurl)\.(?:cn|com)/"
    r"(?:(?:o|m)/)?[A-Za-z0-9_-]+"
    r"(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?#\-\[\]]*)?"
)

install_coze_async_route(XHS)

__all__ = ["XHS"]
