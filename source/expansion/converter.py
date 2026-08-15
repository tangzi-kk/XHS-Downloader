from json import JSONDecodeError, loads
from re import compile
from typing import Union

from lxml.etree import HTML
from yaml import YAMLError, safe_load

__all__ = ["Converter", "InitialStateParseError"]


class InitialStateParseError(ValueError):
    """Stable, non-sensitive error for an unreadable XHS initial state."""

    code = "initial_state_parse_error"

    def __init__(
        self,
        *,
        json_error: JSONDecodeError | None = None,
        yaml_error: YAMLError | None = None,
        reason: str = "invalid_payload",
    ):
        self.json_error = json_error
        self.yaml_error = yaml_error
        self.reason = reason
        super().__init__(self.code)

    @staticmethod
    def _location(error: Exception) -> dict | None:
        if isinstance(error, JSONDecodeError):
            return {"line": error.lineno, "column": error.colno}
        mark = getattr(error, "problem_mark", None)
        if mark is None:
            return None
        return {"line": mark.line + 1, "column": mark.column + 1}

    def as_detail(self) -> dict:
        """Return a safe API detail without echoing page content or secrets."""
        detail = {
            "code": self.code,
            "message": "小红书初始状态不是可解析的 JSON 或 YAML 对象",
            "reason": self.reason,
        }
        if self.json_error:
            detail["json"] = {
                "status": "invalid",
                "location": self._location(self.json_error),
            }
        if self.yaml_error:
            detail["yaml"] = {
                "status": "invalid",
                "location": self._location(self.yaml_error),
            }
        return detail


class Converter:
    YAML_ILLEGAL = compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    INITIAL_STATE_PREFIX = compile(r"^window\.__INITIAL_STATE__\s*=\s*")
    TRAILING_SEMICOLON = compile(r";\s*$")
    INITIAL_STATE = "//script/text()"
    PC_KEYS_LINK = (
        "note",
        "noteDetailMap",
        "[-1]",
        "note",
    )
    PHONE_KEYS_LINK = (
        "noteData",
        "data",
        "noteData",
    )

    def run(self, content: str) -> dict:
        return self._filter_object(self._convert_object(self._extract_object(content)))

    def _extract_object(self, html: str) -> str:
        if not html:
            return ""
        html_tree = HTML(html)
        scripts = html_tree.xpath(self.INITIAL_STATE)
        return self.get_script(scripts)

    @classmethod
    def _convert_object(cls, text: str) -> dict:
        cleaned = cls._clean_initial_state(text)
        if not cleaned:
            raise InitialStateParseError(reason="empty_payload")

        try:
            data = loads(cleaned)
        except JSONDecodeError as json_error:
            try:
                data = safe_load(cleaned)
            except YAMLError as yaml_error:
                raise InitialStateParseError(
                    json_error=json_error,
                    yaml_error=yaml_error,
                ) from yaml_error
        if not isinstance(data, dict):
            raise InitialStateParseError(
                reason="top_level_object_required",
            )
        return data

    @classmethod
    def _clean_initial_state(cls, text: str) -> str:
        if not isinstance(text, str):
            raise InitialStateParseError(reason="payload_must_be_text")
        cleaned = cls.YAML_ILLEGAL.sub("", text.strip())
        cleaned = cls.INITIAL_STATE_PREFIX.sub("", cleaned, count=1).strip()
        return cls.TRAILING_SEMICOLON.sub("", cleaned, count=1).strip()

    @classmethod
    def _filter_object(cls, data: dict) -> dict:
        return (
            cls.deep_get(data, cls.PHONE_KEYS_LINK)
            or cls.deep_get(data, cls.PC_KEYS_LINK)
            or {}
        )

    @classmethod
    def deep_get(cls, data: dict, keys: list | tuple, default=None):
        if not data:
            return default
        try:
            for key in keys:
                if key.startswith("[") and key.endswith("]"):
                    data = cls.safe_get(data, int(key[1:-1]))
                else:
                    data = data[key]
            return data
        except (KeyError, IndexError, ValueError, TypeError):
            return default

    @staticmethod
    def safe_get(data: Union[dict, list, tuple, set], index: int):
        if isinstance(data, dict):
            return list(data.values())[index]
        elif isinstance(data, list | tuple | set):
            return data[index]
        raise TypeError

    @staticmethod
    def get_script(scripts: list) -> str:
        scripts.reverse()
        return next(
            (
                script
                for script in scripts
                if script.startswith("window.__INITIAL_STATE__")
            ),
            "",
        )
