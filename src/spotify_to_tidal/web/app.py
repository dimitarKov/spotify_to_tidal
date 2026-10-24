import hmac
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .settings import WebSettings

_HERE = Path(__file__).parent
SESSION_COOKIE = "stt_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # seconds


def _is_invited(request: Request) -> bool:
    return request.session.get("invited") is True


def create_app(settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Spotify to Tidal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie=SESSION_COOKIE,
                       max_age=SESSION_MAX_AGE, same_site="lax", https_only=settings.https_only)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        if not _is_invited(request):
            return RedirectResponse("/welcome", status_code=303)
        return templates.TemplateResponse(request, "home.html")

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request) -> Response:
        if _is_invited(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "welcome.html")

    @app.post("/welcome", response_class=HTMLResponse)
    def check_invite_code(request: Request, invite_code: Annotated[str, Form()] = "") -> Response:
        # constant-time comparison, so response times don't reveal how much of the code was right
        if hmac.compare_digest(invite_code.strip().encode(), settings.invite_code.encode()):
            request.session["invited"] = True
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "welcome.html", {"error": "That invite code isn't right."}, status_code=403)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/welcome", status_code=303)

    return app
