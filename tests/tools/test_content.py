"""Tests for the shared content-pagination helper (B12 default cap)."""

from arxiv_mcp_server.tools import content as content_mod
from arxiv_mcp_server.tools.content import paginate_content


def _set_default_cap(monkeypatch, value):
    monkeypatch.setattr(content_mod.settings, "CONTENT_DEFAULT_MAX_CHARS", value)


def test_default_cap_applies_when_max_chars_omitted(monkeypatch):
    _set_default_cap(monkeypatch, 100)
    text = "x" * 250
    page = paginate_content(text, {})
    assert page["returned_chars"] == 100
    assert page["is_truncated"] is True
    assert page["next_start"] == 100
    assert page["content_length"] == 250


def test_explicit_max_chars_overrides_default(monkeypatch):
    _set_default_cap(monkeypatch, 100)
    text = "x" * 250
    page = paginate_content(text, {"max_chars": 250})
    assert page["returned_chars"] == 250
    assert page["is_truncated"] is False
    assert page["next_start"] is None


def test_small_content_unaffected_by_default_cap(monkeypatch):
    _set_default_cap(monkeypatch, 100)
    text = "short paper"
    page = paginate_content(text, {})
    assert page["content"] == text
    assert page["is_truncated"] is False
    assert page["next_start"] is None


def test_zero_cap_disables_default_legacy_full_content(monkeypatch):
    _set_default_cap(monkeypatch, 0)
    text = "x" * 250
    page = paginate_content(text, {})
    assert page["returned_chars"] == 250
    assert page["is_truncated"] is False


def test_default_cap_pages_consistently_via_next_start(monkeypatch):
    _set_default_cap(monkeypatch, 100)
    text = "".join(str(i % 10) for i in range(250))
    first = paginate_content(text, {})
    second = paginate_content(text, {"start": first["next_start"]})
    third = paginate_content(text, {"start": second["next_start"]})
    reassembled = first["content"] + second["content"] + third["content"]
    assert reassembled == text
    assert third["is_truncated"] is False


def test_garbage_default_cap_setting_fails_open(monkeypatch):
    # A non-numeric env value must not break reads — falls back to uncapped.
    _set_default_cap(monkeypatch, "not-a-number")
    text = "x" * 250
    page = paginate_content(text, {})
    assert page["returned_chars"] == 250
