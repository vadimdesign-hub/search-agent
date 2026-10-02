"""Сервер «по требованию»: работает, только пока открыта страница поиска.

macOS (launchd) держит порт 8000 и запускает сервер при первом обращении страницы.
Страница раз в несколько секунд присылает «я открыта» (/api/heartbeat), а при закрытии —
«закрыта» (/api/bye). Когда открытых страниц нет и нет незавершённых запросов,
сервер завершается; при следующем открытии страницы launchd запустит его снова.
"""

from __future__ import annotations

import os
import sys
import time

# Сколько ждать сигнала от страницы. Фоновые вкладки браузер может «усыплять»
# до одного сигнала в минуту, поэтому запас больше минуты.
PAGE_TIMEOUT_S = 90
BYE_GRACE_S = 5          # после закрытия страницы: вдруг это была перезагрузка
STARTUP_GRACE_S = 20     # после запуска: страница ещё не успела прислать сигнал

_pages: dict[str, float] = {}
_state = {"started": time.time(), "inflight": 0, "last_bye": 0.0}


def page_alive(page_id: str) -> None:
    _pages[page_id] = time.time()


def page_closed(page_id: str) -> None:
    _pages.pop(page_id, None)
    _state["last_bye"] = time.time()


def request_started() -> None:
    _state["inflight"] += 1


def request_finished() -> None:
    _state["inflight"] -= 1


def pages_open(now: float | None = None) -> int:
    now = now or time.time()
    return sum(1 for seen in _pages.values() if now - seen < PAGE_TIMEOUT_S)


def should_stop(now: float | None = None) -> bool:
    now = now or time.time()
    if _state["inflight"] > 0 or pages_open(now):
        return False
    if now - _state["started"] < STARTUP_GRACE_S:
        return False
    return now - _state["last_bye"] >= BYE_GRACE_S


def info() -> dict:
    """Состояние сервера для индикатора в шапке страницы."""
    return {
        "uptime_s": round(time.time() - _state["started"]),
        "pages_open": pages_open(),
        "on_demand": "--on-demand" in sys.argv,
        "pid": os.getpid(),
    }
