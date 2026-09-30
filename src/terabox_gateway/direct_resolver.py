"""Resolve share metadata directly when the worker cannot extract jsToken."""

import re
import asyncio
import json
import hashlib
import hmac
import time
import uuid
from urllib.parse import unquote, urljoin, urlencode, urlparse

import aiohttp
from yarl import URL

from .config import headers

MEDIA_DOMAINS = (
    "terabox.com", "terabox.app", "1024tera.com", "1024terabox.com",
    "freeterabox.com", "teraboxcdn.com", "baidupcs.com", "teraboxlink.com",
    "terasharelink.com", "terafileshare.com", "teraboxshare.com",
    "terasharefile.com", "teraboxurl.com",
)


def valid_media_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        return (parsed.scheme == "https" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password
                and any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS))
    except ValueError:
        return False


def stream_signature(browser_id: str, timestamp: int) -> str:
    # TeraBox's web player uses HMAC-SHA1 over clienttype/channel/browserid/time.
    message = f"0dubox{browser_id}{timestamp}".encode()
    return hmac.new(b"iuuPc64E4Fhn0rTXEzrnbLph0o5qyEEa", message, hashlib.sha1).hexdigest()


def rewrite_playlist(content: str, upstream_url: str, gateway_url: str) -> str:
    def rewrite(uri: str) -> str:
        absolute = urljoin(upstream_url, uri)
        if not valid_media_url(absolute):
            raise ValueError("Unsupported media host")
        return gateway_url + "?" + urlencode({"mode": "segment", "direct": "1", "url": absolute})

    lines = []
    for line in content.splitlines():
        if line.startswith("#"):
            line = re.sub(r'URI="([^\"]+)"', lambda m: 'URI="' + rewrite(m.group(1)) + '"', line)
        elif line.strip():
            line = rewrite(line.strip())
        lines.append(line)
    return "\n".join(lines) + "\n"


async def stream_direct(surl: str, cookies: dict, quality: str, gateway_url: str, fid: str = "") -> dict:
    if surl.startswith("1") and len(surl) == 23:
        surl = surl[1:]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", surl):
        return {"error": "Invalid share ID", "status_code": 400}
    if quality not in {"M3U8_AUTO_360", "M3U8_AUTO_480", "M3U8_AUTO_720", "M3U8_AUTO_1080"}:
        return {"error": "Unsupported streaming quality", "status_code": 400}
    base = "https://www.terabox.com"
    page_url = f"{base}/s/1{surl}"
    session_cookies = dict(cookies)
    session_cookies.setdefault("browserid", uuid.uuid4().hex)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    try:
        async with aiohttp.ClientSession(
            connector=connector, cookies=session_cookies, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10), trust_env=True,
        ) as session:
            async with session.get(page_url) as response:
                if response.status != 200:
                    return {"error": "TeraBox share page unavailable", "status_code": 502}
                token = extract_js_token(await response.text())
            common = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0", "jsToken": token}
            async with session.get(
                f"{base}/api/shorturlinfo", params={**common, "shorturl": "1" + surl, "root": "1"},
                headers={"Referer": page_url},
            ) as response:
                data = await response.json()
            if data.get("errno") != 0 or not data.get("list"):
                return {"error": "TeraBox did not return streaming metadata", "errno": data.get("errno"), "status_code": 502}
            files = [item for item in data["list"] if str(item.get("isdir", 0)) == "0"]
            item = next((item for item in files if str(item.get("fs_id")) == str(fid)), None) if fid else next(iter(files), None)
            if not item:
                return {"error": "No matching video in this share", "status_code": 404}
            timestamp = int(time.time())
            current = session.cookie_jar.filter_cookies(URL(base))
            browser_id = current["browserid"].value
            params = {**common, "uk": str(data["uk"]), "shareid": str(data["shareid"]),
                      "fid": str(item["fs_id"]), "type": quality, "timestamp": str(timestamp),
                      "sign": stream_signature(browser_id, timestamp), "esl": "1", "isplayer": "1", "ehps": "1"}
            # Match the web player's bounded retries for its transient 31341 response.
            for attempt in range(5):
                async with session.get(f"{base}/share/streaming", params=params, headers={"Referer": page_url}) as response:
                    body = await response.text()
                    if response.status == 200 and body.lstrip().startswith("#EXTM3U"):
                        return {"content": rewrite_playlist(body, str(response.url), gateway_url), "status": 200,
                                "headers": {"Cache-Control": "no-store"}, "content_type": "application/vnd.apple.mpegurl"}
                    try:
                        errno = json.loads(body).get("errno")
                    except (ValueError, AttributeError):
                        errno = None
                if errno != 31341 or attempt == 4:
                    return {"error": "TeraBox rejected the streaming request", "errno": errno,
                            "code": "direct_stream_rejected", "status_code": 502}
                await asyncio.sleep(3 * (attempt + 1))
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        return {"error": "Direct TeraBox streaming failed", "code": "direct_stream_failed", "status_code": 502}


async def media_direct(url: str, gateway_url: str, range_header: str = "") -> dict:
    """Fetch only permitted media hosts, validating every redirect."""
    if not valid_media_url(url):
        return {"error": "Unsupported media URL", "status_code": 400}
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    media_headers = {**headers, "Referer": "https://www.terabox.com/"}
    if range_header:
        media_headers["Range"] = range_header
    try:
        async with aiohttp.ClientSession(connector=connector, headers=media_headers,
                timeout=aiohttp.ClientTimeout(total=25, connect=10), trust_env=True) as session:
            for _ in range(4):
                if not valid_media_url(url):
                    return {"error": "Unsupported media redirect", "status_code": 400}
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status not in {200, 206}:
                        return {"error": "TeraBox media request failed", "status_code": 502}
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            return {"error": "Media segment exceeds the gateway response limit", "status_code": 502}
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
                    if content.lstrip().startswith(b"#EXTM3U"):
                        content = rewrite_playlist(content.decode(), url, gateway_url).encode()
                        content_type = "application/vnd.apple.mpegurl"
                    output_headers = {k: v for k, v in response.headers.items()
                                      if k.lower() in {"content-range", "accept-ranges"}}
                    return {"content": content, "status": response.status, "headers": output_headers,
                            "content_type": content_type}
            return {"error": "Too many media redirects", "status_code": 502}
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return {"error": "Direct media fetch failed", "status_code": 502}


def extract_js_token(html: str) -> str:
    """Read the token assignment without evaluating remote JavaScript."""
    scripts = re.findall(r"decodeURIComponent\(`([^`]+)`\)", html)
    for script in [html] + [unquote(item) for item in scripts]:
        for pattern in (
            r"window\.jsToken\s*=\s*['\"]([^'\"]+)['\"]",
            r"fn\(\s*['\"]([^'\"]+)['\"]\s*\)",
        ):
            if pattern.startswith("fn") and "window.jsToken" not in script:
                continue
            match = re.search(pattern, script)
            if match:
                return match.group(1)
    raise ValueError("TeraBox did not provide a jsToken")


def token_extraction_failed(result: dict) -> bool:
    """Limit the direct fallback to known token extraction errors."""
    details = result.get("details")
    code = result.get("code")
    if isinstance(details, dict):
        code = code or details.get("code")
    return code in {"token_extract_failed", "token_extract_failed_all"}


async def resolve_direct(surl: str, cookies: dict, password: str = "") -> dict:
    """Fetch the real share/list response using the configured TeraBox cookies.

    This returns metadata only. It does not manufacture download or HLS links.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", surl):
        return {"error": "Invalid share ID", "status_code": 400}
    base = "https://www.terabox.com"
    page_url = f"{base}/s/1{surl}"
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    try:
        async with aiohttp.ClientSession(
            connector=connector, cookies=cookies, headers=headers,
            timeout=aiohttp.ClientTimeout(total=25, connect=10), trust_env=True,
        ) as session:
            async with session.get(page_url) as response:
                if response.status != 200:
                    return {"error": "TeraBox share page unavailable", "status_code": response.status}
                token = extract_js_token(await response.text())
            params = {
                "app_id": "250528", "web": "1", "channel": "dubox",
                "clienttype": "0", "jsToken": token, "shorturl": surl,
                "root": "1", "page": "1", "num": "100",
                "order": "asc", "by": "name",
            }
            if password:
                params["pwd"] = password
            async with session.get(
                f"{base}/share/list", params=params, headers={"Referer": page_url}
            ) as response:
                if response.status != 200:
                    return {"error": "TeraBox share API unavailable", "status_code": response.status}
                data = await response.json()
            if not isinstance(data, dict) or data.get("errno") != 0 or not isinstance(data.get("list"), list):
                return {"error": "TeraBox did not return a valid file list", "status_code": 502}
            return {"source": "direct", "upstream": data}
    except (aiohttp.ClientError, TimeoutError, ValueError):
        # Do not expose token-bearing URLs or response bodies in public errors.
        return {"error": "Direct TeraBox resolution failed", "status_code": 502}
