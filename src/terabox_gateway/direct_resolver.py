"""Resolve share metadata directly when the worker cannot extract jsToken."""

import re
from urllib.parse import unquote

import aiohttp

from .config import headers


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
