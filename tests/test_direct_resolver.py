import asyncio
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest

from terabox_gateway import api as api_module
from terabox_gateway.direct_resolver import extract_js_token, token_extraction_failed


def test_decode_token_without_executing_javascript():
    script = 'function fn(a){window.jsToken = a};fn("test-token")'
    assert extract_js_token(f'eval(decodeURIComponent(`{quote(script)}`))') == "test-token"


def test_missing_token_is_rejected():
    with pytest.raises(ValueError):
        extract_js_token("<html>Verification required</html>")


def test_only_token_errors_enable_fallback():
    assert token_extraction_failed({"details": {"code": "token_extract_failed_all"}})
    assert not token_extraction_failed({"details": {"code": "unauthorized"}})


@pytest.mark.parametrize("raw,key", [("1", "upstream"), ("0", "data")])
def test_resolve_falls_back_to_direct_metadata(monkeypatch, raw, key):
    proxy = AsyncMock(return_value={
        "error": "Failed to extract jsToken", "status_code": 403,
        "details": {"code": "token_extract_failed_all"},
    })
    direct = AsyncMock(return_value={
        "source": "direct", "upstream": {"errno": 0, "list": [{"fs_id": "42"}]},
    })
    monkeypatch.setattr(api_module, "_proxy_request", proxy)
    monkeypatch.setattr(api_module, "resolve_direct", direct)
    monkeypatch.setattr(api_module, "load_cookies", lambda: {"ndus": "test-cookie"})
    with api_module.app.test_request_context(
        f"/api?mode=resolve&surl=1BTiUkD8WCjRLgOQoywSbXQ&raw={raw}"
    ):
        response = asyncio.run(api_module.api.__wrapped__())
    assert response.json[key]["list"][0]["fs_id"] == "42"
    direct.assert_awaited_once_with("BTiUkD8WCjRLgOQoywSbXQ", {"ndus": "test-cookie"}, "")
    assert "test-cookie" not in response.get_data(as_text=True)


def test_stream_does_not_misrepresent_metadata_as_video(monkeypatch):
    proxy = AsyncMock(return_value={
        "error": "Failed to extract jsToken", "status_code": 403,
        "details": {"code": "token_extract_failed_all"},
    })
    direct = AsyncMock()
    monkeypatch.setattr(api_module, "_proxy_request", proxy)
    monkeypatch.setattr(api_module, "resolve_direct", direct)
    with api_module.app.test_request_context("/api?mode=stream&surl=abc"):
        _, status = asyncio.run(api_module.api.__wrapped__())
    assert status == 403
    direct.assert_not_awaited()


def test_legacy_refresh_reaches_client_and_preserves_403(monkeypatch):
    fetch = AsyncMock(return_value={
        "error": "Failed to extract jsToken", "status_code": 403,
        "code": "token_extract_failed_all", "message": "Proxy resolution failed",
    })
    monkeypatch.setattr(api_module, "fetch_download_link", fetch)
    url = "https://1024terabox.com/s/1abc"
    with api_module.app.test_request_context("/api", query_string={"url": url, "refresh": "1"}):
        response, status = asyncio.run(api_module.api.__wrapped__())
    assert status == 403
    assert response.json["message"]
    assert response.json["code"] == "token_extract_failed_all"
    fetch.assert_awaited_once_with(url, "", refresh=True)
