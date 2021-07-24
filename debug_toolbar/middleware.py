import functools
import json
import re
import typing as t
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import NoMatchFound
from starlette.types import ASGIApp

from debug_toolbar.api import render_panel
from debug_toolbar.settings import DebugToolbarSettings
from debug_toolbar.toolbar import DebugToolbar
from debug_toolbar.utils import import_string


def show_toolbar(request: Request, settings: DebugToolbarSettings) -> bool:
    if settings.ALLOWED_IPS is not None:
        remote_addr, _ = request.scope["client"]
        return request.app.debug and remote_addr in settings.ALLOWED_IPS
    return request.app.debug


class DebugToolbarMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, **settings: t.Any) -> None:
        super().__init__(app)
        self.settings = DebugToolbarSettings(**settings)
        self.show_toolbar = import_string(self.settings.SHOW_TOOLBAR_CALLBACK)
        self.router: APIRouter = app  # type: ignore

        while not isinstance(self.router, APIRouter):
            self.router = self.router.app
        try:
            self.router.url_path_for(name="debug_toolbar.render_panel")
        except NoMatchFound:
            self.init_toolbar()

    def init_toolbar(self) -> None:
        self.router.get(
            self.settings.API_URL,
            name="debug_toolbar.render_panel",
        )(self.require_show_toolbar(render_panel))

        self.router.mount(
            self.settings.STATIC_URL,
            StaticFiles(packages=[__package__]),
            name="debug_toolbar.static",
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if (
            not self.show_toolbar(request, self.settings)
            or self.settings.API_URL in request.url.path
        ):
            return await call_next(request)

        toolbar = DebugToolbar(request, call_next, self.settings)
        response = await toolbar.process_request(request)
        content_type = response.headers.get("Content-Type", "")
        is_html = content_type.startswith("text/html")
        is_json = content_type == "application/json"

        if (
            not (is_html or is_json)
            or "gzip" in response.headers.get("Accept-Encoding", "")
            or request.scope.get("endpoint") is None
        ):
            return response

        await toolbar.record_stats(response)
        await toolbar.record_server_timing(response)
        toolbar.generate_server_timing_header(response)

        async for body in response.body_iterator:  # type: ignore
            if not isinstance(body, bytes):
                body = body.encode(response.charset)

        decoded = body.decode(response.charset)

        if is_html:
            pattern = re.escape(self.settings.INSERT_BEFORE)
            bits = re.split(pattern, decoded, flags=re.IGNORECASE)

            if len(bits) > 1:
                bits[-2] += toolbar.render_toolbar()
                body = self.settings.INSERT_BEFORE.join(bits).encode(response.charset)
        else:
            data = json.loads(decoded, object_pairs_hook=OrderedDict)
            data["debugToolbar"] = toolbar.refresh()
            body = json.dumps(data).encode(response.charset)

        async def stream() -> t.AsyncGenerator[bytes, None]:
            yield body

        response.body_iterator = stream()  # type: ignore
        response.headers["Content-Length"] = str(len(body))
        return response

    def require_show_toolbar(
        self,
        f: t.Callable[..., t.Any],
    ) -> t.Callable[[t.Any], t.Any]:
        @functools.wraps(f)
        def decorator(request: Request, *args: t.Any, **kwargs: t.Any) -> t.Any:
            if not self.show_toolbar(request, self.settings):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            return f(request, *args, **kwargs)

        return decorator
