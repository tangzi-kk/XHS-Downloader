from re import compile

from .app import XHS

XHS.SHORT = compile(
    r"(?:https?://)?(?:www\.)?(?:xhslink|xhsurl)\.(?:cn|com)/"
    r"(?:(?:o|m)/)?[A-Za-z0-9_-]+"
    r"(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?#\-\[\]]*)?"
)

__all__ = ["XHS"]
