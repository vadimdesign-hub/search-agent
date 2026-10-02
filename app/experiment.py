"""Эксперимент: компактная локальная база структуры макета.

Загружает файл (или страницу/секцию из ссылки) через Figma REST API, оставляет только
структуру — тип, название, видимость, родителя, связь инстанса с компонентом и текст —
и сохраняет в SQLite (db/figma.sqlite). Замеряет время и размер на каждом шаге,
чтобы понять, сколько займёт база по всем макетам.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import links
from .config import PROJECT_ROOT, ConfigError, load_token, parse_file_key, parse_node_id
from .figma_client import API_BASE, TIMEOUT, FigmaError, _error_from_response

router = APIRouter(prefix="/api/exp")
log = logging.getLogger("app.experiment")

DB_DIR = PROJECT_ROOT / "db"
DB_PATH = DB_DIR / "figma.sqlite"

# Простые фигуры для поиска бесполезны, а в больших макетах их большинство — не храним
# ни их, ни их содержимое. Всё остальное (фреймы, секции, группы, компоненты, инстансы,
# тексты) остаётся: из него строится путь и по нему ищем.
SKIP_TYPES = {
    "VECTOR", "RECTANGLE", "ELLIPSE", "LINE", "STAR", "POLYGON", "BOOLEAN_OPERATION",
    "REGULAR_POLYGON", "SLICE",
}
# Названия, которые Figma даёт фигурам сама: «Vector», «Rectangle 12», «Ellipse 3», «Union»…
DEFAULT_SHAPE_NAME = re.compile(
    r"^(vector|rectangle|ellipse|line|star|polygon|arrow|union|subtract|intersect|exclude|slice|path|image)"
    r"(\s+\d+)?$", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_key TEXT, scope_id TEXT, name TEXT, scope_name TEXT, version TEXT, last_modified TEXT,
    loaded_at TEXT, colors INTEGER, PRIMARY KEY (file_key, scope_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS nodes (
    file_key TEXT, scope_id TEXT, id TEXT, parent_id TEXT, type TEXT, name TEXT,
    visible INTEGER, component_id TEXT, text TEXT,
    x INTEGER, y INTEGER, w INTEGER, h INTEGER,  -- рамка на холсте, в десятых долях пикселя
    image INTEGER,                              -- 1: слой-картинка (заливка изображением)
    page_id TEXT,                               -- страница (CANVAS), на которой лежит объект
    hid INTEGER,                                -- 1: скрыт сам или кто-то из родителей
    pinst TEXT,                                 -- ближайший родитель-инстанс
    fill TEXT, fill_a INTEGER, fill_src TEXT,   -- цвет заливки (у текста — цвет текста): RRGGBB,
    stroke TEXT, stroke_a INTEGER, stroke_src TEXT,  -- непрозрачность в % (если не 100) и источник
    -- Дополнительно (номер значения в словаре vals — повторяющиеся значения хранятся один раз):
    font INTEGER,      -- шрифт текста: «семейство;начертание;вес;кегль;интерлиньяж;трекинг»
    tstyle INTEGER,    -- текстовый стиль
    img INTEGER,       -- какая картинка (imageRef)
    radius INTEGER,    -- скругление: «12» или «12,12,0,0»
    effect INTEGER,    -- тени и размытия: «s:Стиль» или описание вручную
    layout INTEGER,    -- auto layout: «H;gap;отступы;wrap;выравнивание»
    opacity INTEGER,   -- прозрачность слоя в % (если не 100)
    dev TEXT,          -- статус Dev Mode: R — Ready for dev, C — Completed
    ovr INTEGER,       -- 1: инстанс изменён относительно мастера (есть overrides)
    link TEXT,         -- куда ведёт клик в прототипе (id экранов через запятую)
    PRIMARY KEY (file_key, scope_id, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS nodes_name ON nodes (name);
CREATE INDEX IF NOT EXISTS nodes_component ON nodes (component_id);
CREATE TABLE IF NOT EXISTS components (
    file_key TEXT, scope_id TEXT, id TEXT, key TEXT, name TEXT, set_id TEXT, set_name TEXT, remote INTEGER,
    PRIMARY KEY (file_key, scope_id, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS components_key ON components (key);
CREATE TABLE IF NOT EXISTS vals (id INTEGER PRIMARY KEY, v TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS loads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_key TEXT, scope_id TEXT, name TEXT, scope_name TEXT,
    loaded_at TEXT, stats TEXT
);
"""


DATA_FORMAT = 2  # формат данных при загрузке: см. колонку files.colors
EXTRA_DICT_COLS = ("font", "tstyle", "img", "radius", "effect", "layout")
EXTRA_COLS = (*EXTRA_DICT_COLS, "opacity", "dev", "ovr", "link")


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    # WAL: чтение (поиск, статус в «Настройках») не ждёт, пока идёт запись нового макета.
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # База, созданная до появления размеров и координат: добавляем столбцы
    # (у старых записей они пустые, пока макет не перезагрузят).
    cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
    for col, kind in (("x", "INTEGER"), ("y", "INTEGER"), ("w", "INTEGER"), ("h", "INTEGER"), ("image", "INTEGER"),
                      ("page_id", "TEXT"), ("hid", "INTEGER"), ("pinst", "TEXT"),
                      ("fill", "TEXT"), ("fill_a", "INTEGER"), ("fill_src", "TEXT"),
                      ("stroke", "TEXT"), ("stroke_a", "INTEGER"), ("stroke_src", "TEXT"),
                      *((c, "INTEGER") for c in EXTRA_DICT_COLS), ("opacity", "INTEGER"), ("dev", "TEXT"),
                      ("ovr", "INTEGER"), ("link", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE nodes ADD COLUMN {col} {kind}")
    # colors — формат данных макета: 1 — с цветами, 2 — ещё и шрифты, картинки, скругления и т. д.
    # colors = 1: макет загружен уже с цветами (у загруженных раньше цветов нет, пока не перезагрузят)
    if "colors" not in {r[1] for r in con.execute("PRAGMA table_info(files)")}:
        con.execute("ALTER TABLE files ADD COLUMN colors INTEGER")
    return con


def _tenths(v) -> int | None:
    """Пиксели → целые десятые доли: 251.4375 → 2514. Так число занимает 1–3 байта вместо 8."""
    return round(v * 10) if isinstance(v, (int, float)) else None


def _box(node: dict) -> tuple:
    b = node.get("absoluteBoundingBox") or {}
    return _tenths(b.get("x")), _tenths(b.get("y")), _tenths(b.get("width")), _tenths(b.get("height"))


def box_px(x, y, w, h) -> dict | None:
    """Десятые доли из базы → пиксели для ответа."""
    if w is None or h is None:
        return None
    return {"x": x / 10 if x is not None else None, "y": y / 10 if y is not None else None, "w": w / 10, "h": h / 10}


def _db_display() -> str:
    try:
        return str(DB_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(DB_PATH)


def _db_bytes() -> int:
    return DB_PATH.stat().st_size if DB_PATH.exists() else 0


def _has_image(node: dict) -> bool:
    """Слой-картинка: в заливках есть изображение (так в Figma устроены «картинки»)."""
    return any(f.get("type") == "IMAGE" and f.get("visible", True) is not False for f in node.get("fills") or [])


def _hex(c: dict) -> str:
    return "".join(f"{max(0, min(255, round((c.get(k) or 0) * 255))):02X}" for k in ("r", "g", "b"))


def _paint(node: dict, field: str, style_key: str, styles: dict) -> tuple:
    """Верхняя видимая сплошная заливка/обводка → (RRGGBB, непрозрачность % или None, источник).

    Источник: «s:Название стиля», «v:» — цвет из переменной, None — задан вручную."""
    paints = node.get(field) or []
    for i in range(len(paints) - 1, -1, -1):  # в Figma последняя заливка в списке — верхняя
        p = paints[i]
        if p.get("type") != "SOLID" or p.get("visible", True) is False:
            continue
        c = p.get("color") or {}
        alpha = round((c.get("a", 1) if c.get("a") is not None else 1) * (p.get("opacity", 1) if p.get("opacity") is not None else 1) * 100)
        style_id = (node.get("styles") or {}).get(style_key)
        if style_id:
            src = "s:" + ((styles.get(style_id) or {}).get("name") or "без названия")
        elif (p.get("boundVariables") or {}).get("color") or (node.get("boundVariables") or {}).get(field):
            src = "v:"
        else:
            src = None
        return _hex(c), (alpha if alpha < 100 else None), src
    return None, None, None


def _num(v) -> str:
    """12.0 → «12», 12.345 → «12.35»."""
    return f"{round(v, 2):g}" if isinstance(v, (int, float)) else ""


def _extras(node: dict, ntype: str, styles: dict) -> tuple:
    """Шрифт, стиль текста, картинка, скругление, эффекты, auto layout, прозрачность,
    Dev-статус, изменён ли инстанс, ссылки прототипа — в порядке EXTRA_COLS (None — нет значения)."""
    st = node.get("styles") or {}
    style_name = lambda key: ((styles.get(st[key]) or {}).get("name") or "без названия") if st.get(key) else None

    font = tstyle = None
    if ntype == "TEXT":
        t = node.get("style") or {}
        if t:
            font = ";".join([t.get("fontFamily") or "", t.get("fontStyle") or ("Italic" if t.get("italic") else ""),
                             _num(t.get("fontWeight")), _num(t.get("fontSize")), _num(t.get("lineHeightPx")),
                             _num(t.get("letterSpacing"))])
        tstyle = style_name("text")

    img = next((f.get("imageRef") for f in node.get("fills") or []
                if f.get("type") == "IMAGE" and f.get("visible", True) is not False and f.get("imageRef")), None)

    radius = None
    radii = node.get("rectangleCornerRadii")
    if isinstance(radii, list) and any(radii):
        radius = _num(radii[0]) if len(set(radii)) == 1 else ",".join(_num(r) for r in radii)
    elif node.get("cornerRadius"):
        radius = _num(node["cornerRadius"])

    effect = None
    effects = [e for e in node.get("effects") or [] if e.get("visible", True) is not False]
    if effects:
        effect = ("s:" + style_name("effect")) if st.get("effect") else "|".join(
            ";".join([e.get("type", ""), _num((e.get("offset") or {}).get("x")), _num((e.get("offset") or {}).get("y")),
                      _num(e.get("radius")), _num(e.get("spread")),
                      _hex(e["color"]) + f":{round((e['color'].get('a', 1)) * 100)}" if e.get("color") else ""])
            for e in effects)

    layout = None
    mode = node.get("layoutMode")
    if mode and mode != "NONE":
        pads = ",".join(_num(node.get(k) or 0) for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft"))
        layout = ";".join([{"HORIZONTAL": "H", "VERTICAL": "V"}.get(mode, mode), _num(node.get("itemSpacing") or 0), pads,
                           "wrap" if node.get("layoutWrap") == "WRAP" else "",
                           f"{node.get('primaryAxisAlignItems', 'MIN')}/{node.get('counterAxisAlignItems', 'MIN')}"])

    op = node.get("opacity")
    opacity = round(op * 100) if isinstance(op, (int, float)) and op < 0.995 else None

    dev = {"READY_FOR_DEV": "R", "COMPLETED": "C"}.get((node.get("devStatus") or {}).get("type"))
    ovr = 1 if ntype == "INSTANCE" and node.get("overrides") else None

    dests: list[str] = []
    for inter in node.get("interactions") or []:
        for a in inter.get("actions") or []:
            d = (a or {}).get("destinationId")
            if d and d not in dests:
                dests.append(d)
    if not dests and node.get("transitionNodeID"):
        dests.append(node["transitionNodeID"])
    link = ",".join(dests) or None
    return font, tstyle, img, radius, effect, layout, opacity, dev, ovr, link


def _default_shape_name(name: str) -> bool:
    return bool(DEFAULT_SHAPE_NAME.match(name.strip()))


def compact(root: dict, styles: dict | None = None) -> tuple[list[tuple], Counter, Counter]:
    """Дерево Figma → строки (id, parent_id, type, name, visible, component_id, text, x, y, w, h, image,
    page_id, hid, pinst, fill, fill_a, fill_src, stroke, stroke_a, stroke_src, *EXTRA_COLS).

    styles — словарь стилей из ответа Figma (id → название), чтобы у цвета был виден его стиль.

    Простые фигуры (векторы, прямоугольники…) храним, только если это картинка или у слоя
    своё название («Light», «Girls_Bg»); с названием по умолчанию («Vector», «Rectangle 12»)
    отбрасываем — их в макетах больше всего, а искать их по названию бессмысленно.
    Возвращает также счётчики типов: сколько пришло всего и сколько сохранено.
    """
    rows: list[tuple] = []
    styles = styles or {}
    seen, kept = Counter(), Counter()
    # (узел, id ближайшего сохранённого родителя, скрыт ли кто-то из пропущенных родителей,
    #  страница, скрыт ли кто-то из всех родителей, ближайший родитель-инстанс)
    stack = [(root, None, False, None, False, None)]
    while stack:
        node, parent, hidden_above, page_id, hid_above, pinst = stack.pop()
        ntype = node.get("type", "")
        seen[ntype] += 1
        name = node.get("name", "")
        own_hidden = node.get("visible", True) is False
        image = _has_image(node)
        keep = ntype not in SKIP_TYPES or image or not _default_shape_name(name)
        if ntype == "CANVAS":
            page_id = node.get("id")
        hid_all = hid_above or own_hidden
        if keep:
            kept["IMAGE" if image and ntype in SKIP_TYPES else ntype] += 1
            rows.append((
                node.get("id", ""), parent, ntype, name,
                0 if (own_hidden or hidden_above) else 1,
                node.get("componentId") if ntype == "INSTANCE" else None,
                node.get("characters") if ntype == "TEXT" else None,
                *_box(node),
                1 if image else 0,
                page_id, 1 if hid_all else 0, pinst,
                *_paint(node, "fills", "fill", styles),
                *_paint(node, "strokes", "stroke", styles),
                *_extras(node, ntype, styles),
            ))
            child_parent, child_hidden = node.get("id"), False
        else:
            # пропущенный слой: его детей привязываем к ближайшему сохранённому родителю
            # и не теряем скрытость
            child_parent, child_hidden = parent, hidden_above or own_hidden
        child_pinst = node.get("id") if ntype == "INSTANCE" and keep else pinst
        for child in reversed(node.get("children") or []):
            stack.append((child, child_parent, child_hidden, page_id, hid_all, child_pinst))
    return rows, seen, kept


async def _download(path: str, token: str, params: dict | None, progress=None) -> tuple[bytes, int, float]:
    """Возвращает (тело ответа, байт передано по сети, секунд). progress(байт) — по мере скачивания.
    При разовом сбое сети или таймауте один раз повторяет запрос."""
    try:
        return await _download_once(path, token, params, progress)
    except FigmaError as exc:
        if exc.code not in ("timeout", "network"):
            raise
        log.info("Сбой связи с Figma (%s), повторяем запрос", exc.code)
        await asyncio.sleep(3)
        return await _download_once(path, token, params, progress)


async def _download_once(path: str, token: str, params: dict | None, progress=None) -> tuple[bytes, int, float]:
    started = time.perf_counter()
    chunks: list[bytes] = []
    got = 0
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("GET", f"{API_BASE}{path}", params=params, headers={"X-Figma-Token": token}) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _error_from_response(resp)
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got)
                wire = resp.num_bytes_downloaded
    except httpx.TimeoutException:
        raise FigmaError("timeout", "Figma не ответила вовремя. Проверьте сеть и повторите.", 504)
    except httpx.RequestError:
        raise FigmaError("network", "Ошибка сети: не удалось подключиться к Figma.", 502)
    return b"".join(chunks), wire, time.perf_counter() - started


def _parse_and_store(file_key: str, node_id: str | None, body: bytes, page_ids: list[str] | None = None,
                     report=None) -> dict:
    """Разбор JSON, сжатие и запись в SQLite. Выполняется в отдельном потоке.

    page_ids — страницы, отобранные по правилам (ссылка на весь файл): ответ /nodes
    с несколькими страницами собираем в один документ.
    """
    report = report or (lambda _d: None)
    t0 = time.perf_counter()
    report({"phase": "parse", "phase_started": time.time()})
    try:
        data = json.loads(body)
    except ValueError:
        raise FigmaError("incomplete", "Получен повреждённый ответ от Figma.", 502)
    if page_ids:
        nodes = data.get("nodes") or {}
        pages, comps, sets, styles = [], {}, {}, {}
        for pid in page_ids:
            entry = nodes.get(pid)
            if not entry or not isinstance(entry.get("document"), dict):
                raise FigmaError("incomplete", f"Figma не вернула страницу {pid}. Загрузка не выполнена.", 502)
            pages.append(entry["document"])
            comps.update(entry.get("components") or {})
            sets.update(entry.get("componentSets") or {})
            styles.update(entry.get("styles") or {})
        root = {"id": "0:0", "type": "DOCUMENT", "name": data.get("name", ""), "children": pages}
    elif node_id:
        entry = (data.get("nodes") or {}).get(node_id)
        if not entry or not isinstance(entry.get("document"), dict):
            raise FigmaError("node_not_found", f"Элемент из ссылки (node-id {node_id}) не найден в файле.", 404)
        root, comps, sets = entry["document"], entry.get("components") or {}, entry.get("componentSets") or {}
        styles = entry.get("styles") or {}
    else:
        if not isinstance(data.get("document"), dict):
            raise FigmaError("incomplete", "Получен неполный ответ от Figma (нет структуры документа).", 502)
        root, comps, sets = data["document"], data.get("components") or {}, data.get("componentSets") or {}
        styles = data.get("styles") or {}
    t1 = time.perf_counter()
    report({"phase": "compact", "phase_started": time.time()})

    rows, seen, kept = compact(root, styles)
    comp_rows = [
        (cid, c.get("key"), c.get("name"), c.get("componentSetId"),
         (sets.get(c.get("componentSetId") or "") or {}).get("name"), 1 if c.get("remote") else 0)
        for cid, c in comps.items()
    ]
    t2 = time.perf_counter()

    scope_id = node_id or ""
    report({"phase": "write", "phase_started": time.time(), "rows": len(rows)})
    db_before = _db_bytes()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM nodes WHERE file_key=? AND scope_id=?", (file_key, scope_id))
            con.execute("DELETE FROM components WHERE file_key=? AND scope_id=?", (file_key, scope_id))
            rows = _encode_dict_cols(con, rows)
            cols = ("id, parent_id, type, name, visible, component_id, text, x, y, w, h, image, page_id, hid, pinst, "
                    "fill, fill_a, fill_src, stroke, stroke_a, stroke_src, " + ", ".join(EXTRA_COLS))
            con.executemany(
                f"INSERT INTO nodes (file_key, scope_id, {cols}) VALUES ({','.join('?' * (2 + len(rows[0]) if rows else 2))})",
                [(file_key, scope_id, *r) for r in rows],
            )
            con.executemany(
                "INSERT INTO components VALUES (?,?,?,?,?,?,?,?)",
                [(file_key, scope_id, *r) for r in comp_rows],
            )
            con.execute(
                "INSERT OR REPLACE INTO files (file_key, scope_id, name, scope_name, version, last_modified, "
                "loaded_at, colors) VALUES (?,?,?,?,?,?,?,?)",
                (file_key, scope_id, data.get("name", ""), root.get("name", ""),
                 str(data.get("version", "")), data.get("lastModified", ""),
                 datetime.now().isoformat(timespec="seconds"), DATA_FORMAT),
            )
        # Без VACUUM: он переписывает всю базу (больше 1 ГБ — десятки секунд, всё это время база занята).
        # Место от прежней версии макета освобождается внутри файла и заполняется следующими загрузками.
        # Сколько места в базе занимает именно этот файл: считаем по содержимому строк.
        stored_bytes = con.execute(
            "SELECT COALESCE(SUM(LENGTH(id)+LENGTH(COALESCE(parent_id,''))+LENGTH(type)+LENGTH(name)"
            "+LENGTH(COALESCE(component_id,''))+LENGTH(COALESCE(text,''))+16),0) FROM nodes WHERE file_key=? AND scope_id=?",
            (file_key, scope_id),
        ).fetchone()[0]
    finally:
        con.close()
    t3 = time.perf_counter()

    return {
        "file_name": data.get("name", ""),
        "scope_name": root.get("name", "") if node_id else ("страницы по правилам" if page_ids else "весь файл"),
        "scope_type": root.get("type", "") if node_id else "DOCUMENT",
        "version": str(data.get("version", "")),
        "parse_s": round(t1 - t0, 3),
        "compact_s": round(t2 - t1, 3),
        "write_s": round(t3 - t2, 3),
        "nodes_total": sum(seen.values()),
        "nodes_stored": len(rows),
        "texts": kept.get("TEXT", 0),
        "images": sum(1 for r in rows if r[11]),
        "instances": kept.get("INSTANCE", 0),
        "components": len(comp_rows),
        "types_total": seen.most_common(12),
        "types_stored": kept.most_common(12),
        "stored_bytes": stored_bytes,
        "with_box": sum(1 for r in rows if r[9] is not None),
        "colors": True,
        "format": DATA_FORMAT,
        "with_fill": sum(1 for r in rows if r[15]),
        "db_bytes_before": db_before,
        "db_bytes": _db_bytes(),
    }


FIRST_DICT_COL = 21  # позиция font в строке compact()


def _encode_dict_cols(con, rows: list[tuple]) -> list[tuple]:
    """Строковые значения словарных колонок (шрифт, картинка…) → номера в таблице vals."""
    n = len(EXTRA_DICT_COLS)
    values = {r[i] for r in rows for i in range(FIRST_DICT_COL, FIRST_DICT_COL + n) if r[i] is not None}
    if not values:
        return rows
    con.executemany("INSERT OR IGNORE INTO vals (v) VALUES (?)", [(v,) for v in values])
    ids: dict[str, int] = {}
    vals = list(values)
    for k in range(0, len(vals), 900):
        chunk = vals[k:k + 900]
        ids.update(con.execute(f"SELECT v, id FROM vals WHERE v IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    out = []
    for r in rows:
        mid = tuple(None if v is None else ids[v] for v in r[FIRST_DICT_COL:FIRST_DICT_COL + n])
        out.append(r[:FIRST_DICT_COL] + mid + r[FIRST_DICT_COL + n:])
    return out


class LoadRequest(BaseModel):
    file_url: str


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "code": code, "message": message}, status_code=http_status)


class LoadError(Exception):
    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


async def run_load(file_url: str, progress=None) -> dict:
    """Загружает макет из ссылки в базу и возвращает замеры. progress(dict) — этапы и байты."""
    report = progress or (lambda _d: None)
    try:
        token = load_token()
        file_key = parse_file_key(file_url)
        node_id = parse_node_id(file_url)
    except ConfigError as exc:
        raise LoadError("config", str(exc), 400)

    started = time.perf_counter()
    pages_info = None
    try:
        if node_id:
            report({"phase": "pages", "pages_total": 1, "pages_loaded": 1})
            body, wire, download_s = await _download(f"/files/{file_key}/nodes", token, {"ids": node_id},
                                                     lambda b: report({"bytes": b}))
            report({"phase": "store"})
            stats = await asyncio.to_thread(_parse_and_store, file_key, node_id, body, None, report)
        else:
            # Ссылка на весь файл: лёгким запросом получаем список страниц, отбираем
            # по правилам из настроек и скачиваем только подходящие.
            report({"phase": "list"})
            rules = links.page_rules()
            list_body, list_wire, list_s = await _download(f"/files/{file_key}", token, {"depth": 1})
            all_pages = [
                {"id": p.get("id"), "name": p.get("name", "")}
                for p in (json.loads(list_body).get("document") or {}).get("children") or []
            ]
            chosen = [p for p in all_pages if links.page_matches(p["name"], rules)] if rules else all_pages
            if not chosen:
                names = ", ".join(f"«{p['name']}»" for p in all_pages) or "нет"
                raise LoadError("no_pages", f"Ни одна страница не подходит под правила ({', '.join(links.rule_label(r) for r in rules)}). "
                                f"Страницы файла: {names}. Правила меняются в «Настройках».", 400)
            ids = [p["id"] for p in chosen]
            report({"phase": "pages", "pages_total": len(all_pages), "pages_loaded": len(chosen)})
            body, wire, download_s = await _download(f"/files/{file_key}/nodes", token, {"ids": ",".join(ids)},
                                                     lambda b: report({"bytes": b}))
            report({"phase": "store"})
            stats = await asyncio.to_thread(_parse_and_store, file_key, None, body, ids, report)
            wire += list_wire
            pages_info = {
                "rules": rules, "list_s": round(list_s, 3), "list_bytes": len(list_body),
                "loaded": [p["name"] for p in chosen],
                "skipped": [p["name"] for p in all_pages if p not in chosen],
            }
            download_s += list_s
    except FigmaError as exc:
        raise LoadError(exc.code, exc.message, exc.http_status)
    except sqlite3.Error as exc:
        log.exception("Ошибка записи в базу")
        raise LoadError("db", f"Не удалось записать в локальную базу: {exc.__class__.__name__}.", 500)

    stats.update({
        "file_key": file_key,
        "node_id": node_id,
        "download_s": round(download_s, 3),
        "total_s": round(time.perf_counter() - started, 3),
        "json_bytes": len(body),
        "wire_bytes": wire,
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
        "pages": pages_info,
    })
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO loads (file_key, scope_id, name, scope_name, loaded_at, stats) VALUES (?,?,?,?,?,?)",
                (file_key, node_id or "", stats["file_name"], stats["scope_name"], stats["loaded_at"],
                 json.dumps(stats, ensure_ascii=False)),
            )
    finally:
        con.close()
    return stats


@router.post("/load")
async def load(req: LoadRequest) -> JSONResponse:
    try:
        stats = await run_load(req.file_url)
    except LoadError as exc:
        return _error(exc.code, exc.message, exc.http_status)
    return JSONResponse({"status": "ok", "stats": stats, "db": _db_state()})


def _db_state() -> dict:
    if not DB_PATH.exists():
        return {"path": _db_display(), "bytes": 0, "files": [], "loads": []}
    con = _connect()
    try:
        files = [
            {"file_key": r[0], "scope_id": r[1], "name": r[2], "scope_name": r[3], "version": r[4],
             "loaded_at": r[6], "nodes": r[7]}
            for r in con.execute(
                "SELECT f.file_key, f.scope_id, f.name, f.scope_name, f.version, f.last_modified, f.loaded_at, (SELECT COUNT(*) FROM nodes n WHERE n.file_key=f.file_key AND n.scope_id=f.scope_id) "
                "FROM files f ORDER BY loaded_at DESC")
        ]
        loads = [
            {"id": r[0], "loaded_at": r[1], **json.loads(r[2])}
            for r in con.execute("SELECT id, loaded_at, stats FROM loads ORDER BY id DESC LIMIT 20")
        ]
    finally:
        con.close()
    return {"path": _db_display(), "bytes": _db_bytes(), "files": files, "loads": loads}


@router.get("/db")
async def db_state() -> JSONResponse:
    return JSONResponse(_db_state())


def delete_scope(file_key: str, scope_id: str) -> dict:
    """Удаляет из базы всё, что загружено по ссылке: объекты, компоненты, запись о файле и историю."""
    if not DB_PATH.exists():
        return {"nodes": 0}
    con = _connect()
    try:
        with con:
            removed = con.execute("SELECT COUNT(*) FROM nodes WHERE file_key=? AND scope_id=?", (file_key, scope_id)).fetchone()[0]
            for table in ("nodes", "components", "files", "loads"):
                con.execute(f"DELETE FROM {table} WHERE file_key=? AND scope_id=?", (file_key, scope_id))
        con.execute("VACUUM")  # чтобы файл базы сразу уменьшился
    finally:
        con.close()
    return {"nodes": removed}


def node_url(file_key: str, node_id: str) -> str:
    """Ссылка «Открыть в Figma» на конкретный слой (ID вида 1:2 или I1:2;3:4)."""
    return f"https://www.figma.com/design/{file_key}/?node-id={quote(node_id.replace(':', '-'), safe='-')}"


# --- Поиск по локальной базе ---
# Сервер ищет один раз по запросу и отдаёт все найденные объекты компактно (/find); фильтры,
# вкладки макетов, скрытые, страницы и копирование браузер делает сам по этому списку.
# Страница, скрытость с учётом родителей и родитель-инстанс лежат в базе у каждого объекта
# (page_id, hid, pinst), поэтому дерево макета в память не загружается. Путь, размер и
# координаты для подсказки считаются по запросу для одного объекта (/node).

LAYER_GROUPS = {"COMPONENT": "masters", "COMPONENT_SET": "masters", "FRAME": "frames", "SECTION": "sections",
                "GROUP": "groups", "CANVAS": "pages", "INSTANCE": "named_instances"}
LAYER_TITLES = {"masters": "Мастер-компоненты", "frames": "Фреймы", "sections": "Секции", "groups": "Группы",
                "pages": "Страницы", "named_instances": "Инстансы с таким названием слоя",
                "images": "Картинки", "shapes": "Фигуры и векторы", "other": "Другие слои"}
LAYER_ORDER = ["masters", "frames", "sections", "groups", "images", "shapes", "named_instances", "pages", "other"]
GROUP_TITLES = {"instances": "Инстансы компонента", "instances_any": "Инстансы", **LAYER_TITLES, "texts": "Тексты"}
GROUP_ORDER = ["instances", "instances_any", *LAYER_ORDER, "texts"]
PAINT_KEYS = ("fill", "fill_a", "fill_src", "stroke", "stroke_a", "stroke_src")

# --- Досчёт page_id / hid / pinst для макетов, загруженных до появления этих полей ---
_backfill_lock = threading.Lock()
_backfill_done = {"ok": False}


def ensure_backfilled() -> None:
    """Один раз проходит по макетам без page_id/hid/pinst и досчитывает их по дереву родителей."""
    if _backfill_done["ok"] or not DB_PATH.exists():
        return
    with _backfill_lock:
        if _backfill_done["ok"]:
            return
        con = _connect()
        try:
            scopes = con.execute("SELECT DISTINCT file_key, scope_id FROM nodes WHERE hid IS NULL").fetchall()
            for fk, sid in scopes:
                started = time.perf_counter()
                rows = con.execute("SELECT id, parent_id, type, visible FROM nodes WHERE file_key=? AND scope_id=?",
                                   (fk, sid)).fetchall()
                known = {r[0] for r in rows}
                children: dict[str | None, list] = {}
                for r in rows:
                    children.setdefault(r[1] if r[1] in known else None, []).append(r)
                # Один проход от корней вниз: (страница, скрыт ли кто-то выше, ближайший инстанс выше)
                out = []
                stack = [(r, (None, 0, None)) for r in children.get(None, [])]
                while stack:
                    (nid, _parent, ntype, visible), (page, hid, pinst) = stack.pop()
                    page = nid if ntype == "CANVAS" else page
                    hid = 1 if (hid or not visible) else 0
                    out.append((page, hid, pinst, fk, sid, nid))
                    ctx = (page, hid, nid if ntype == "INSTANCE" else pinst)
                    stack.extend((c, ctx) for c in children.get(nid, []))
                with con:
                    con.executemany("UPDATE nodes SET page_id=?, hid=?, pinst=? WHERE file_key=? AND scope_id=? AND id=?", out)
                log.info("Досчитаны страницы и видимость: %s|%s, %d объектов за %.1f с", fk, sid, len(out),
                         time.perf_counter() - started)
        finally:
            con.close()
        _backfill_done["ok"] = True


def start_backfill() -> None:
    """При запуске сервера — досчитать в фоне, чтобы первый поиск не ждал."""
    threading.Thread(target=ensure_backfilled, name="backfill", daemon=True).start()


def _low_sql(q: str) -> str:
    # Встроенный lower() SQLite понижает регистр только у латиницы. Для запроса латиницей
    # этого достаточно (и в разы быстрее); для кириллицы и прочего — своя функция low().
    return "lower" if q.isascii() else "low"


def _size_sql(w: float | None, h: float | None, col: str = "") -> tuple[str, list]:
    """Условие на точные ширину/высоту (в базе — десятые доли пикселя): только то, что задано."""
    sql, args = "", []
    for name, v in (("w", w), ("h", h)):
        if v is not None:
            sql += f" AND {col}{name} = ?"
            args.append(round(v * 10))
    return sql, args


ALPHA_SPREAD = 2  # прозрачность выбирается с шагом 5 %: 70 % находит цвета с прозрачностью 68–72 %


def _color_sql(colors: list[str] | None, col: str = "", alpha: int | None = None) -> tuple[str, list]:
    """Условие на цвет: заливка (у текста — цвет текста) или обводка — один из цветов списка.
    alpha — прозрачность цвета в % (как opacity в Figma, 100 — непрозрачный): у той же заливки
    или обводки, чей цвет совпал."""
    if colors is None:
        return "", []
    if not colors:
        return " AND 0", []
    marks = ",".join("?" * len(colors))
    if alpha is None:
        return f" AND ({col}fill IN ({marks}) OR {col}stroke IN ({marks}))", [*colors, *colors]
    lo, hi = alpha - ALPHA_SPREAD, alpha + ALPHA_SPREAD
    return (f" AND (({col}fill IN ({marks}) AND COALESCE({col}fill_a, 100) BETWEEN ? AND ?)"
            f" OR ({col}stroke IN ({marks}) AND COALESCE({col}stroke_a, 100) BETWEEN ? AND ?))",
            [*colors, lo, hi, *colors, lo, hi])


def _near_colors(con, color: str, tol: float) -> list[str]:
    """Все цвета в базе, отличающиеся от заданного не больше чем на tol %.
    Разных цветов в макетах немного (тысячи), поэтому сравниваем их, а не каждый объект."""
    if tol <= 0:
        return [color]
    found = {color}
    for v in _all_colors(con):
        if color_distance(color, v) <= tol + 1e-9:
            found.add(v)
    return sorted(found)


_colors_cache: dict = {"stamp": None, "colors": []}


def _all_colors(con) -> list[str]:
    """Все разные цвета в базе. Запоминаются до изменения файла базы (новой загрузки или удаления)."""
    try:
        st = DB_PATH.stat()
        stamp = (str(DB_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if stamp is None or stamp != _colors_cache["stamp"]:
        _colors_cache["colors"] = [v for (v,) in con.execute(
            "SELECT fill FROM nodes WHERE fill IS NOT NULL UNION SELECT stroke FROM nodes WHERE stroke IS NOT NULL")]
        _colors_cache["stamp"] = stamp
    return _colors_cache["colors"]


def _lab(hex6: str) -> tuple[float, float, float]:
    """RRGGBB → CIE Lab (D65): в нём расстояние между цветами близко к тому, как их различает глаз."""
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hex6[i:i + 2], 16)) for i in (0, 2, 4))
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def color_distance(a: str, b: str) -> float:
    """Отличие цветов в % (ΔE76 в Lab; 100 — как чёрный от белого)."""
    la, lb = _lab(a), _lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5



def norm_color(v: str | None) -> str | None:
    """«#ff3366», «F36», «ff3366cc» → «FF3366». Пустое → None, неверное → ValueError."""
    v = (v or "").strip().lstrip("#").upper()
    if not v:
        return None
    if re.fullmatch(r"[0-9A-F]{3}", v):
        v = "".join(ch * 2 for ch in v)
    if not re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", v):
        raise ValueError(v)
    return v[:6]


def _find(q: str, w: float | None = None, h: float | None = None, color: str | None = None,
          tol: float = 0, alpha: int | None = None) -> dict:
    """Все объекты по запросу, по всем макетам, включая скрытые (фильтрует браузер).

    w, h — точные ширина и высота в пикселях: если заданы, учитываются вместе с названием,
    а без названия ищутся все объекты с таким размером."""
    ensure_backfilled()
    started = time.perf_counter()
    ql = q.lower()
    LOW = _low_sql(ql)
    size_n, size_args = _size_sql(w, h, "n.")
    size_plain, _ = _size_sql(w, h)
    con = _connect()
    con.create_function("low", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
    colors = _near_colors(con, color, tol) if color else None
    c_n, c_args = _color_sql(colors, "n.", alpha)
    c_plain, _ = _color_sql(colors, "", alpha)
    size_n, size_plain, size_args = size_n + c_n, size_plain + c_plain, size_args + c_args
    try:
        files = {f"{r[0]}|{r[1]}": {"id": f"{r[0]}|{r[1]}", "key": r[0], "name": r[2], "loaded_at": r[3],
                                    "colors": bool(r[4])}
                 for r in con.execute("SELECT file_key, scope_id, name, loaded_at, colors FROM files")}
        pages = {(r[0], r[1], r[2]): r[3] for r in con.execute(
            "SELECT file_key, scope_id, id, name FROM nodes WHERE type='CANVAS'")}
        items: list[dict] = []

        def add(group, fk, sid, nid, name, page_id, hid, paint=(), **extra):
            it = {"g": group, "f": f"{fk}|{sid}", "file_key": fk, "id": nid, "name": name,
                  "page": pages.get((fk, sid, page_id), ""), "hidden": bool(hid), **extra}
            if paint:
                it.update(zip(PAINT_KEYS, paint))
            items.append(it)

        if not ql:
            out = _find_by_size(con, files, pages, items, add, size_n, size_args, started, w, h)
            out["color"], out["tol"], out["alpha"] = color, tol, alpha
            return out

        # 1. Инстансы компонента: название компонента или набора вариантов совпадает (без учёта регистра).
        comp_rows = con.execute(
            "SELECT file_key, scope_id, id, key, name, set_id, set_name, remote FROM components "
            f"WHERE {LOW}(set_name)=? OR {LOW}(name)=?", (ql, ql)).fetchall()
        comp_ids = {(r[0], r[1], r[2]): r for r in comp_rows}
        matched = {}
        for r in comp_rows:
            is_set = bool(r[6]) and r[6].lower() == ql
            label, kind = (r[6], "набор вариантов") if is_set else (r[4], "компонент")
            c = matched.setdefault((label, kind), {"name": label, "kind": kind, "remote": bool(r[7]), "variants": set()})
            if is_set:
                c["variants"].add(r[4])
        seen_inst: set[tuple] = set()
        if comp_ids:
            # CROSS JOIN фиксирует порядок: сначала немногие подходящие компоненты, потом их инстансы по индексу
            for fk, sid, nid, name, cid, page_id, hid, *paint in con.execute(
                    "SELECT n.file_key, n.scope_id, n.id, n.name, n.component_id, n.page_id, n.hid, n.fill, n.fill_a, n.fill_src, n.stroke, n.stroke_a, n.stroke_src "
                    "FROM components c CROSS JOIN nodes n "
                    "  ON n.component_id = c.id AND n.file_key = c.file_key AND n.scope_id = c.scope_id "
                    f"WHERE ({LOW}(c.set_name)=? OR {LOW}(c.name)=?) AND n.type='INSTANCE'{size_n} "
                    "ORDER BY n.file_key, n.scope_id, n.id", (ql, ql, *size_args)):
                comp = comp_ids[(fk, sid, cid)]
                variant = comp[4] if comp[6] and comp[6].lower() == ql else None
                seen_inst.add((fk, sid, nid))
                add("instances", fk, sid, nid, name, page_id, hid, paint, variant=variant, comp=comp[6] or comp[4])

        # 2. Слои по названию: вхождение без учёта регистра, точные совпадения — первыми.
        layer_items: dict[str, list] = {}
        for fk, sid, nid, name, ntype, image, page_id, hid, *paint in con.execute(
                "SELECT file_key, scope_id, id, name, type, image, page_id, hid, fill, fill_a, fill_src, stroke, stroke_a, stroke_src FROM nodes "
                f"WHERE type NOT IN ('TEXT','DOCUMENT') AND instr({LOW}(name), ?) > 0{size_plain}", (ql, *size_args)):
            if (fk, sid, nid) in seen_inst:
                continue
            if image and ntype not in ("INSTANCE", "COMPONENT", "COMPONENT_SET"):
                key = "images"
            elif ntype in SKIP_TYPES:
                key = "shapes"
            else:
                key = LAYER_GROUPS.get(ntype, "other")
            layer_items.setdefault(key, []).append((fk, sid, nid, name, page_id, hid, ntype, name.lower() == ql, paint))
        for key in LAYER_ORDER:
            for fk, sid, nid, name, page_id, hid, ntype, exact, paint in sorted(layer_items.get(key, []), key=lambda t: not t[7]):
                add(key, fk, sid, nid, name, page_id, hid, paint, type=ntype, exact=exact)

        # 3. Тексты, содержащие запрос.
        for fk, sid, nid, name, text, page_id, hid, *paint in con.execute(
                "SELECT file_key, scope_id, id, name, text, page_id, hid, fill, fill_a, fill_src, stroke, stroke_a, stroke_src FROM nodes "
                f"WHERE type='TEXT' AND instr({LOW}(text), ?) > 0{size_plain}", (ql, *size_args)):
            add("texts", fk, sid, nid, name, page_id, hid, paint, text=text)

        # 4. Если точного компонента нет — похожие названия (подсказки).
        similar: list[str] = []
        if not comp_rows:
            for set_name, name in con.execute(
                    f"SELECT set_name, name FROM components WHERE instr({LOW}(COALESCE(set_name, name)), ?) > 0 LIMIT 200",
                    (ql,)):
                label = set_name or name
                if label not in similar:
                    similar.append(label)
            similar = sorted(similar)[:20]
    finally:
        con.close()
    return {
        "q": q, "w": w, "h": h, "color": color, "tol": tol, "alpha": alpha,
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "components": [{**c, "variants": sorted(c["variants"])} for c in matched.values()],
        "files": files,
        "items": items,
        "similar": similar,
    }


def _find_by_size(con, files, pages, items, add, size_n, size_args, started, w, h) -> dict:
    """Без названия: все объекты с точными шириной/высотой и/или цветом, по тем же группам.
    У инстансов — их компонент и вариант (для фильтров «Компонент» и свойств)."""
    layer_items: dict[str, list] = {}
    for fk, sid, nid, name, ntype, image, page_id, hid, text, cname, cset, *paint in con.execute(
            "SELECT n.file_key, n.scope_id, n.id, n.name, n.type, n.image, n.page_id, n.hid, n.text, c.name, c.set_name, "
            "n.fill, n.fill_a, n.fill_src, n.stroke, n.stroke_a, n.stroke_src "
            "FROM nodes n LEFT JOIN components c "
            "  ON c.file_key = n.file_key AND c.scope_id = n.scope_id AND c.id = n.component_id "
            f"WHERE n.type NOT IN ('DOCUMENT','CANVAS'){size_n} ORDER BY n.file_key, n.scope_id, n.id", size_args):
        if ntype == "INSTANCE":
            add("instances_any", fk, sid, nid, name, page_id, hid, paint,
                variant=cname if cset else None, comp=cset or cname)
        elif ntype == "TEXT":
            add("texts", fk, sid, nid, name, page_id, hid, paint, text=text)
        else:
            if image and ntype not in ("COMPONENT", "COMPONENT_SET"):
                key = "images"
            elif ntype in SKIP_TYPES:
                key = "shapes"
            else:
                key = LAYER_GROUPS.get(ntype, "other")
            layer_items.setdefault(key, []).append((fk, sid, nid, name, page_id, hid, ntype, paint))
    for key in LAYER_ORDER:
        for fk, sid, nid, name, page_id, hid, ntype, paint in layer_items.get(key, []):
            add(key, fk, sid, nid, name, page_id, hid, paint, type=ntype)
    items.sort(key=lambda it: GROUP_ORDER.index(it["g"]))
    return {"q": "", "w": w, "h": h, "ms": round((time.perf_counter() - started) * 1000, 1),
            "components": [], "files": files, "items": items, "similar": []}


def _pack(found: dict) -> dict:
    """Компактный ответ для браузера: повторяющиеся строки (названия, страницы, варианты) — один раз.

    Объект: [группа, макет, id, название, страница, вариант, скрыт, точное совпадение, текст, тип, компонент,
    заливка, непрозрачность заливки, источник заливки, обводка, непрозрачность обводки, источник обводки],
    где строковые поля — номера в общем списке строк s (или -1), непрозрачность — % или null (100%)."""
    strings: list[str] = []
    index: dict[str, int] = {}

    def s(v):
        if v is None:
            return -1
        i = index.get(v)
        if i is None:
            i = index[v] = len(strings)
            strings.append(v)
        return i

    groups = {g: n for n, g in enumerate(GROUP_ORDER)}
    fids = list(found["files"])
    fidx = {f: n for n, f in enumerate(fids)}
    rows = [[groups[it["g"]], fidx[it["f"]], it["id"], s(it["name"]), s(it["page"]), s(it.get("variant")),
             1 if it["hidden"] else 0, {True: 1, False: 0}.get(it.get("exact"), 2), s(it.get("text")), s(it.get("type")),
             s(it.get("comp")), s(it.get("fill")), it.get("fill_a"), s(it.get("fill_src")),
             s(it.get("stroke")), it.get("stroke_a"), s(it.get("stroke_src"))]
            for it in found["items"]]
    return {
        "status": "ok", "q": found["q"], "w": found.get("w"), "h": found.get("h"), "color": found.get("color"), "tol": found.get("tol") or 0, "alpha": found.get("alpha"), "ms": found["ms"],
        "components": found["components"], "similar": found["similar"],
        "groups": [{"key": g, "title": GROUP_TITLES[g]} for g in GROUP_ORDER],
        "files": [found["files"][f] for f in fids],
        "s": strings, "items": rows,
    }


class FindRequest(BaseModel):
    q: str = ""
    w: float | None = None  # точная ширина, px
    h: float | None = None  # точная высота, px
    color: str | None = None  # цвет HEX: «#FF3366» или «F36»
    tol: float = 0            # допустимое отклонение цвета, % (0 — точное совпадение)
    alpha: int | None = None  # прозрачность цвета, % как opacity в Figma (None — любая)


@router.post("/find")
async def find(req: FindRequest) -> JSONResponse:
    q = req.q.strip()
    try:
        color = norm_color(req.color)
    except ValueError:
        return _error("bad_color", "Цвет — в формате HEX, например #FF3366.", 400)
    if not 0 <= req.tol <= 100:
        return _error("bad_tol", "Отклонение цвета — от 0 до 100 %.", 400)
    if req.alpha is not None and not 0 <= req.alpha <= 100:
        return _error("bad_alpha", "Прозрачность цвета — от 0 до 100 %.", 400)
    if not q and req.w is None and req.h is None and not color:
        return _error("empty", "Введите название, размер (ширину, высоту) или цвет.", 400)
    for v in (req.w, req.h):
        if v is not None and not (0 <= v < 1_000_000):
            return _error("bad_size", "Размер должен быть числом в пикселях, например 56.", 400)
    if not DB_PATH.exists():
        return _error("no_db", "База пуста — сначала загрузите макеты в «Настройках».", 400)
    return JSONResponse(_pack(await asyncio.to_thread(_find, q, req.w, req.h, color, req.tol if color else 0,
                                                        req.alpha if color else None)))


class NodeRequest(BaseModel):
    file: str   # "file_key|scope_id"
    id: str


@router.post("/node")
async def node_details(req: NodeRequest) -> JSONResponse:
    """Подробности для подсказки: путь по контейнерам, размер, координаты, родитель-инстанс."""
    fk, _, sid = req.file.partition("|")
    return JSONResponse(await asyncio.to_thread(_node_details, fk, sid, req.id))


def _node_details(fk: str, sid: str, nid: str) -> dict:
    con = _connect()
    try:
        row = con.execute("SELECT parent_id, x, y, w, h, pinst, " + ", ".join(EXTRA_COLS) +
                          " FROM nodes WHERE file_key=? AND scope_id=? AND id=?", (fk, sid, nid)).fetchone()
        if not row:
            return {"status": "error", "message": "Объект не найден в базе."}
        parent, *box, pinst = row[:6]
        extra = dict(zip(EXTRA_COLS, row[6:]))
        ids = [extra[c] for c in EXTRA_DICT_COLS if extra[c] is not None]
        if ids:
            names = dict(con.execute(f"SELECT id, v FROM vals WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
            for c in EXTRA_DICT_COLS:
                if extra[c] is not None:
                    extra[c] = names.get(extra[c])
        extra = {k: v for k, v in extra.items() if v is not None}
        path = []
        for _ in range(200):  # защита от зацикливания
            if not parent:
                break
            r = con.execute("SELECT parent_id, name, type FROM nodes WHERE file_key=? AND scope_id=? AND id=?",
                            (fk, sid, parent)).fetchone()
            if not r or r[2] in ("CANVAS", "DOCUMENT"):
                break
            path.insert(0, r[1])
            parent = r[0]
        pname = None
        if pinst:
            pr = con.execute("SELECT name FROM nodes WHERE file_key=? AND scope_id=? AND id=?", (fk, sid, pinst)).fetchone()
            pname = pr[0] if pr else None
    finally:
        con.close()
    return {"status": "ok", "path": path, "box": box_px(*box), "extra": extra,
            "parent_instance": {"id": pinst, "name": pname, "url": node_url(fk, pinst)} if pinst else None}


# --- Серверная фильтрация (API /search и /export): то же, что делает браузер, — для проверок и скриптов ---

def _variant_props(variant: str | None) -> dict[str, str]:
    """«Size=RB 56px, Color=Trans Black» → {"Size": "RB 56px", "Color": "Trans Black"}."""
    props = {}
    for part in (variant or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                props[k.strip()] = v.strip()
    return props


def _facet_values(it: dict) -> dict[str, str]:
    """Значения быстрых фильтров у одного результата: страница и свойства варианта (считаются один раз)."""
    vals = it.get("_fv")
    if vals is None:
        vals = {"page": it.get("page") or "—"}
        if it.get("comp"):
            vals["comp"] = it["comp"]
        for k, v in _variant_props(it.get("variant")).items():
            vals["prop:" + k] = v
        it["_fv"] = vals
    return vals


def _passes(it: dict, filters: dict[str, list[str]], skip: str | None = None) -> bool:
    """Внутри фильтра — любое из выбранных значений, между фильтрами — все сразу."""
    vals = _facet_values(it)
    return all(vals.get(k) in set(sel) for k, sel in filters.items() if sel and k != skip)


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _facets(items: list[dict], filters: dict[str, list[str]]) -> list[dict]:
    """Фильтры с числами: счётчик значения учитывает остальные выбранные фильтры (но не этот же)."""
    keys: list[str] = []
    all_values: dict[str, set] = {}
    for it in items:
        for k, v in _facet_values(it).items():
            if k not in keys:
                keys.append(k)
            all_values.setdefault(k, set()).add(v)
    keys.sort(key=lambda k: (k != "page", k))
    out = []
    for key in keys:
        counts: Counter = Counter()
        for it in items:
            if _passes(it, filters, skip=key):
                v = _facet_values(it).get(key)
                if v is not None:
                    counts[v] += 1
        selected = set(filters.get(key) or [])
        values = sorted(all_values.get(key, set()) | selected, key=_natural)
        if len(values) < 2 and not selected:
            continue
        out.append({"key": key, "title": {"page": "Страница", "comp": "Компонент"}.get(key, key[5:]),
                    "values": [{"value": v, "count": counts.get(v, 0), "selected": v in selected} for v in values]})
    return out


def _page(block: dict, pages: dict[str, int] | None, size: int, key: str) -> dict:
    """Одна страница результатов группы: page (с 1), pages — сколько всего страниц."""
    total = len(block["items"])
    count = max(1, -(-total // size))
    page = min(max(1, int((pages or {}).get(key, 1))), count)
    return {**block, "items": [_with_url(it) for it in block["items"][(page - 1) * size: page * size]],
            "page": page, "pages": count}


def _with_url(it: dict) -> dict:
    out = {k: v for k, v in it.items() if k not in ("_fv", "g", "f")}
    out["file_id"] = it["f"]
    out["url"] = node_url(it["file_key"], it["id"])
    return out


def _filtered(found: dict, exclude_hidden: bool, only_file: str | None, filters: dict[str, list[str]]):
    """Отбор как в браузере: без скрытых, по вкладке макета, по фильтрам."""
    base = [it for it in found["items"] if not (exclude_hidden and it["hidden"])]
    by_file = Counter(it["f"] for it in base if _passes(it, filters))
    scoped = [it for it in base if not only_file or it["f"] == only_file]
    facets = _facets(scoped, filters)
    final = [it for it in scoped if _passes(it, filters)]
    hidden = sum(1 for it in found["items"] if it["hidden"] and (not only_file or it["f"] == only_file))
    return final, facets, by_file, hidden


def _search(q: str, exclude_hidden: bool, only_file: str | None = None,
            page_size: int = 50, pages: dict[str, int] | None = None,
            filters: dict[str, list[str]] | None = None) -> dict:
    found = _find(q)
    filters = {k: v for k, v in (filters or {}).items() if v}
    final, facets, by_file, hidden = _filtered(found, exclude_hidden, only_file, filters)
    blocks = {g: {"key": g, "title": GROUP_TITLES[g], "items": []} for g in GROUP_ORDER}
    for it in final:
        blocks[it["g"]]["items"].append(it)
    for b in blocks.values():
        b["total"] = len(b["items"])
    return {
        "status": "ok", "q": q, "ms": found["ms"],
        "components": found["components"], "similar": found["similar"],
        "instances": _page(blocks["instances"], pages, page_size, "instances"),
        "texts": _page(blocks["texts"], pages, page_size, "texts"),
        "layers": [_page(blocks[g], pages, page_size, g) for g in LAYER_ORDER if blocks[g]["total"]],
        "facets": facets, "filters": filters, "page_size": page_size,
        "hidden_skipped": hidden if exclude_hidden else 0,
        "file": only_file,
        "by_file": sorted([{"key": f, "name": d["name"], "total": by_file.get(f, 0)} for f, d in found["files"].items()],
                          key=lambda x: (-x["total"], x["name"])),
        "files": [{"name": d["name"], "loaded_at": d["loaded_at"]} for d in found["files"].values()],
    }


class SearchRequest(BaseModel):
    q: str
    exclude_hidden: bool = False
    file: str | None = None
    page_size: int = 50
    pages: dict[str, int] = {}
    filters: dict[str, list[str]] = {}


@router.post("/search")
async def search(req: SearchRequest) -> JSONResponse:
    q = req.q.strip()
    if not q:
        return _error("empty", "Введите название компонента или текст.", 400)
    if not DB_PATH.exists():
        return _error("no_db", "База пуста — сначала загрузите макет.", 400)
    size = max(10, min(req.page_size, 500))
    return JSONResponse(await asyncio.to_thread(_search, q, req.exclude_hidden, req.file or None, size, req.pages, req.filters))


class ExportRequest(BaseModel):
    q: str
    group: str
    exclude_hidden: bool = False
    file: str | None = None
    filters: dict[str, list[str]] = {}


@router.post("/export")
async def export(req: ExportRequest) -> JSONResponse:
    """Все результаты одной группы (без разбиения на страницы) — для кнопки «Скопировать»."""
    q = req.q.strip()
    if not q or not DB_PATH.exists():
        return _error("empty", "Нечего копировать.", 400)
    found = await asyncio.to_thread(_find, q)
    final, *_ = _filtered(found, req.exclude_hidden, req.file or None, {k: v for k, v in req.filters.items() if v})
    items = [it for it in final if it["g"] == req.group]
    if not items:
        return _error("empty", "В этой группе нет результатов.", 404)
    keep = ("id", "name", "page", "variant", "text", "hidden")
    return JSONResponse({"status": "ok", "total": len(items), "items": [
        {**{k: it.get(k) for k in keep}, "file": found["files"][it["f"]]["name"], "url": node_url(it["file_key"], it["id"])}
        for it in items]})
