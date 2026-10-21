from starlette.middleware.gzip import GZipMiddleware


class MediaSafeGZipMiddleware(GZipMiddleware):
    """Byte ranges and MP4 fragments must reach the decoder without gzip."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/video/stream/"):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)
