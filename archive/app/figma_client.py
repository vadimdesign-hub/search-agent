"""Загрузка структуры файла через Figma REST API (только чтение)."""

from __future__ import annotations

import asyncio
import logging

import httpx

API_BASE = "https://api.figma.com/v1"
TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0)
PAGES_TIMEOUT = 10.0  # название страницы — второстепенно, долго не ждём

log = logging.getLogger("app.figma")


class FigmaError(Exception):
    """Ошибка получения данных. `message` показывается пользователю как есть."""

    def __init__(self, code: str, message: str, http_status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _error_from_response(resp: httpx.Response) -> FigmaError:
    status = resp.status_code
    detail = ""
    try:
        body = resp.json()
        detail = str(body.get("err") or body.get("message") or "")
    except ValueError:
        pass

    if status == 400:
        return FigmaError("bad_request", "Figma отклонила запрос: проверьте ссылку на файл." + _suffix(detail), 400)
    if status in (401, 403):
        return FigmaError(
            "forbidden",
            "Нет доступа к файлу: токен неверный или истёк, либо у владельца токена нет прав "
            "на этот файл (нужен scope file_content:read).",
            403,
        )
    if status == 404:
        return FigmaError(
            "not_found_file",
            "Файл не найден: проверьте ссылку. Если файл существует, убедитесь, что у владельца токена есть к нему доступ.",
            404,
        )
    if status == 429:
        retry = resp.headers.get("Retry-After")
        wait = f" Повторите через {retry} с." if retry and retry.isdigit() else " Подождите немного и повторите."
        return FigmaError("rate_limited", "Превышен лимит запросов к Figma API." + wait, 429)
    if status >= 500:
        return FigmaError("figma_unavailable", f"Сервер Figma временно недоступен (HTTP {status}). Повторите позже.", 502)
    return FigmaError("figma_error", f"Неожиданный ответ Figma (HTTP {status})." + _suffix(detail), 502)


def _suffix(detail: str) -> str:
    return f" Ответ Figma: {detail}" if detail else ""


async def _get_json(path: str, token: str, params: dict | None,
                    transport: httpx.AsyncBaseTransport | None) -> dict:
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            resp = await client.get(url, params=params, headers={"X-Figma-Token": token})
    except httpx.TimeoutException:
        raise FigmaError("timeout", "Figma не ответила вовремя. Проверьте сеть и повторите поиск.", 504)
    except httpx.RequestError as exc:
        log.warning("Сетевая ошибка при обращении к Figma: %s", exc.__class__.__name__)
        raise FigmaError("network", "Ошибка сети: не удалось подключиться к Figma. Проверьте интернет-соединение.", 502)

    if resp.status_code != 200:
        log.warning("Figma вернула HTTP %s для %s", resp.status_code, path)
        raise _error_from_response(resp)
    try:
        data = resp.json()
    except ValueError:
        raise FigmaError("incomplete", "Получен повреждённый ответ от Figma. Поиск не выполнен — повторите попытку.", 502)
    if not isinstance(data, dict):
        raise FigmaError("incomplete", "Получен неполный ответ от Figma. Поиск не выполнен.", 502)
    return data


def _check_document(doc) -> None:
    if not isinstance(doc, dict) or not isinstance(doc.get("children", []), list):
        raise FigmaError("incomplete", "Получен неполный ответ от Figma (нет структуры документа). Поиск не выполнен.", 502)


async def fetch_file(file_key: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """GET /v1/files/:key — полное дерево документа плюс словари components / componentSets.

    Для больших файлов это очень тяжёлый ответ; используется только при поиске по всему файлу.
    """
    log.info("Загрузка всего файла %s из Figma API", file_key)
    data = await _get_json(f"/files/{file_key}", token, None, transport)
    doc = data.get("document")
    if not isinstance(doc, dict) or not isinstance(doc.get("children"), list):
        _check_document(None)
    return data


async def fetch_node(file_key: str, token: str, node_id: str,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """GET /v1/files/:key/nodes?ids=… — только поддерево выбранного узла (секции, фрейма, страницы).

    Возвращает данные в том же виде, что fetch_file: документ с одной страницей,
    внутри которой лежит выбранный узел, плюс components / componentSets этого поддерева.
    Название страницы узнаём отдельным лёгким запросом (depth=2); если он не удался,
    страница показывается как «—», на поиск это не влияет.
    """
    log.info("Загрузка узла %s файла %s из Figma API", node_id, file_key)
    nodes_task = asyncio.create_task(_get_json(f"/files/{file_key}/nodes", token, {"ids": node_id}, transport))
    pages_task = asyncio.create_task(_get_json(f"/files/{file_key}", token, {"depth": 2}, transport))
    try:
        data = await nodes_task
    except BaseException:
        pages_task.cancel()
        raise
    try:
        pages = (await asyncio.wait_for(pages_task, PAGES_TIMEOUT)).get("document", {}).get("children", []) or []
    except (FigmaError, asyncio.TimeoutError):
        pages = []

    entry = (data.get("nodes") or {}).get(node_id)
    if entry is None:
        raise FigmaError(
            "node_not_found",
            f"Элемент из ссылки (node-id {node_id}) не найден в файле: возможно, он удалён. "
            "Обновите ссылку в настройках.",
            404,
        )
    node = entry.get("document")
    _check_document(node)

    if node.get("type") == "CANVAS":
        page = node
    else:
        page_name = next(
            (p.get("name", "") for p in pages if any(c.get("id") == node_id for c in p.get("children", []) or [])),
            "—",
        )
        page = {"id": "", "type": "CANVAS", "name": page_name, "children": [node]}

    return {
        "name": data.get("name", ""),
        "lastModified": data.get("lastModified", ""),
        "version": data.get("version", ""),
        "document": {"id": "0:0", "type": "DOCUMENT", "name": "Document", "children": [page]},
        "components": entry.get("components") or {},
        "componentSets": entry.get("componentSets") or {},
        "scope": {"node_id": node_id, "name": node.get("name", ""), "type": node.get("type", ""),
                  "page": page.get("name", "")},
    }
