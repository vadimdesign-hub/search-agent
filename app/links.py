"""Сохранённые ссылки на макеты Figma (страница «Настройки»).

Хранятся в config.json рядом с токеном, в поле figma_links. Пока только сохраняются —
дальше система будет работать с этим списком.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from urllib.parse import unquote, urlparse

from .config import ConfigError, _URL_PATH_RE, config_path, parse_file_key, parse_node_id, read_config_file


def _write_config(data: dict) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def describe(url: str) -> dict:
    """Разбирает ссылку: ключ файла, элемент, название макета из адреса. Бросает ConfigError."""
    url = url.strip()
    key = parse_file_key(url)
    node = parse_node_id(url)
    parsed = urlparse(url if "://" in url else "https://" + url)
    m = _URL_PATH_RE.match(parsed.path)
    rest = parsed.path[m.end():].strip("/") if m else ""
    slug = unquote(rest.split("/")[0]) if rest else ""
    title = slug.replace("-", " ").strip() or f"Файл {key}"
    # Ссылка без служебных параметров (t=…, share=…): только файл и элемент.
    clean = f"https://www.figma.com/design/{key}/{slug}" if slug else f"https://www.figma.com/design/{key}"
    if node:
        clean += "?node-id=" + node.replace(":", "-")
    return {"url": clean, "file_key": key, "node_id": node, "title": title,
            "scope": f"элемент {node}" if node else "весь файл"}


def _stored() -> list[dict]:
    links = read_config_file().get("figma_links") or []
    return [l for l in links if isinstance(l, dict) and l.get("url")]


def list_links() -> list[dict]:
    out = []
    for item in _stored():
        try:
            out.append({**describe(item["url"]), "added_at": item.get("added_at", "")})
        except ConfigError:
            continue
    return out


def add_link(url: str) -> tuple[list[dict], bool]:
    """Добавляет ссылку. Возвращает (список, добавлена ли — False, если такая уже есть)."""
    info = describe(url)
    stored = _stored()
    if any(_same(s["url"], info) for s in stored):
        return list_links(), False
    data = read_config_file()
    data["figma_links"] = stored + [{"url": info["url"], "added_at": datetime.now().isoformat(timespec="seconds")}]
    _write_config(data)
    return list_links(), True


def remove_link(url: str) -> list[dict]:
    info = describe(url)
    data = read_config_file()
    data["figma_links"] = [s for s in _stored() if not _same(s["url"], info)]
    _write_config(data)
    return list_links()


def _same(stored_url: str, info: dict) -> bool:
    try:
        other = describe(stored_url)
    except ConfigError:
        return False
    return other["file_key"] == info["file_key"] and other["node_id"] == info["node_id"]


# --- Правила: какие страницы макета загружать в базу ---
# Правило: {"text": "Stage", "exact": False}. exact=False — название страницы содержит текст
# («Stage» подходит для «Stage 1»), exact=True — название совпадает целиком («Flow» — только «Flow»).
# Регистр в обоих случаях не важен.

DEFAULT_PAGE_RULES = [
    {"text": "Stage", "exact": False},
    {"text": "Local components", "exact": False},
    {"text": "Flow", "exact": False},
]


def _norm_rule(r) -> dict | None:
    """Правило из настроек или запроса; старый формат — просто строка (= по вхождению)."""
    if isinstance(r, str):
        r = {"text": r, "exact": False}
    if not isinstance(r, dict) or not isinstance(r.get("text"), str) or not r["text"].strip():
        return None
    return {"text": r["text"].strip(), "exact": bool(r.get("exact"))}


def page_rules() -> list[dict]:
    rules = read_config_file().get("page_rules")
    if not isinstance(rules, list):
        return [dict(r) for r in DEFAULT_PAGE_RULES]
    return [r for r in (_norm_rule(x) for x in rules) if r]


def save_page_rules(rules: list) -> list[dict]:
    clean: list[dict] = []
    for r in (_norm_rule(x) for x in rules):
        if r and r["text"].lower() not in (c["text"].lower() for c in clean):
            clean.append(r)
    data = read_config_file()
    data["page_rules"] = clean
    _write_config(data)
    return clean


def page_matches(name: str, rules: list[dict]) -> bool:
    low = name.strip().lower()
    return any((low == r["text"].lower()) if r["exact"] else (r["text"].lower() in low) for r in rules)


def rule_label(r: dict) -> str:
    return f"«{r['text']}»" + (" (точно)" if r["exact"] else "")
