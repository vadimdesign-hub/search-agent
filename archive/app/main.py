"""Локальный веб-сервер: страница поиска и API /api/search."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import figma_client, lifecycle, links
from .config import ConfigError, Settings, env_overrides, load_settings, load_token, read_config_file, save_settings
from .figma_client import FigmaError
from .search import (
    FileIndex,
    build_index,
    candidate_to_dict,
    find_candidates,
    find_instances,
    instance_to_dict,
    unresolved_instances_named,
)

STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL_SECONDS = 300

log = logging.getLogger("app")
app = FastAPI(title="Поиск инстансов компонента в Figma", docs_url=None, redoc_url=None)
# index.html, открытый двойным кликом (file://), шлёт запросы с Origin: null.
# Другим сайтам доступ к локальному API не открываем.
app.add_middleware(CORSMiddleware, allow_origins=["null"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


# Незавершённые запросы (например, долгий поиск) не дают серверу остановиться.
@app.middleware("http")
async def _track_inflight(request, call_next):
    lifecycle.request_started()
    try:
        return await call_next(request)
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

# Кэш последней загрузки: повторный запрос (например, выбор одного из
# одноимённых компонентов) не тратит лимит Figma API.
_cache: dict[str, tuple[float, FileIndex]] = {}
_cache_lock = asyncio.Lock()


class SearchRequest(BaseModel):
    name: str
    file_url: str | None = None  # ссылка на файл / страницу / секцию / фрейм; пусто — последняя сохранённая
    candidate_id: str | None = None
    refresh: bool = False
    exclude_hidden: bool = False  # не учитывать скрытые инстансы (сам слой или родитель скрыт)


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "code": code, "message": message}, status_code=http_status)


async def _get_index(settings: Settings, node_id: str | None, refresh: bool) -> tuple[FileIndex, float, bool]:
    """Возвращает (индекс, время загрузки, взято ли из кэша)."""
    cache_key = f"{settings.file_key}|{node_id or '*'}"
    async with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and not refresh and time.time() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[0], True
        if node_id:
            data = await figma_client.fetch_node(settings.file_key, settings.token, node_id)
        else:
            data = await figma_client.fetch_file(settings.file_key, settings.token)
        index = build_index(settings.file_key, data)
        fetched_at = time.time()
        _cache[cache_key] = (fetched_at, index)
        return index, fetched_at, False


def _file_meta(index: FileIndex, fetched_at: float) -> dict:
    return {
        "name": index.file_name,
        "last_modified": index.last_modified,
        "fetched_at": datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S"),
        "scope": index.scope,
        "scope_label": _scope_label(index.scope),
        "key": index.file_key,
    }


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/experiment")
async def experiment_page() -> FileResponse:
    """Эксперимент: компактная локальная база структуры макета."""
    return FileResponse(STATIC_DIR / "experiment.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    """Настройки: список ссылок на макеты Figma."""
    return FileResponse(STATIC_DIR / "settings.html")


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


@app.get("/plugin")
async def plugin_page() -> FileResponse:
    """Вариант 2: поиск без скачивания, через плагин в Figma."""
    return FileResponse(STATIC_DIR / "plugin.html")


class SettingsRequest(BaseModel):
    token: str | None = None


SCOPE_LABELS = {"CANVAS": "страница", "SECTION": "секция", "FRAME": "фрейм", "GROUP": "группа",
                "COMPONENT": "компонент", "COMPONENT_SET": "набор вариантов", "INSTANCE": "инстанс"}


def _scope_label(scope: dict | None) -> str:
    if not scope:
        return "весь файл"
    kind = SCOPE_LABELS.get(scope.get("type", ""), "элемент")
    label = f"{kind} «{scope.get('name', '')}»"
    page = scope.get("page")
    if scope.get("type") != "CANVAS" and page and page != "—":
        label += f" на странице «{page}»"
    return label


def _settings_state() -> dict:
    """Текущее состояние настроек для интерфейса. Токен наружу не отдаётся — только факт его наличия."""
    stored = read_config_file()
    state = {
        "file_url": os.environ.get("FIGMA_FILE_URL") or stored.get("figma_file_url", ""),
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
    _cache.clear()
    return JSONResponse(_settings_state())


@app.post("/api/search")
async def search(req: SearchRequest) -> JSONResponse:
    name = req.name.strip()
    if not name:
        return _error("empty_name", "Введите название мастер-компонента.", 400)
    try:
        settings = load_settings(req.file_url)
        if req.file_url:
            save_settings(file_url=req.file_url)  # запоминаем последнюю ссылку
    except ConfigError as exc:
        return _error("config", str(exc), 400)
    except OSError:
        pass  # не смогли запомнить ссылку — на поиск не влияет

    # Выбор кандидата — продолжение того же поиска, данные берём из кэша.
    try:
        t_load = time.perf_counter()
        index, fetched_at, from_cache = await _get_index(settings, settings.node_id, refresh=req.refresh and not req.candidate_id)
        t_search = time.perf_counter()
    except FigmaError as exc:
        return _error(exc.code, exc.message, exc.http_status)
    except Exception:  # поиск не завершён — не выдаём частичный результат
        log.exception("Ошибка обработки файла Figma")
        return _error("internal", "Не удалось обработать структуру файла. Поиск не выполнен.", 500)

    meta = _file_meta(index, fetched_at)
    load_s = t_search - t_load

    def respond(payload: dict) -> JSONResponse:
        # Время на сервере: загрузка из Figma (или кэш) и сам поиск по дереву.
        payload["timing"] = {
            "load_s": round(load_s, 3),
            "search_s": round(time.perf_counter() - t_search, 3),
            "from_cache": from_cache,
            "instances_scanned": len(index.instances),
        }
        return JSONResponse(payload)

    candidates = find_candidates(index, name)

    if not candidates:
        unresolved = unresolved_instances_named(index, name)
        if unresolved:
            return respond({
                "status": "unresolved",
                "file": meta,
                "message": (
                    f"Найдено слоёв-инстансов с именем «{name}»: {len(unresolved)}, но их мастер-компонент "
                    "не удалось определить из ответа Figma API (возможно, библиотека удалена или недоступна "
                    "владельцу токена). Надёжно связать их с компонентом нельзя, поэтому они не показаны."
                ),
            })
        if index.scope:
            message = (
                f"Компонент «{name}» не найден: в области поиска ({_scope_label(index.scope)}) нет ни его "
                "мастер-компонента, ни его инстансов. Проверьте название или дайте ссылку на весь файл."
            )
        else:
            message = f"Компонент «{name}» не найден в файле и в подключённых к нему библиотечных компонентах."
        return respond({"status": "not_found", "file": meta, "message": message})

    if req.candidate_id:
        chosen = next((c for c in candidates if c.cid == req.candidate_id), None)
        if chosen is None:
            return _error("stale_choice", "Выбранный компонент больше не найден — запустите поиск заново.", 409)
    elif len(candidates) > 1:
        return respond({
            "status": "ambiguous",
            "file": meta,
            "message": f"Найдено несколько разных компонентов с названием «{name}». Выберите нужный.",
            "candidates": [candidate_to_dict(index, c) for c in candidates],
        })
    else:
        chosen = candidates[0]

    instances = find_instances(index, chosen)
    hidden_skipped = 0
    if req.exclude_hidden:
        visible = [i for i in instances if not i.hidden]
        hidden_skipped = len(instances) - len(visible)
        instances = visible
    return respond({
        "status": "ok",
        "file": meta,
        "component": candidate_to_dict(index, chosen),
        "count": len(instances),
        "hidden_skipped": hidden_skipped,
        "instances": [instance_to_dict(index, chosen, i) for i in instances],
    })




# --- Мост к плагину Figma: выделение найденных объектов ---
# Ссылка с node-id не всегда выделяет вложенный слой (Figma может открыть родительский
# фрейм), поэтому выделение делает локальный плагин (figma-plugin/). Страница кладёт
# сюда запрос, плагин опрашивает сервер, выделяет узлы и присылает результат.

PLUGIN_ALIVE_SECONDS = 5
_bridge: dict = {"seq": 0, "request": None, "result": None, "plugin_seen": 0.0}


def _plugin_connected() -> bool:
    return time.time() - _bridge["plugin_seen"] < PLUGIN_ALIVE_SECONDS


class SelectRequest(BaseModel):
    file_key: str
    node_ids: list[str]


class SelectResult(BaseModel):
    seq: int
    ok: bool
    message: str = ""


@app.post("/api/figma/select")
async def figma_select(req: SelectRequest) -> JSONResponse:
    if not req.node_ids:
        return _error("empty", "Нечего выделять.", 400)
    _bridge["seq"] += 1
    _bridge["request"] = {"file_key": req.file_key, "node_ids": req.node_ids[:2000]}
    _bridge["result"] = None
    return JSONResponse({"seq": _bridge["seq"], "plugin_connected": _plugin_connected()})


@app.get("/api/figma/poll")
async def figma_poll() -> JSONResponse:
    """Опрос от плагина: отмечает, что плагин запущен, и отдаёт последний запрос."""
    _bridge["plugin_seen"] = time.time()
    return JSONResponse({"seq": _bridge["seq"], "request": _bridge["request"], "page_open": lifecycle.pages_open() > 0})


@app.post("/api/figma/result")
async def figma_result(res: SelectResult) -> JSONResponse:
    _bridge["plugin_seen"] = time.time()
    if res.seq == _bridge["seq"]:
        _bridge["result"] = res.model_dump()
    return JSONResponse({"ok": True})


@app.get("/api/figma/status")
async def figma_status() -> JSONResponse:
    return JSONResponse({"plugin_connected": _plugin_connected(), "seq": _bridge["seq"], "result": _bridge["result"]})


# Вариант 2 — поиск через плагин без скачивания файла (страница /plugin).
from .plugin_bridge import router as plugin_router  # noqa: E402

app.include_router(plugin_router)

# Эксперимент — локальная база структуры макета (страница /experiment).
from .experiment import router as experiment_router  # noqa: E402

app.include_router(experiment_router)

# База макетов на странице «Настройки»: загрузка по ссылке и проверка обновлений.
from .database import router as database_router  # noqa: E402

app.include_router(database_router)
