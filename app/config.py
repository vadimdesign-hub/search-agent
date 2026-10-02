"""Локальные настройки: ссылка на Figma-файл и токен доступа.

Источники (по приоритету): переменные окружения FIGMA_FILE_URL / FIGMA_TOKEN,
затем файл config.json в корне проекта (путь можно переопределить через
FIGMA_SEARCH_CONFIG).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

# figma.com/design/<key>/..., /file/<key>/..., /proto/<key>/...,
# ветки: /design/<key>/branch/<branchKey>/...
_URL_PATH_RE = re.compile(
    r"^/(?:file|design|proto)/(?P<key>[A-Za-z0-9]+)(?:/branch/(?P<branch>[A-Za-z0-9]+))?"
)
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9]{10,}$")
_NODE_ID_RE = re.compile(r"^I?\d+:\d+(?:;\d+:\d+)*$")


class ConfigError(Exception):
    """Настройки отсутствуют или некорректны. Сообщение показывается пользователю."""


@dataclass(frozen=True)
class Settings:
    file_url: str
    file_key: str
    token: str
    node_id: str | None = None  # узел из ссылки (?node-id=…) — область поиска

    def __repr__(self) -> str:  # токен не должен попасть в логи через repr
        return f"Settings(file_key={self.file_key!r}, node_id={self.node_id!r}, token=***)"


def parse_file_key(url: str) -> str:
    """Возвращает ключ файла (или ветки) из ссылки Figma."""
    url = (url or "").strip()
    if not url:
        raise ConfigError("Не задана ссылка на Figma-файл (figma_file_url).")
    if _BARE_KEY_RE.match(url):
        return url
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "figma.com" and not host.endswith(".figma.com"):
        raise ConfigError(
            "Неверная ссылка на Figma-файл: ожидается адрес вида "
            "https://www.figma.com/design/<ключ файла>/..."
        )
    m = _URL_PATH_RE.match(parsed.path)
    if not m:
        raise ConfigError(
            "Неверная ссылка на Figma-файл: не удалось найти ключ файла. "
            "Скопируйте ссылку через Share → Copy link."
        )
    return m.group("branch") or m.group("key")


def parse_node_id(url: str) -> str | None:
    """node-id из ссылки: в URL он записан как 1-2 (или 1:2), в API — 1:2."""
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url
    values = parse_qs(urlparse(url).query).get("node-id")
    if not values or not values[0].strip():
        return None
    node_id = values[0].strip().replace("-", ":")
    if not _NODE_ID_RE.match(node_id):
        raise ConfigError("Неверная ссылка: не удалось разобрать node-id. Скопируйте ссылку на секцию заново (Copy link to selection).")
    return node_id


def config_path() -> Path:
    return Path(os.environ.get("FIGMA_SEARCH_CONFIG", DEFAULT_CONFIG_PATH))


def read_config_file() -> dict:
    """Содержимое config.json (или {} если файла нет / он повреждён)."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(file_url: str | None = None, token: str | None = None) -> None:
    """Сохраняет в config.json то, что передано: последнюю ссылку и/или токен."""
    data = read_config_file()
    if file_url is not None and file_url.strip():
        parse_file_key(file_url)  # проверяем ссылку до записи
        parse_node_id(file_url)
        data["figma_file_url"] = file_url.strip()
    if token and token.strip():
        data["figma_token"] = token.strip()
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def env_overrides() -> list[str]:
    return [name for name in ("FIGMA_FILE_URL", "FIGMA_TOKEN") if os.environ.get(name)]


def load_token() -> str:
    token = (os.environ.get("FIGMA_TOKEN") or read_config_file().get("figma_token") or "").strip()
    if not token:
        raise ConfigError("Не задан токен доступа Figma: укажите его в разделе «Настройки».")
    _install_redaction(token)
    return token


def load_settings(file_url: str | None = None) -> Settings:
    """Токен — из настроек; ссылка — переданная в запросе, иначе последняя сохранённая.

    Область поиска определяется ссылкой: без node-id — весь файл, с node-id —
    только этот элемент (страница, секция, фрейм…).
    """
    path = config_path()
    if path.exists() and not read_config_file():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Не удалось прочитать файл настроек {path.name}: {exc.__class__.__name__}.")
    token = load_token()
    url = (file_url or "").strip() or os.environ.get("FIGMA_FILE_URL") or read_config_file().get("figma_file_url") or ""
    if not url.strip():
        raise ConfigError("Вставьте ссылку на Figma-файл, страницу, секцию или фрейм.")
    return Settings(file_url=url.strip(), file_key=parse_file_key(url), token=token, node_id=parse_node_id(url))


class _RedactFilter(logging.Filter):
    """Страховка: вырезает токен из любых записей лога."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            # Переписываем запись, только если в ней есть токен: иначе ломается
            # форматирование журналов, которым нужны исходные args (например, uvicorn.access).
            if any(s in msg for s in self.secrets):
                for s in self.secrets:
                    msg = msg.replace(s, "***")
                record.msg, record.args = msg, None
        return True


_redact_filter = _RedactFilter()


def _install_redaction(token: str) -> None:
    if token in _redact_filter.secrets:
        return
    _redact_filter.secrets.add(token)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "app"):
        logger = logging.getLogger(name)
        if _redact_filter not in logger.filters:
            logger.addFilter(_redact_filter)
        for handler in logger.handlers:
            if _redact_filter not in handler.filters:
                handler.addFilter(_redact_filter)
