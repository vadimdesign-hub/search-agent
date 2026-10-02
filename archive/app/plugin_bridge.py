"""Вариант 2: поиск без скачивания файла — его выполняет плагин внутри Figma.

Страница /plugin отправляет сюда запрос, сервер ставит задачу в очередь, плагин
«Search Agent — поиск без скачивания» (figma-plugin-search/) забирает её опросом,
обходит дерево прямо в Figma через Plugin API и присылает готовый результат.
Файл через REST API не загружается, токен Figma не нужен.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import ConfigError, parse_file_key, parse_node_id
from .search import node_url

router = APIRouter(prefix="/api/p2")

PLUGIN_ALIVE_SECONDS = 5
TASK_TTL_SECONDS = 30       # задачу, которую плагин не забрал за это время, не выполняем
SEARCH_TIMEOUT_SECONDS = 180
SELECT_TIMEOUT_SECONDS = 10

_state: dict = {"seq": 0, "plugin_seen": 0.0}
_tasks: dict[int, dict] = {}
_waiters: dict[int, asyncio.Future] = {}


def plugin_connected() -> bool:
    return time.time() - _state["plugin_seen"] < PLUGIN_ALIVE_SECONDS


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "code": code, "message": message}, status_code=http_status)


async def _run_task(kind: str, payload: dict, timeout: float) -> dict:
    """Ставит задачу плагину и ждёт его ответ."""
    _state["seq"] += 1
    seq = _state["seq"]
    now = time.time()
    for old in [s for s, t in _tasks.items() if now - t["created"] > TASK_TTL_SECONDS]:
        _tasks.pop(old, None)
    _tasks[seq] = {"seq": seq, "type": kind, "payload": payload, "created": now}
    fut = asyncio.get_running_loop().create_future()
    _waiters[seq] = fut
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        _waiters.pop(seq, None)
        _tasks.pop(seq, None)


# --- Опрос и ответы плагина ---

@router.get("/poll")
async def poll(after: int = -1) -> JSONResponse:
    _state["plugin_seen"] = time.time()
    tasks = [
        {"seq": t["seq"], "type": t["type"], "payload": t["payload"]}
        for t in sorted(_tasks.values(), key=lambda t: t["seq"])
        if after >= 0 and t["seq"] > after
    ]
    from . import lifecycle
    return JSONResponse({"seq": _state["seq"], "tasks": tasks, "page_open": lifecycle.pages_open() > 0})


class PluginResult(BaseModel):
    seq: int
    ok: bool
    data: dict | None = None
    message: str = ""


@router.post("/result")
async def result(res: PluginResult) -> JSONResponse:
    _state["plugin_seen"] = time.time()
    fut = _waiters.get(res.seq)
    if fut and not fut.done():
        fut.set_result(res.model_dump())
    return JSONResponse({"ok": True})


@router.get("/status")
async def status() -> JSONResponse:
    return JSONResponse({"plugin_connected": plugin_connected()})


# --- Запросы страницы ---

PLUGIN_OFFLINE = (
    "Плагин «Search Agent — поиск без скачивания» не запущен. "
    "Откройте файл в Figma и запустите плагин: Plugins → Development."
)


class SearchRequest(BaseModel):
    name: str
    file_url: str
    candidate_id: str | None = None
    exclude_hidden: bool = False


def _with_urls(file_key: str, data: dict) -> dict:
    """Плагин присылает только ID — ссылки на Figma строим здесь, как в варианте 1."""
    for inst in data.get("instances") or []:
        inst["url"] = node_url(file_key, inst["id"])
        if inst.get("parent_instance"):
            inst["parent_instance"]["url"] = node_url(file_key, inst["parent_instance"]["id"])
    for cand in (data.get("candidates") or []) + ([data["component"]] if data.get("component") else []):
        cand["url"] = node_url(file_key, cand["node_id"]) if cand.get("node_id") else None
    return data


@router.post("/search")
async def search(req: SearchRequest) -> JSONResponse:
    from .main import _scope_label  # общий формат подписи области с вариантом 1

    name = req.name.strip()
    if not name:
        return _error("empty_name", "Введите название мастер-компонента.", 400)
    try:
        file_key = parse_file_key(req.file_url)
        node_id = parse_node_id(req.file_url)
    except ConfigError as exc:
        return _error("config", str(exc), 400)
    if not plugin_connected():
        return _error("plugin_offline", PLUGIN_OFFLINE, 409)

    started = time.perf_counter()
    try:
        res = await _run_task("search", {
            "fileKey": file_key, "nodeId": node_id, "name": name,
            "candidateId": req.candidate_id, "excludeHidden": req.exclude_hidden,
        }, SEARCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _error("plugin_timeout", "Плагин не ответил за 3 минуты. Поиск не выполнен — проверьте, что плагин запущен.", 504)
    total = time.perf_counter() - started

    data = res.get("data") or {}
    if not res["ok"]:
        return _error(data.get("code") or "plugin_error", res.get("message") or "Плагин не смог выполнить поиск.", 400)

    t = data.pop("timing", {}) or {}
    load_s, search_s = t.get("load_ms", 0) / 1000, t.get("search_ms", 0) / 1000
    scope = data.pop("scope", None)
    data["file"] = {
        "name": data.pop("file_name", ""),
        "key": file_key,
        "scope": scope,
        "scope_label": _scope_label(scope),
        "fetched_at": datetime.now().strftime("%H:%M:%S"),
    }
    data["timing"] = {
        "mode": "plugin",
        "load_s": round(load_s, 3),
        "search_s": round(search_s, 3),
        "bridge_s": round(max(total - load_s - search_s, 0), 3),
        "from_cache": False,
        "instances_scanned": t.get("instances_scanned", 0),
    }
    return JSONResponse(_with_urls(file_key, data))


class SelectRequest(BaseModel):
    node_ids: list[str]


@router.post("/select")
async def select(req: SelectRequest) -> JSONResponse:
    if not req.node_ids:
        return _error("empty", "Нечего выделять.", 400)
    if not plugin_connected():
        return _error("plugin_offline", PLUGIN_OFFLINE, 409)
    try:
        res = await _run_task("select", {"nodeIds": req.node_ids[:2000]}, SELECT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _error("plugin_timeout", "Плагин не ответил. Проверьте, что он запущен в Figma.", 504)
    return JSONResponse({"ok": res["ok"], "message": res.get("message", "")})
