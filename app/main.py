"""Локальный веб-сервер: поиск по локальной базе макетов и настройки."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import lifecycle, links
from .config import ConfigError, env_overrides, load_token, read_config_file, save_settings

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("app")
app = FastAPI(title="Search Agent — поиск по макетам Figma", docs_url=None, redoc_url=None)
# index.html, открытый двойным кликом (file://), шлёт запросы с Origin: null.
# Другим сайтам доступ к локальному API не открываем.
app.add_middleware(CORSMiddleware, allow_origins=["null"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


# Незавершённые запросы (например, долгая загрузка) не дают серверу остановиться.
@app.middleware("http")
async def _track_inflight(request, call_next):
    lifecycle.request_started()
    try:
        response = await call_next(request)
        # Страницы и данные всегда свежие: без этого браузер может показать старую копию
        # страницы из кэша (например, прежнюю главную со старыми вкладками).
        response.headers["Cache-Control"] = "no-cache"
        return response
    finally:
        lifecycle.request_finished()


class PageSignal(BaseModel):
    page: str


@app.post("/api/heartbeat")
async def heartbeat(sig: PageSignal) -> JSONResponse:
    """Страница открыта — сервер не останавливается."""
    lifecycle.page_alive(sig.page)
    return JSONResponse({"ok": True, **lifecycle.info()})


@app.post("/api/bye")
async def bye(request: Request) -> JSONResponse:
    """Страницу закрыли (navigator.sendBeacon при закрытии вкладки)."""
    try:
        page = json.loads(await request.body() or b"{}").get("page", "")
    except ValueError:
        page = ""
    lifecycle.page_closed(page)
    return JSONResponse({"ok": True})


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "code": code, "message": message}, status_code=http_status)


@app.get("/")
async def page() -> FileResponse:
    """Главная: поиск по локальной базе макетов."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/experiment")
async def experiment_page() -> RedirectResponse:
    """Старый адрес вкладки «Эксперимент» — теперь это главная страница «Поиск»."""
    return RedirectResponse("/")


@app.get("/settings")
async def settings_page() -> FileResponse:
    """Настройки: список ссылок на макеты Figma."""
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/faq")
async def faq_page() -> FileResponse:
    """FAQ: какие данные есть в базе и как по ним искать."""
    return FileResponse(STATIC_DIR / "faq.html")


class LinkRequest(BaseModel):
    url: str


@app.get("/api/links")
async def get_links() -> JSONResponse:
    return JSONResponse({"links": links.list_links()})


@app.post("/api/links")
async def post_link(req: LinkRequest) -> JSONResponse:
    try:
        items, added = links.add_link(req.url)
    except ConfigError as exc:
        return _error("bad_link", str(exc), 400)
    except OSError:
        return _error("save_failed", "Не удалось сохранить ссылку в config.json.", 500)
    return JSONResponse({"links": items, "added": added})


class RulesRequest(BaseModel):
    rules: list[dict | str]  # {"text": "Flow", "exact": true} или просто "Stage"


@app.get("/api/rules")
async def get_rules() -> JSONResponse:
    return JSONResponse({"rules": links.page_rules()})


@app.post("/api/rules")
async def post_rules(req: RulesRequest) -> JSONResponse:
    try:
        return JSONResponse({"rules": links.save_page_rules(req.rules)})
    except OSError:
        return _error("save_failed", "Не удалось сохранить правила в config.json.", 500)


@app.post("/api/links/delete")
async def delete_link(req: LinkRequest) -> JSONResponse:
    try:
        items = links.remove_link(req.url)
    except (ConfigError, OSError):
        return _error("delete_failed", "Не удалось удалить ссылку.", 400)
    return JSONResponse({"links": items})


class SettingsRequest(BaseModel):
    token: str | None = None


def _settings_state() -> dict:
    """Текущее состояние настроек для интерфейса. Токен наружу не отдаётся — только факт его наличия."""
    stored = read_config_file()
    state = {
        "has_token": bool(stored.get("figma_token")),
        "env_overrides": env_overrides(),
    }
    try:
        load_token()
    except ConfigError as exc:
        return {**state, "configured": False, "message": str(exc)}
    return {**state, "configured": True}


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(_settings_state())


@app.post("/api/settings")
async def update_settings(req: SettingsRequest) -> JSONResponse:
    try:
        save_settings(token=req.token)
    except ConfigError as exc:
        return _error("config", str(exc), 400)
    except OSError:
        return _error("config", "Не удалось сохранить настройки в config.json.", 500)
    return JSONResponse(_settings_state())


# Локальная база: загрузка структуры макетов и поиск по ней (главная страница).
from .experiment import router as experiment_router  # noqa: E402

app.include_router(experiment_router)

# База макетов на странице «Настройки»: загрузка по ссылке и проверка обновлений.
from .database import router as database_router  # noqa: E402

app.include_router(database_router)
