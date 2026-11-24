"""Bounded streaming downloads for photographs stored by Telegram."""

import asyncio
import logging
import re
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)
MAX_PHOTO_BYTES = 10_000_000


async def download_telegram_photo(bot, file_id):
    remote = await bot.get_file(file_id)
    if remote.file_size is not None and remote.file_size > MAX_PHOTO_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_PHOTO_BYTES, actual_size=remote.file_size)

    url = remote.file_path
    expected_prefix = f"/file/bot{bot.token}/"
    parts = urlsplit(url or "")
    suffix = parts.path.removeprefix(expected_prefix)
    if (parts.scheme != "https" or parts.netloc != "api.telegram.org"
            or not parts.path.startswith(expected_prefix) or not suffix
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", suffix)
            or any(part in {"", ".", ".."} for part in suffix.split("/"))
            or parts.query or parts.fragment):
        logger.warning("Telegram returned an invalid file path")
        raise web.HTTPBadGateway(text="دریافت عکس از تلگرام انجام نشد.")

    timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            async with session.get(url, allow_redirects=False, headers={"Accept-Encoding": "identity"}) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise web.HTTPBadGateway(text="دریافت عکس از تلگرام انجام نشد.")
                if response.content_length is not None and response.content_length > MAX_PHOTO_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_PHOTO_BYTES, actual_size=response.content_length)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > MAX_PHOTO_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_PHOTO_BYTES, actual_size=len(data) + len(chunk))
                    data.extend(chunk)
                return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Telegram photo download failed: %s", type(exc).__name__)
        raise web.HTTPBadGateway(text="دریافت عکس از تلگرام انجام نشد.") from exc
