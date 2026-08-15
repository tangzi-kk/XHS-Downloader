import pytest

from source.expansion.converter import Converter, InitialStateParseError


def test_json_initial_state_strips_exact_prefix_and_semicolon():
    payload = 'window.__INITIAL_STATE__ = {"note": {"title": "json"}};'

    assert Converter._convert_object(payload) == {"note": {"title": "json"}}


def test_legacy_yaml_initial_state_is_still_supported():
    payload = "window.__INITIAL_STATE__={note: {title: legacy}};"

    assert Converter._convert_object(payload) == {"note": {"title": "legacy"}}


def test_malformed_initial_state_has_stable_non_sensitive_error():
    payload = "window.__INITIAL_STATE__={note: new Map([])};"

    with pytest.raises(InitialStateParseError) as caught:
        Converter._convert_object(payload)

    error = caught.value
    assert str(error) == "initial_state_parse_error"
    detail = error.as_detail()
    assert detail["code"] == "initial_state_parse_error"
    assert detail["yaml"]["status"] == "invalid"
    assert "new Map" not in repr(detail)
