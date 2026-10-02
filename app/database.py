"""База макетов на странице «Настройки»: загрузка по ссылке, прогресс, проверка обновлений.

Каждая сохранённая ссылка загружается в локальную базу отдельно (кнопка в строке).
Загрузки идут по одной — очередью, чтобы не упираться в лимиты Figma API.
«Проверить обновления» лёгким запросом узнаёт версию каждого загруженного файла в Figma
и сравнивает с версией, которая лежит в базе.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import experiment, lifecycle, links
from .config import ConfigError, load_token, parse_file_key, parse_node_id
from .figma_client import API_BASE, FigmaError, _error_from_response

router = APIRouter(prefix="/api/db")
log = logging.getLogger("app.database")

_jobs: dict[str, dict] = {}          # ключ ссылки → состояние загрузки
_updates: dict[str, dict] = {}       # ключ ссылки → результат проверки обновлений
_queue_lock = asyncio.Lock()
_queue_seq = {"n": 0}
_tasks: dict[str, asyncio.Task] = {}  # ключ ссылки → задача загрузки (чтобы можно было остановить)


def link_key(url: str) -> str:
    return f"{parse_file_key(url)}|{parse_node_id(url) or ''}"


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "code": code, "message": message}, status_code=http_status)


def _last_loads() -> dict[str, dict]:
    """Последняя загрузка по каждому (файл, область) и версия в базе."""
    if not experiment.DB_PATH.exists():
        return {}
    con = experiment._connect()
    try:
        out: dict[str, dict] = {}
        for fk, sid, stats in con.execute(
                "SELECT file_key, scope_id, stats FROM loads WHERE id IN "
                "(SELECT MAX(id) FROM loads GROUP BY file_key, scope_id)"):
            st = json.loads(stats)
            out[f"{fk}|{sid}"] = {
                "loaded_at": st.get("loaded_at"), "version": st.get("version"),
                "nodes": st.get("nodes_stored"), "json_bytes": st.get("json_bytes"),
                "stored_bytes": st.get("stored_bytes"), "total_s": st.get("total_s"),
                "download_s": st.get("download_s"), "file_name": st.get("file_name"),
                "pages": (st.get("pages") or {}).get("loaded"),
                "skipped": (st.get("pages") or {}).get("skipped"),
                "scope_name": st.get("scope_name"), "node_id": st.get("node_id"),
                # макет загружен в текущем формате (цвета, шрифты, картинки, скругления…)
                "colors": (st.get("format") or 0) >= experiment.DATA_FORMAT,
            }
        return out
    finally:
        con.close()


def _job_view(job: dict) -> dict:
    now = time.time()
    view = {k: job[k] for k in ("state", "phase", "bytes", "expected_bytes", "pages_total", "pages_loaded", "message")}
    view["elapsed_s"] = round((job.get("finished") or now) - job["created"], 1)
    view["rows"] = job.get("rows")
    if job.get("phase_started"):
        view["phase_s"] = round(now - job["phase_started"], 1)
    dl_started = job.get("download_started")
    if dl_started and job["bytes"]:
        speed = job["bytes"] / max(now - dl_started, 0.001)
        view["speed_bps"] = round(speed)
        if job["expected_bytes"] and job["expected_bytes"] > job["bytes"] and job["phase"] == "pages":
            view["remaining_s"] = round((job["expected_bytes"] - job["bytes"]) / speed, 1)
    return view


async def _run_job(key: str, url: str) -> None:
    job = _jobs[key]
    lifecycle.request_started()  # пока идёт загрузка, сервер не останавливается
    try:
        async with _queue_lock:
            job.update(state="running", phase="start", started=time.time())

            def progress(d: dict) -> None:
                if "bytes" in d and not job.get("download_started"):
                    job["download_started"] = time.time()
                job.update(d)

            try:
                stats = await experiment.run_load(url, progress)
                job.update(state="done", phase="done", finished=time.time(),
                           message=f"Загружено за {stats['total_s']:.1f} с")
                _updates.pop(key, None)  # база свежая — старый результат проверки неактуален
            except experiment.LoadError as exc:
                job.update(state="error", finished=time.time(), message=exc.message)
            except asyncio.CancelledError:
                job.update(state="stopped", finished=time.time(), message="Загрузка остановлена")
                raise
            except Exception:
                log.exception("Ошибка загрузки %s", key)
                job.update(state="error", finished=time.time(), message="Не удалось загрузить макет. Подробности в журнале.")
    except asyncio.CancelledError:
        # остановили, пока макет ждал в очереди
        if job["state"] == "queued":
            job.update(state="stopped", finished=time.time(), message="Загрузка остановлена")
    finally:
        _tasks.pop(key, None)
        lifecycle.request_finished()


class LinkRequest(BaseModel):
    url: str


@router.post("/load")
async def load(req: LinkRequest) -> JSONResponse:
    try:
        key = link_key(req.url)
    except ConfigError as exc:
        return _error("bad_link", str(exc), 400)
    return JSONResponse({"status": "ok", "job": _job_view(_enqueue(key, req.url, _last_loads()))})


def _enqueue(key: str, url: str, loads: dict) -> dict:
    """Ставит ссылку в очередь загрузки (если она уже в очереди или грузится — ничего не делает)."""
    job = _jobs.get(key)
    if job and job["state"] in ("queued", "running"):
        return job
    _queue_seq["n"] += 1
    _jobs[key] = {"state": "queued", "phase": "queued", "bytes": 0,
                  "expected_bytes": (loads.get(key) or {}).get("json_bytes"),
                  "pages_total": None, "pages_loaded": None, "message": "", "created": time.time(),
                  "order": _queue_seq["n"]}
    _tasks[key] = asyncio.get_running_loop().create_task(_run_job(key, url))
    return _jobs[key]


@router.post("/stop")
async def stop() -> JSONResponse:
    """«Остановить»: снимает с очереди все макеты и прерывает текущую загрузку.
    В базе остаётся то, что было до этой загрузки (если запись уже идёт — она завершится целиком)."""
    stopped = 0
    for key, task in list(_tasks.items()):
        job = _jobs.get(key)
        if job and job["state"] in ("queued", "running") and not task.done():
            task.cancel()
            stopped += 1
    await asyncio.sleep(0)  # дать задачам обработать отмену
    return JSONResponse({"status": "ok", "stopped": stopped})


@router.post("/load-all")
async def load_all() -> JSONResponse:
    """«Загрузить все заново»: в очередь — только макеты, которые ещё не загружены или загружены
    в старом формате. Уже загруженные в текущем формате не трогаем.
    Пока идёт загрузка, поиск работает по тому, что уже лежит в базе."""
    loads = await asyncio.to_thread(_last_loads_safe)
    queued = skipped = 0
    for l in links.list_links():
        try:
            key = link_key(l["url"])
        except ConfigError:
            continue
        if (loads.get(key) or {}).get("colors"):
            skipped += 1
            continue
        _enqueue(key, l["url"], loads)
        queued += 1
    return JSONResponse({"status": "ok", "queued": queued, "skipped": skipped})


_loads_cache: dict = {"value": {}}


def _last_loads_safe() -> dict[str, dict]:
    """Как _last_loads, но если база на мгновение занята — прежний результат, а не ошибка."""
    try:
        _loads_cache["value"] = _last_loads()
    except sqlite3.Error as exc:
        log.info("База занята, статус из кэша: %s", exc)
    return _loads_cache["value"]


@router.get("/status")
async def status() -> JSONResponse:
    # в отдельном потоке: чтение базы не задерживает остальные запросы сервера
    loads = await asyncio.to_thread(_last_loads_safe)
    items = {}
    for l in links.list_links():
        key = link_key(l["url"])
        items[key] = {
            "url": l["url"],
            "loaded": loads.get(key),
            "job": _job_view(_jobs[key]) if key in _jobs else None,
            "update": _updates.get(key),
        }
    # Данные в базе, для которых ссылки в списке больше нет (например, удалённые до того,
    # как крестик стал чистить базу) — показываем, чтобы их можно было удалить.
    orphans = [
        {"key": key, "file_key": key.split("|")[0], "scope_id": key.split("|")[1], "name": d.get("file_name"),
         "scope_name": d.get("scope_name"), "loaded_at": d.get("loaded_at"), "nodes": d.get("nodes"),
         "stored_bytes": d.get("stored_bytes")}
        for key, d in loads.items() if key not in items
    ]
    return JSONResponse({"items": items, "orphans": orphans, "db_bytes": experiment._db_bytes()})


async def _remote_version(client: httpx.AsyncClient, token: str, file_key: str) -> dict:
    """Версия и дата изменения файла в Figma — лёгким запросом /meta (при ошибке — depth=1)."""
    headers = {"X-Figma-Token": token}
    resp = await client.get(f"{API_BASE}/files/{file_key}/meta", headers=headers)
    if resp.status_code == 200:
        f = resp.json().get("file") or {}
        if f.get("version"):
            return {"version": str(f["version"]), "modified": f.get("last_touched_at", "")}
    resp = await client.get(f"{API_BASE}/files/{file_key}", params={"depth": 1}, headers=headers)
    if resp.status_code != 200:
        raise _error_from_response(resp)
    d = resp.json()
    return {"version": str(d.get("version", "")), "modified": d.get("lastModified", "")}


@router.post("/check")
async def check() -> JSONResponse:
    """Для каждого загруженного макета: изменился ли он в Figma после загрузки."""
    try:
        token = load_token()
    except ConfigError as exc:
        return _error("config", str(exc), 400)
    loads = _last_loads()
    checked = 0
    lifecycle.request_started()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            by_file: dict[str, dict] = {}   # один запрос на файл, даже если ссылок на него несколько
            for l in links.list_links():
                key = link_key(l["url"])
                local = loads.get(key)
                if not local:
                    continue
                fk = l["file_key"]
                try:
                    if fk not in by_file:
                        by_file[fk] = await _remote_version(client, token, fk)
                    remote = by_file[fk]
                    _updates[key] = {
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "remote_version": remote["version"], "remote_modified": remote["modified"],
                        "available": bool(remote["version"]) and remote["version"] != (local.get("version") or ""),
                    }
                except (FigmaError, httpx.HTTPError) as exc:
                    msg = exc.message if isinstance(exc, FigmaError) else "ошибка сети"
                    _updates[key] = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": msg}
                checked += 1
    finally:
        lifecycle.request_finished()
    return JSONResponse({"status": "ok", "checked": checked})


class DeleteRequest(BaseModel):
    url: str


@router.post("/delete")
async def delete(req: DeleteRequest) -> JSONResponse:
    """Удаляет ссылку из настроек и всё загруженное по ней из локальной базы."""
    try:
        key = link_key(req.url)
    except ConfigError as exc:
        return _error("bad_link", str(exc), 400)
    job = _jobs.get(key)
    if job and job["state"] in ("queued", "running"):
        return _error("busy", "Идёт загрузка этого макета — дождитесь окончания и удалите снова.", 409)
    file_key, scope_id = key.split("|", 1)
    try:
        removed = await asyncio.to_thread(experiment.delete_scope, file_key, scope_id)
        items = links.remove_link(req.url)
    except (ConfigError, OSError) as exc:
        log.warning("Не удалось удалить %s: %s", key, exc.__class__.__name__)
        return _error("delete_failed", "Не удалось удалить макет.", 500)
    _jobs.pop(key, None)
    _updates.pop(key, None)
    return JSONResponse({"status": "ok", "links": items, "removed_nodes": removed["nodes"],
                         "db_bytes": experiment._db_bytes()})


class OrphanDeleteRequest(BaseModel):
    file_key: str
    scope_id: str = ""


@router.post("/delete-orphan")
async def delete_orphan(req: OrphanDeleteRequest) -> JSONResponse:
    """Удаляет из базы данные макета, ссылки на который в списке нет."""
    key = f"{req.file_key}|{req.scope_id}"
    if any(link_key(l["url"]) == key for l in links.list_links()):
        return _error("has_link", "Этот макет есть в списке — удалите его крестиком в строке.", 400)
    removed = await asyncio.to_thread(experiment.delete_scope, req.file_key, req.scope_id)
    _updates.pop(key, None)
    return JSONResponse({"status": "ok", "removed_nodes": removed["nodes"], "db_bytes": experiment._db_bytes()})
