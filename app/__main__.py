"""Запуск сервера.

    python -m app              — обычный запуск (работает, пока не остановите);
    python -m app --on-demand  — запуск службой macOS по обращению страницы: порт 8000
                                 держит launchd, сервер сам завершается, когда страницу закрыли.
"""

import asyncio
import ctypes
import logging
import os
import socket
import sys

import uvicorn

HOST = "127.0.0.1"
LAUNCHD_SOCKET_NAME = b"Listeners"  # имя из Sockets в plist службы

log = logging.getLogger("app.lifecycle")


def _launchd_sockets() -> list[socket.socket]:
    """Сокеты, которые launchd передал процессу (launch_activate_socket, macOS 10.10+)."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    err = libc.launch_activate_socket(LAUNCHD_SOCKET_NAME, ctypes.byref(fds), ctypes.byref(count))
    if err:
        raise OSError(err, "launchd не передал сокет (служба не установлена?)")
    socks = [socket.socket(fileno=fds[i]) for i in range(count.value)]
    libc.free(fds)
    return socks


def _listen(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, port))
    s.listen(128)
    s.setblocking(False)
    return s


async def _stop_when_idle(server: uvicorn.Server) -> None:
    from app import lifecycle

    while not server.should_exit:
        await asyncio.sleep(2)
        if lifecycle.should_stop():
            log.info("Страница поиска закрыта — сервер останавливается")
            server.should_exit = True


async def main() -> None:
    on_demand = "--on-demand" in sys.argv
    port = int(os.environ.get("PORT", "8000"))

    sockets = _launchd_sockets() if on_demand else [_listen(port)]

    server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="info"))
    # Макеты, загруженные до появления полей «страница / скрыт / родитель-инстанс», досчитываем в фоне.
    from app.experiment import start_backfill
    start_backfill()
    if on_demand:
        asyncio.get_running_loop().create_task(_stop_when_idle(server))
    else:
        print(f"Откройте http://{HOST}:{port}")
    await server.serve(sockets=sockets)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
