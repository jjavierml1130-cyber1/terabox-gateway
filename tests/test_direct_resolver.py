import asyncio
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest

from terabox_gateway import api as api_module
from terabox_gateway.direct_resolver import extract_js_token, token_extraction_failed, rewrite_playlist, valid_media_url, stream_signature


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
    direct = AsyncMock(return_value={"error": "TeraBox rejected the streaming request", "status_code": 502})
    monkeypatch.setattr(api_module, "_proxy_request", proxy)
    monkeypatch.setattr(api_module, "stream_direct", direct)
    with api_module.app.test_request_context("/api?mode=stream&surl=abc"):
        _, status = asyncio.run(api_module.api.__wrapped__())
    assert status == 502
    direct.assert_awaited_once()


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


def test_playlist_rewrites_segments_nested_playlists_and_keys():
    from urllib.parse import urlparse, parse_qs
    text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:4,\nvideo.ts\nchild.m3u8\n'
    out = rewrite_playlist(text, "https://dm.terabox.com/path/master.m3u8", "https://gateway.example/api")
    assert 'URI="https://gateway.example/api?' in out
    for line in out.splitlines()[3:]:
        params = parse_qs(urlparse(line).query)
        assert params["mode"] == ["segment"]
        assert params["direct"] == ["1"]
        assert params["url"][0].startswith("https://dm.terabox.com/path/")


@pytest.mark.parametrize("url", ["http://dm.terabox.com/v.ts", "https://localhost/v.ts",
    "https://terabox.com.evil.test/v.ts", "https://dm.terabox.com:8443/v.ts", "https://user@dm.terabox.com/v.ts"])
def test_segment_url_rejects_untrusted_hosts(url):
    assert not valid_media_url(url)


def test_stream_signature_matches_official_web_player():
    import hmac, hashlib
    expected = hmac.new(b"iuuPc64E4Fhn0rTXEzrnbLph0o5qyEEa", b"0duboxexample1700000000", hashlib.sha1).hexdigest()
    assert stream_signature("example", 1700000000) == expected


def test_stream_success_returns_hls_not_json(monkeypatch):
    monkeypatch.setattr(api_module, "_proxy_request", AsyncMock(return_value={
        "error": "Failed to extract jsToken", "details": {"code": "token_extract_failed_all"}}))
    monkeypatch.setattr(api_module, "stream_direct", AsyncMock(return_value={
        "content": "#EXTM3U\n#EXTINF:4,\nhttps://gateway.example/api?mode=segment\n", "status": 200,
        "headers": {}, "content_type": "application/vnd.apple.mpegurl"}))
    with api_module.app.test_request_context("/api?mode=stream&surl=abc"):
        response = asyncio.run(api_module.api.__wrapped__())
    assert response.status_code == 200
    assert response.mimetype == "application/vnd.apple.mpegurl"
    assert response.get_data(as_text=True).startswith("#EXTM3U")


@pytest.mark.parametrize("errors,expected_status", [([31341, 31341], 200), ([130], 502), ([31341] * 5, 502)])
def test_direct_stream_retries_only_transient_errors(monkeypatch, errors, expected_status):
    from http.cookies import SimpleCookie
    from terabox_gateway import direct_resolver as module

    class Response:
        status = 200
        url = "https://www.terabox.com/share/streaming"

        def __init__(self, text="", data=None):
            self.body, self.data = text, data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            return self.body

        async def json(self):
            return self.data

    responses = [Response('window.jsToken="test-token"'), Response(data={
        "errno": 0, "uk": "7", "shareid": "8", "list": [{"fs_id": "9", "isdir": 0}],
    })] + [Response('{"errno":' + str(error) + '}') for error in errors]
    if expected_status == 200:
        responses.append(Response('#EXTM3U\n#EXTINF:4,\nhttps://v4.freeterabox.com/video.ts\n'))

    class Session:
        cookie_jar = type("Jar", (), {"filter_cookies": lambda self, url: SimpleCookie("browserid=test-id")})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **kwargs: Session())
    monkeypatch.setattr(module.aiohttp, "TCPConnector", lambda **kwargs: None)
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    result = asyncio.run(module.stream_direct("abc", {}, "M3U8_AUTO_720", "https://gateway.example/api"))
    assert result.get("status", result.get("status_code")) == expected_status
    assert sleep.await_count == (len(errors) if expected_status == 200 else max(0, len(errors) - 1))
    assert not responses
