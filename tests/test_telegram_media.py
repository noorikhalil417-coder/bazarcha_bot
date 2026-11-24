import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from services.telegram_media import download_telegram_photo


class FakeResponse:
    def __init__(self, chunks, *, status=200, headers=None, length=None):
        self.chunks = chunks
        self.status = status
        self.headers = headers or {}
        self.content_length = length
        self.content = self
        self.read_chunks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.options = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.options = kwargs
        return self.response


class TelegramMediaTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, path="https://api.telegram.org/file/bot123:test/photos/file.jpg", size=None):
        return SimpleNamespace(token="123:test", get_file=AsyncMock(
            return_value=SimpleNamespace(file_path=path, file_size=size)))

    async def test_streamed_data_stops_at_limit_without_reading_remainder(self):
        response = FakeResponse([b"123456", b"789012", b"never read"])
        session = FakeSession(response)
        with patch("services.telegram_media.MAX_PHOTO_BYTES", 10), \
             patch("services.telegram_media.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                await download_telegram_photo(self.bot(), "image")
        self.assertEqual(response.read_chunks, 2)
        self.assertFalse(session.options["allow_redirects"])

    async def test_valid_file_and_reject_redirect_or_untrusted_url(self):
        response = FakeResponse([b"abc", b"def"])
        with patch("services.telegram_media.aiohttp.ClientSession", return_value=FakeSession(response)):
            self.assertEqual(await download_telegram_photo(self.bot(), "image"), b"abcdef")
        for path in ("https://evil.example/file/bot123:test/photos/a.jpg",
                     "https://api.telegram.org/file/bot123:test/../x",
                     "https://api.telegram.org/file/bot123:test/photos/x?token=other"):
            with self.subTest(path=path), self.assertRaises(web.HTTPBadGateway):
                await download_telegram_photo(self.bot(path), "image")
        with patch("services.telegram_media.aiohttp.ClientSession", return_value=FakeSession(FakeResponse([], status=302))):
            with self.assertRaises(web.HTTPBadGateway):
                await download_telegram_photo(self.bot(), "image")

    async def test_rejects_declared_oversize_and_compressed_responses(self):
        with patch("services.telegram_media.MAX_PHOTO_BYTES", 10):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                await download_telegram_photo(self.bot(size=11), "image")
            for response in (FakeResponse([], length=11), FakeResponse([], headers={"Content-Encoding": "gzip"})):
                with patch("services.telegram_media.aiohttp.ClientSession", return_value=FakeSession(response)):
                    with self.assertRaises((web.HTTPRequestEntityTooLarge, web.HTTPBadGateway)):
                        await download_telegram_photo(self.bot(), "image")
