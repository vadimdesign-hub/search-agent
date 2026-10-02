import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import ConfigError, parse_file_key, parse_node_id

FILE_KEY = "AbCdEf1234567890"


def node(id, type, name, children=None, **extra):
    n = {"id": id, "type": type, "name": name, **extra}
    if children is not None:
        n["children"] = children
    return n


def make_file():
    button_set = node("1:0", "COMPONENT_SET", "Button", [
        node("1:1", "COMPONENT", "Size=Large"),
        node("1:2", "COMPONENT", "Size=Small"),
    ])
    card = node("2:0", "COMPONENT", "Card", [
        node("2:1", "INSTANCE", "Button", componentId="1:2"),  # инстанс внутри мастера Card
    ])
    page1 = node("0:1", "CANVAS", "Components", [button_set, card])
    page2 = node("0:2", "CANVAS", "Screens", [
        node("10:0", "FRAME", "Login", [
            node("10:1", "INSTANCE", "Button", componentId="1:1",                    # цвет из переменной
                 fills=[{"type": "SOLID", "color": {"r": 1, "g": 0.2, "b": 0.4, "a": 1},
                         "boundVariables": {"color": {"type": "VARIABLE_ALIAS", "id": "VariableID:1:2"}}}]),
            node("10:2", "INSTANCE", "Submit CTA", componentId="1:2"),              # переименован
            node("10:3", "GROUP", "Hidden group", visible=False, children=[
                node("10:4", "INSTANCE", "Button", componentId="1:1"),              # скрыт через родителя
            ]),
            node("10:5", "INSTANCE", "Card", componentId="2:0", children=[
                node("I10:5;2:1", "INSTANCE", "Button", componentId="1:2"),         # вложен в инстанс
            ]),
            node("10:6", "FRAME", "Button", styles={"fill": "S:brand"},             # detached / обычный слой
                 fills=[{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0, "a": 1}},  # нижняя заливка
                        {"type": "SOLID", "color": {"r": 1, "g": 0.2, "b": 0.4, "a": 1}, "opacity": 0.5}],
                 strokes=[{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 1, "a": 1}}]),
            node("10:7", "TEXT", "Button", characters="Button",                     # цвет текста задан вручную
                 fills=[{"type": "SOLID", "color": {"r": 0.0667, "g": 0.0667, "b": 0.0667, "a": 1}},
                        {"type": "SOLID", "visible": False, "color": {"r": 1, "g": 1, "b": 1}}]),
        ]),
    ])
    page3 = node("0:3", "CANVAS", "Archive", [
        node("20:1", "INSTANCE", "old", componentId="1:1", visible=False),         # скрыт сам
        node("20:2", "INSTANCE", "Icon", componentId="99:1"),                       # библиотечный
        node("20:3", "INSTANCE", "Ghost", componentId="77:7"),                      # мастер неизвестен
        node("20:4", "COMPONENT", "Card"),                                          # второй «Card»
    ])
    return {
        "name": "Test file", "lastModified": "2026-09-01T00:00:00Z", "version": "1",
        "document": node("0:0", "DOCUMENT", "Document", [page1, page2, page3]),
        "components": {
            "1:1": {"key": "k11", "name": "Size=Large", "componentSetId": "1:0"},
            "1:2": {"key": "k12", "name": "Size=Small", "componentSetId": "1:0"},
            "2:0": {"key": "k20", "name": "Card"},
            "20:4": {"key": "k204", "name": "Card"},
            "99:1": {"key": "lib-icon", "name": "Icon", "remote": True},
            "99:2": {"key": "lib-icon", "name": "Icon", "remote": True},  # та же библиотечная, другая запись
        },
        "componentSets": {"1:0": {"key": "kset", "name": "Button"}},
        "styles": {"S:brand": {"key": "sb", "name": "Brand/Pink", "styleType": "FILL"}},
    }


def test_parse_file_key():
    assert parse_file_key("https://www.figma.com/design/AbC123xyz0/Name?node-id=1-2") == "AbC123xyz0"
    assert parse_file_key("figma.com/file/AbC123xyz0/Name") == "AbC123xyz0"
    assert parse_file_key("https://www.figma.com/design/AbC123xyz0/branch/BrAnCh9999/Name") == "BrAnCh9999"
    for bad in ["https://example.com/design/AbC123xyz0", "https://www.figma.com/files/recent", "hello"]:
        with pytest.raises(ConfigError):
            parse_file_key(bad)


# --- API ---


def test_settings_saved_from_web_ui(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("FIGMA_SEARCH_CONFIG", str(cfg))
    monkeypatch.delenv("FIGMA_FILE_URL", raising=False)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    c = TestClient(main.app)

    assert c.get("/api/status").json()["configured"] is False
    r = c.post("/api/settings", json={"token": "secret-token"})
    assert r.json()["configured"] and r.json()["has_token"] and "secret-token" not in r.text
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert "secret-token" not in c.get("/api/status").text


def test_parse_node_id():
    base = f"https://www.figma.com/design/{FILE_KEY}/T"
    assert parse_node_id(base) is None
    assert parse_node_id(base + "?node-id=10-0&t=abc") == "10:0"
    assert parse_node_id(base + "?node-id=0%3A2") == "0:2"
    with pytest.raises(ConfigError):
        parse_node_id(base + "?node-id=garbage")


URL = f"https://www.figma.com/design/{FILE_KEY}/Test"


def test_lifecycle_stops_only_when_page_closed(monkeypatch):
    from app import lifecycle
    lifecycle._pages.clear()
    lifecycle._state.update(started=1000.0, inflight=0, last_bye=0.0)
    assert not lifecycle.should_stop(1010)          # только что запустился — ждём страницу
    assert lifecycle.should_stop(1030)              # страница так и не открылась
    lifecycle.page_alive("p1")
    now = lifecycle._pages["p1"]
    assert not lifecycle.should_stop(now + 60)      # страница открыта (фоновая вкладка шлёт редко)
    assert lifecycle.should_stop(now + 120)         # сигналов нет дольше таймаута
    lifecycle.page_alive("p1")
    lifecycle.page_closed("p1")
    t = lifecycle._state["last_bye"]
    assert not lifecycle.should_stop(t + 2)         # вдруг это перезагрузка страницы
    assert lifecycle.should_stop(t + 10)
    lifecycle.request_started()
    assert not lifecycle.should_stop(t + 10)        # идёт поиск — не останавливаемся
    lifecycle.request_finished()


def test_heartbeat_and_bye_endpoints():
    from app import lifecycle
    lifecycle._pages.clear()
    client = TestClient(main.app)
    info = client.post("/api/heartbeat", json={"page": "tab-1"}).json()
    assert info["ok"] and info["pages_open"] == 1 and "uptime_s" in info
    client.post("/api/bye", content='{"page": "tab-1"}', headers={"Content-Type": "text/plain"})
    assert lifecycle.pages_open() == 0


def test_pages_and_archived_routes():
    c = TestClient(main.app)
    assert "Поиск по макетам" in c.get("/").text
    assert "Макеты Figma" in c.get("/settings").text
    r = c.get("/experiment", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    # функции, перенесённые в архив, больше не отвечают
    assert c.get("/plugin").status_code == 404
    assert c.post("/api/search", json={"name": "x"}).status_code in (404, 405)


def test_log_redaction_keeps_other_records():
    import logging
    from app.config import _RedactFilter
    f = _RedactFilter()
    f.secrets.add("figd_secret")
    rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s"', ("127.0.0.1", "GET", "/"), None)
    f.filter(rec)
    assert rec.args == ("127.0.0.1", "GET", "/")  # без токена запись не трогаем
    rec2 = logging.LogRecord("app", 20, "", 0, "token=%s", ("figd_secret",), None)
    f.filter(rec2)
    assert rec2.getMessage() == "token=***"


def test_saved_links(monkeypatch, tmp_path):
    monkeypatch.setenv("FIGMA_SEARCH_CONFIG", str(tmp_path / "config.json"))
    c = TestClient(main.app)
    link = f"https://www.figma.com/design/{FILE_KEY}/Dating-Sim?node-id=8385-17686&t=GDnC2Q91NtwQK-4"
    r = c.post("/api/links", json={"url": link}).json()
    assert r["added"] is True
    [item] = r["links"]
    assert item["title"] == "Dating Sim" and item["node_id"] == "8385:17686"
    assert item["url"] == f"https://www.figma.com/design/{FILE_KEY}/Dating-Sim?node-id=8385-17686"  # без t=…
    assert c.post("/api/links", json={"url": link.replace("&t=GDnC2Q91NtwQK-4", "")}).json()["added"] is False
    c.post("/api/links", json={"url": f"https://www.figma.com/design/{FILE_KEY}/Dating-Sim"})
    assert len(c.get("/api/links").json()["links"]) == 2
    assert c.post("/api/links", json={"url": "https://example.com/x"}).status_code == 400
    r = c.post("/api/links/delete", json={"url": link}).json()
    assert [l["scope"] for l in r["links"]] == ["весь файл"]


@pytest.fixture
def exp_client(monkeypatch, tmp_path):
    import json as _json
    from app import experiment
    monkeypatch.setenv("FIGMA_SEARCH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("FIGMA_TOKEN", "secret-token")
    monkeypatch.setattr(experiment, "DB_DIR", tmp_path / "db")
    monkeypatch.setattr(experiment, "DB_PATH", tmp_path / "db" / "figma.sqlite")
    full = make_file()
    full["document"]["children"][1]["name"] = "Stage 1"          # Screens → Stage 1
    full["document"]["children"][0]["name"] = "Local components"  # Components
    calls = []

    async def fake_download(path, token, params, progress=None):
        calls.append((path, params))
        if params == {"depth": 1}:
            doc = {**full["document"], "children": [{"id": p["id"], "name": p["name"], "type": "CANVAS"} for p in full["document"]["children"]]}
            body = {"name": "Test file", "document": doc}
        elif path.endswith("/nodes"):
            by_id = {p["id"]: p for p in full["document"]["children"]}
            by_id["10:0"] = full["document"]["children"][1]["children"][0]
            body = {"name": "Test file", "version": "7", "nodes": {
                i: {"document": by_id[i], "components": full["components"], "componentSets": full["componentSets"],
                    "styles": full["styles"]}
                for i in params["ids"].split(",")}}
        else:
            body = full
        raw = _json.dumps(body).encode()
        return raw, len(raw) // 3, 0.01
    monkeypatch.setattr(experiment, "_download", fake_download)
    c = TestClient(main.app)
    c.calls = calls
    return c


def test_experiment_whole_file_uses_page_rules(exp_client):
    assert [r["text"] for r in exp_client.get("/api/rules").json()["rules"]] == ["Stage", "Local components", "Flow"]
    r = exp_client.post("/api/exp/load", json={"file_url": URL}).json()
    assert r["status"] == "ok", r
    st = r["stats"]
    assert st["pages"]["loaded"] == ["Local components", "Stage 1"] and st["pages"]["skipped"] == ["Archive"]
    assert exp_client.calls[-1] == (f"/files/{FILE_KEY}/nodes", {"ids": "0:1,0:2"})
    # сохранено всё, кроме простых фигур; тексты и инстансы на месте, Archive не загружен
    assert st["instances"] == 6 and st["texts"] == 1 and st["nodes_stored"] == st["nodes_total"]
    assert st["db_bytes"] > 0 and st["json_bytes"] > 0 and st["stored_bytes"] > 0
    import sqlite3
    from app import experiment
    con = sqlite3.connect(experiment.DB_PATH)
    names = {row[0] for row in con.execute("SELECT name FROM nodes")}
    assert "Submit CTA" in names and "old" not in names  # «old» лежит на странице Archive
    assert exp_client.get("/api/exp/db").json()["loads"][0]["pages"]["loaded"] == ["Local components", "Stage 1"]


def test_experiment_node_link_and_rules_edit(exp_client):
    r = exp_client.post("/api/exp/load", json={"file_url": URL + "?node-id=10-0"}).json()
    assert r["status"] == "ok" and r["stats"]["scope_name"] == "Login" and r["stats"]["pages"] is None
    saved = exp_client.post("/api/rules", json={"rules": ["Flow", " flow ", "", {"text": "Nope", "exact": True}]}).json()["rules"]
    assert saved == [{"text": "Flow", "exact": False}, {"text": "Nope", "exact": True}]
    r = exp_client.post("/api/exp/load", json={"file_url": URL})
    assert r.status_code == 400 and r.json()["code"] == "no_pages" and "Archive" in r.json()["message"]


def test_compact_keeps_images_and_named_shapes():
    from app.experiment import compact
    root = node("0:0", "DOCUMENT", "Doc", [node("1:1", "FRAME", "F", [
        node("1:2", "RECTANGLE", "Rectangle 12"),                                   # имя по умолчанию — не храним
        node("1:6", "RECTANGLE", "Rectangle 3", fills=[{"type": "IMAGE"}]),         # картинка — храним
        node("1:7", "RECTANGLE", "Light"),                                          # своё название — храним
        node("1:3", "BOOLEAN_OPERATION", "Union", visible=False, children=[
            node("1:4", "VECTOR", "Vector"),
            node("1:8", "VECTOR", "Girls_Bg"),                                      # внутри пропущенного Union
        ]),
        node("1:5", "TEXT", "Title", characters="Привет"),
    ])])
    rows, seen, kept = compact(root)
    ids = [r[0] for r in rows]
    assert sum(seen.values()) == 9 and ids == ["0:0", "1:1", "1:6", "1:7", "1:8", "1:5"]
    by = {r[0]: r for r in rows}
    assert by["1:6"][11] == 1 and by["1:7"][11] == 0
    assert by["1:8"][1] == "1:1" and by["1:8"][4] == 0   # родитель — ближайший сохранённый; Union был скрыт
    assert by["1:5"][6] == "Привет" and kept["IMAGE"] == 1


def test_experiment_search(exp_client):
    exp_client.post("/api/exp/load", json={"file_url": URL})
    r = exp_client.post("/api/exp/search", json={"q": "button"}).json()  # регистр не важен
    assert [c["name"] for c in r["components"]] == ["Button"]
    ids = [i["id"] for i in r["instances"]["items"]]
    assert set(ids) == {"2:1", "10:1", "10:2", "10:4", "I10:5;2:1"}  # Archive не загружен
    renamed = next(i for i in r["instances"]["items"] if i["id"] == "10:2")
    assert renamed["name"] == "Submit CTA" and renamed["variant"] == "Size=Small" and renamed["page"] == "Stage 1"
    assert next(i for i in r["instances"]["items"] if i["id"] == "10:4")["hidden"] is True
    r = exp_client.post("/api/exp/search", json={"q": "Button", "exclude_hidden": True}).json()
    assert r["instances"]["total"] == 4 and r["hidden_skipped"] == 1
    assert exp_client.post("/api/exp/search", json={"q": "Butt"}).json()["similar"] == ["Button"]


def test_experiment_box_stored_in_tenths(exp_client, monkeypatch):
    from app import experiment
    import sqlite3
    root = node("0:0", "DOCUMENT", "Doc", [node("1:1", "FRAME", "F", [
        node("1:5", "TEXT", "Title", characters="Войти", absoluteBoundingBox={"x": -12.25, "y": 480, "width": 251.4375, "height": 56}),
    ], absoluteBoundingBox={"x": 0, "y": 0, "width": 375, "height": 812})])
    rows, _, _ = experiment.compact(root)
    assert rows[2][7:11] == (-122, 4800, 2514, 560)  # десятые доли пикселя
    assert experiment.box_px(-122, 4800, 2514, 560) == {"x": -12.2, "y": 480.0, "w": 251.4, "h": 56.0}
    assert experiment.box_px(None, None, None, None) is None
    # старая база без столбцов x/y/w/h обновляется сама
    experiment.DB_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(experiment.DB_PATH)
    con.execute("CREATE TABLE nodes (file_key TEXT, scope_id TEXT, id TEXT, parent_id TEXT, type TEXT, name TEXT, "
                "visible INTEGER, component_id TEXT, text TEXT, PRIMARY KEY (file_key, scope_id, id)) WITHOUT ROWID")
    con.close()
    cols = {r[1] for r in experiment._connect().execute("PRAGMA table_info(nodes)")}
    assert {"x", "y", "w", "h"} <= cols


def test_experiment_search_groups_layers(exp_client):
    exp_client.post("/api/exp/load", json={"file_url": URL})
    r = exp_client.post("/api/exp/search", json={"q": "login"}).json()  # фрейм Login, регистр не важен
    assert [(g["key"], g["total"]) for g in r["layers"]] == [("frames", 1)]
    assert r["layers"][0]["items"][0]["id"] == "10:0" and r["layers"][0]["items"][0]["exact"] is True
    r = exp_client.post("/api/exp/search", json={"q": "Button"}).json()
    keys = {g["key"]: g for g in r["layers"]}
    # мастер-набор найден по названию; инстансы компонента не дублируются в «названии слоя»,
    # а обычный фрейм «Button» (detached) попадает во «Фреймы»
    assert keys["masters"]["items"][0]["id"] == "1:0"
    assert [i["id"] for i in keys["frames"]["items"]] == ["10:6"]
    assert "named_instances" not in keys
    r = exp_client.post("/api/exp/search", json={"q": "Card"}).json()
    assert {g["key"] for g in r["layers"]} == {"masters"} and r["instances"]["total"] == 1


def test_settings_db_load_status_and_update_check(exp_client, monkeypatch):
    import time as _t
    from app import database
    database._jobs.clear()
    database._updates.clear()
    c = exp_client
    c.__enter__()  # один цикл событий: фоновая загрузка живёт между запросами
    try:
        c.post("/api/links", json={"url": URL})
        st = c.get("/api/db/status").json()
        key = f"{FILE_KEY}|"
        assert st["items"][key]["loaded"] is None and st["items"][key]["job"] is None

        assert c.post("/api/db/load", json={"url": URL}).json()["job"]["state"] in ("queued", "running")
        for _ in range(100):
            job = c.get("/api/db/status").json()["items"][key]["job"]
            if job["state"] in ("done", "error"):
                break
            _t.sleep(0.02)
        assert job["state"] == "done", job
        loaded = c.get("/api/db/status").json()["items"][key]["loaded"]
        assert loaded["version"] == "7" and loaded["nodes"] > 0 and loaded["pages"] == ["Local components", "Stage 1"]

        async def same(client, token, fk):
            return {"version": "7", "modified": "2026-09-28T10:00:00Z"}
        monkeypatch.setattr(database, "_remote_version", same)
        assert c.post("/api/db/check").json()["checked"] == 1
        assert c.get("/api/db/status").json()["items"][key]["update"]["available"] is False

        async def newer(client, token, fk):
            return {"version": "8", "modified": "2026-09-28T12:00:00Z"}
        monkeypatch.setattr(database, "_remote_version", newer)
        c.post("/api/db/check")
        upd = c.get("/api/db/status").json()["items"][key]["update"]
        assert upd["available"] is True and upd["remote_modified"] == "2026-09-28T12:00:00Z"
    finally:
        c.__exit__(None, None, None)


def test_page_rules_exact_and_contains():
    from app.links import page_matches
    rules = [{"text": "Stage", "exact": False}, {"text": "Flow", "exact": True}]
    assert page_matches("Stage 1 -> Core gameplay", rules)
    assert page_matches(" flow ", rules)                    # точное, но без учёта регистра и пробелов по краям
    assert not page_matches("User flow", rules)             # «Flow» только целиком
    assert not page_matches("Flow old", rules)
    assert not page_matches("Archive", rules)


def test_delete_link_removes_its_data_from_db(exp_client):
    import sqlite3
    from app import database, experiment
    database._jobs.clear()
    c = exp_client
    c.post("/api/links", json={"url": URL})
    c.post("/api/links", json={"url": URL + "?node-id=10-0"})
    c.post("/api/exp/load", json={"file_url": URL})
    c.post("/api/exp/load", json={"file_url": URL + "?node-id=10-0"})
    key = f"{FILE_KEY}|"
    assert c.get("/api/db/status").json()["items"][key]["loaded"] is not None

    r = c.post("/api/db/delete", json={"url": URL}).json()
    assert r["status"] == "ok" and r["removed_nodes"] > 0 and len(r["links"]) == 1
    con = sqlite3.connect(experiment.DB_PATH)
    for table in ("nodes", "components", "files", "loads"):
        assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE file_key=? AND scope_id=''", (FILE_KEY,)).fetchone()[0] == 0
    # данные по другой ссылке на тот же файл (фрейм) не тронуты
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE scope_id='10:0'").fetchone()[0] > 0
    con.close()
    # вставили ссылку снова — строка чистая, «Не загружено»
    c.post("/api/links", json={"url": URL})
    assert c.get("/api/db/status").json()["items"][key]["loaded"] is None
    # удалить, пока идёт загрузка, нельзя
    database._jobs[key] = {"state": "running"}
    assert c.post("/api/db/delete", json={"url": URL}).status_code == 409
    database._jobs.clear()


def test_orphan_data_listed_and_deleted(exp_client):
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})          # в базе есть, а ссылки в списке нет
    st = c.get("/api/db/status").json()
    assert [o["file_key"] for o in st["orphans"]] == [FILE_KEY]
    c.post("/api/links", json={"url": URL})
    assert c.post("/api/db/delete-orphan", json={"file_key": FILE_KEY}).status_code == 400  # ссылка есть — нельзя
    c.post("/api/links/delete", json={"url": URL})
    r = c.post("/api/db/delete-orphan", json={"file_key": FILE_KEY}).json()
    assert r["removed_nodes"] > 0 and c.get("/api/db/status").json()["orphans"] == []


def test_pages_are_not_cached():
    c = TestClient(main.app)
    for path in ("/", "/settings", "/api/links"):
        assert c.get(path).headers["cache-control"] == "no-cache"


def test_search_split_by_file_tabs(exp_client):
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})                    # весь файл по правилам
    c.post("/api/exp/load", json={"file_url": URL + "?node-id=10-0"})  # отдельно фрейм Login
    r = c.post("/api/exp/search", json={"q": "Button"}).json()
    counts = {f["key"]: f["total"] for f in r["by_file"]}
    assert set(counts) == {f"{FILE_KEY}|", f"{FILE_KEY}|10:0"} and all(v > 0 for v in counts.values())
    only = c.post("/api/exp/search", json={"q": "Button", "file": f"{FILE_KEY}|10:0"}).json()
    ids = [i["id"] for i in only["instances"]["items"]]
    assert ids and all(i["file_id"] == f"{FILE_KEY}|10:0" for i in only["instances"]["items"])
    assert "2:1" not in ids                                          # инстанс со страницы Local components — в другом «макете»
    assert {f["key"]: f["total"] for f in only["by_file"]} == counts  # подписи вкладок не зависят от выбранной


def test_search_pagination(exp_client):
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    r = c.post("/api/exp/search", json={"q": "Button", "page_size": 10}).json()
    inst = r["instances"]
    assert inst["total"] == 5 and inst["page"] == 1 and inst["pages"] == 1 and len(inst["items"]) == 5
    # страница за пределами — отдаём последнюю, а не пустоту
    r = c.post("/api/exp/search", json={"q": "Button", "page_size": 10, "pages": {"instances": 7}}).json()
    assert r["instances"]["page"] == 1 and r["page_size"] == 10
    from app.experiment import _page
    items = [{"file_key": FILE_KEY, "id": f"1:{n}", "_fv": {}, "g": "x", "f": f"{FILE_KEY}|"} for n in range(23)]
    block = _page({"total": 23, "items": items}, {"x": 3}, 10, "x")
    assert block["page"] == 3 and block["pages"] == 3 and [i["id"] for i in block["items"]] == ["1:20", "1:21", "1:22"]
    assert block["items"][0]["url"].endswith("node-id=1-20") and "_fv" not in block["items"][0]  # служебное не отдаём


def test_quick_filters():
    from app.experiment import _facets, _passes, _variant_props
    assert _variant_props("Size=RB 56px, Color=Trans Black") == {"Size": "RB 56px", "Color": "Trans Black"}
    items = [
        {"page": "Stage 1", "variant": "Size=L, Color=B2"},
        {"page": "Stage 1", "variant": "Size=S, Color=Pink"},
        {"page": "Stage 2", "variant": "Size=L, Color=Pink"},
        {"page": "Stage 10", "variant": None},
    ]
    f = {x["key"]: x for x in _facets(items, {})}
    assert [v["value"] for v in f["page"]["values"]] == ["Stage 1", "Stage 2", "Stage 10"]   # натуральный порядок
    assert {v["value"]: v["count"] for v in f["prop:Color"]["values"]} == {"B2": 1, "Pink": 2}
    flt = {"prop:Color": ["B2", "Pink"], "page": ["Stage 1"]}                              # или внутри, и между
    assert [i["variant"] for i in items if _passes(i, flt)] == ["Size=L, Color=B2", "Size=S, Color=Pink"]
    f = {x["key"]: x for x in _facets(items, {"page": ["Stage 2"]})}
    # счётчик страницы не зависит от выбранной страницы, а у цвета — учитывает её
    assert {v["value"]: v["count"] for v in f["page"]["values"]}["Stage 1"] == 2
    assert {v["value"]: v["count"] for v in f["prop:Color"]["values"]} == {"B2": 0, "Pink": 1}


def test_search_with_filters(exp_client):
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    r = c.post("/api/exp/search", json={"q": "Button"}).json()
    keys = {f["key"] for f in r["facets"]}
    assert {"page", "prop:Size"} <= keys
    r = c.post("/api/exp/search", json={"q": "Button", "filters": {"prop:Size": ["Large"]}}).json()
    assert r["instances"]["total"] == 2 and all(i["variant"] == "Size=Large" for i in r["instances"]["items"])
    assert r["filters"] == {"prop:Size": ["Large"]}


def test_export_group_all_pages(exp_client):
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    r = c.post("/api/exp/export", json={"q": "Button", "group": "instances"}).json()
    assert r["total"] == 5 and len(r["items"]) == 5          # все, не только первая страница
    it = r["items"][0]
    assert set(it) == {"id", "name", "file", "page", "variant", "text", "hidden", "url"}
    assert it["url"].startswith("https://www.figma.com/design/")
    r = c.post("/api/exp/export", json={"q": "Button", "group": "instances", "filters": {"prop:Size": ["Large"]}}).json()
    assert r["total"] == 2
    assert c.post("/api/exp/export", json={"q": "Button", "group": "nope"}).status_code == 404



def test_find_compact_and_node_details(exp_client):
    """Новый поиск: всё найденное одним ответом + подробности одного объекта по запросу."""
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    r = c.post("/api/exp/find", json={"q": "button"}).json()
    S, groups = r["s"], [g["key"] for g in r["groups"]]
    rows = [dict(zip(["g", "f", "id", "name", "page", "variant", "hidden", "exact", "text", "type"], x)) for x in r["items"]]
    inst = [x for x in rows if groups[x["g"]] == "instances"]
    assert len(inst) == 5 and {x["id"] for x in inst} == {"2:1", "10:1", "10:2", "10:4", "I10:5;2:1"}
    hidden = {x["id"]: bool(x["hidden"]) for x in inst}
    assert hidden["10:4"] is True and hidden["10:1"] is False          # скрыт через родителя
    renamed = next(x for x in inst if x["id"] == "10:2")
    assert S[renamed["name"]] == "Submit CTA" and S[renamed["page"]] == "Stage 1" and S[renamed["variant"]] == "Size=Small"
    d = c.post("/api/exp/node", json={"file": r["files"][0]["id"], "id": "I10:5;2:1"}).json()
    assert d["path"] == ["Login", "Card"] and d["parent_instance"]["name"] == "Card"


def test_backfill_old_rows(exp_client):
    """Макет, загруженный до появления полей page_id/hid/pinst, досчитывается один раз."""
    import sqlite3
    from app import experiment
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    con = sqlite3.connect(experiment.DB_PATH)
    con.execute("UPDATE nodes SET page_id=NULL, hid=NULL, pinst=NULL")
    con.commit()
    experiment._backfill_done["ok"] = False
    experiment.ensure_backfilled()
    got = {r[0]: r[1:] for r in con.execute("SELECT id, page_id, hid, pinst FROM nodes")}
    con.close()
    assert got["10:4"] == ("0:2", 1, None)        # страница Stage 1 (0:2), скрыт через группу
    assert got["I10:5;2:1"] == ("0:2", 0, "10:5")  # внутри инстанса Card


def test_find_by_size(exp_client):
    """Размеры: с названием уточняют поиск, без названия — все объекты такого размера."""
    import sqlite3
    from app import experiment
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    con = sqlite3.connect(experiment.DB_PATH)
    con.execute("UPDATE nodes SET w=560, h=560 WHERE id IN ('10:1', '10:4', '10:0')")   # 56 × 56 px
    con.execute("UPDATE nodes SET w=560, h=480 WHERE id = '10:2'")                      # 56 × 48 px
    con.commit(); con.close()

    def ids(**body):
        r = c.post("/api/exp/find", json=body).json()
        return sorted(x[2] for x in r["items"]), r
    got, _ = ids(q="Button", w=56, h=56)
    assert got == ["10:1", "10:4"]                             # только инстансы Button размером 56×56
    got, _ = ids(q="Button", w=56)
    assert got == ["10:1", "10:2", "10:4"]                     # только ширина
    got, r = ids(w=56, h=56)                                   # без названия — всё 56×56
    assert got == ["10:0", "10:1", "10:4"]
    groups = [g["key"] for g in r["groups"]]
    by_id = {x[2]: x for x in r["items"]}
    assert groups[by_id["10:1"][0]] == "instances_any" and r["s"][by_id["10:1"][10]] == "Button"   # компонент
    assert groups[by_id["10:0"][0]] == "frames"
    assert c.post("/api/exp/find", json={"q": ""}).status_code == 400


def test_colors_stored_and_searched(exp_client):
    """Цвета: заливка (у текста — цвет текста), обводка и источник — стиль, переменная или вручную."""
    import sqlite3
    from app import experiment
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    con = sqlite3.connect(experiment.DB_PATH)
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT id, fill, fill_a, fill_src, stroke, stroke_a, stroke_src FROM nodes WHERE fill IS NOT NULL")}
    assert con.execute("SELECT colors FROM files").fetchone()[0] >= 1
    con.close()
    assert got["10:6"] == ("FF3366", 50, "s:Brand/Pink", "0000FF", None, None)   # верхняя заливка, стиль
    assert got["10:1"] == ("FF3366", None, "v:", None, None, None)              # переменная
    assert got["10:7"] == ("111111", None, None, None, None, None)              # вручную, скрытая не в счёт

    def ids(**body):
        r = c.post("/api/exp/find", json=body).json()
        return sorted(x[2] for x in r["items"]), r
    got, r = ids(color="#ff3366")                 # без названия — все объекты этого цвета
    assert got == ["10:1", "10:6"] and r["color"] == "FF3366"
    row = next(x for x in r["items"] if x[2] == "10:6")
    assert r["s"][row[11]] == "FF3366" and row[12] == 50 and r["s"][row[13]] == "s:Brand/Pink"
    assert ids(color="00f")[0] == ["10:6"]       # обводка тоже считается
    assert ids(q="Button", color="111111")[0] == ["10:7"]   # с названием — уточняет
    assert r["files"][0]["colors"] is True
    # с отклонением: #FF3366 и почти такой же #FF4477 (≈8 %) — да при 10 %, нет при 5 %; #111111 (≈5 % от чёрного)
    assert ids(color="FF4477", tol=10)[0] == ["10:1", "10:6"]
    assert ids(color="FF4477", tol=5)[0] == []
    assert ids(color="000000", tol=6)[0] == ["10:7"]
    assert c.post("/api/exp/find", json={"color": "000", "tol": 120}).status_code == 400
    # прозрачность цвета: у 10:6 заливка #FF3366 при 50 %, у 10:1 — 100 %
    assert ids(color="FF3366", alpha=50)[0] == ["10:6"]
    assert ids(color="FF3366", alpha=52)[0] == ["10:6"]      # ±2 %
    assert ids(color="FF3366", alpha=100)[0] == ["10:1"]
    assert ids(color="FF3366", alpha=70)[0] == []
    bad = c.post("/api/exp/find", json={"color": "red"})
    assert bad.status_code == 400 and bad.json()["code"] == "bad_color"


def test_load_all_queues_every_link(exp_client):
    c = exp_client
    c.__enter__()  # один event loop: фоновая загрузка успевает выполниться между запросами
    c.post("/api/links", json={"url": URL})
    r = c.post("/api/db/load-all").json()
    assert r == {"status": "ok", "queued": 1, "skipped": 0}
    # уже загруженный в новом формате — не перезагружается
    import time
    for _ in range(50):
        if c.get("/api/db/status").json()["items"][f"{FILE_KEY}|"]["job"]["state"] == "done":
            break
        time.sleep(0.05)
    assert c.post("/api/db/load-all").json() == {"status": "ok", "queued": 0, "skipped": 1}
    c.__exit__(None, None, None)


def test_extras_stored_and_shown(exp_client):
    """Шрифт, стиль текста, картинка, скругление, эффекты, auto layout, прозрачность, Dev-статус,
    изменённый инстанс и связи прототипа сохраняются и видны в подробностях объекта."""
    import sqlite3
    from app import experiment
    styles = {"S:t": {"name": "Body/M"}, "S:e": {"name": "Shadow/L"}}
    root = node("0:0", "DOCUMENT", "Doc", [node("0:1", "CANVAS", "Stage", [
        node("1:1", "TEXT", "Title", characters="Hi", styles={"text": "S:t"},
             style={"fontFamily": "Inter", "fontStyle": "Medium", "fontWeight": 500, "fontSize": 14, "lineHeightPx": 20.0, "letterSpacing": 0}),
        node("1:2", "FRAME", "Card", cornerRadius=12, opacity=0.5, layoutMode="VERTICAL", itemSpacing=8,
             paddingTop=16, paddingLeft=16, paddingRight=16, paddingBottom=16, devStatus={"type": "READY_FOR_DEV"},
             effects=[{"type": "DROP_SHADOW", "offset": {"x": 0, "y": 4}, "radius": 12, "color": {"r": 0, "g": 0, "b": 0, "a": 0.25}}],
             interactions=[{"actions": [{"type": "NODE", "destinationId": "5:5"}]}]),
        node("1:3", "RECTANGLE", "Banner", rectangleCornerRadii=[8, 8, 0, 0], styles={"effect": "S:e"},
             effects=[{"type": "DROP_SHADOW"}], fills=[{"type": "IMAGE", "imageRef": "abc123"}]),
        node("1:4", "INSTANCE", "Button", componentId="9:9", overrides=[{"id": "1:4", "overriddenFields": ["characters"]}]),
    ])])
    rows, _, _ = experiment.compact(root, styles)
    ex = {r[0]: dict(zip(experiment.EXTRA_COLS, r[21:])) for r in rows}
    assert ex["1:1"]["font"] == "Inter;Medium;500;14;20;0" and ex["1:1"]["tstyle"] == "Body/M"
    assert ex["1:2"]["radius"] == "12" and ex["1:2"]["opacity"] == 50 and ex["1:2"]["dev"] == "R"
    assert ex["1:2"]["layout"] == "V;8;16,16,16,16;;MIN/MIN" and ex["1:2"]["link"] == "5:5"
    assert ex["1:2"]["effect"] == "DROP_SHADOW;0;4;12;;000000:25"
    assert ex["1:3"]["radius"] == "8,8,0,0" and ex["1:3"]["effect"] == "s:Shadow/L" and ex["1:3"]["img"] == "abc123"
    assert ex["1:4"]["ovr"] == 1 and ex["1:1"]["ovr"] is None

    # через загрузку: значения в словаре vals, в подробностях — уже строки
    c = exp_client
    c.post("/api/exp/load", json={"file_url": URL})
    con = sqlite3.connect(experiment.DB_PATH)
    assert con.execute("SELECT colors FROM files").fetchone()[0] == experiment.DATA_FORMAT
    con.close()
    d = c.post("/api/exp/node", json={"file": f"{FILE_KEY}|", "id": "10:7"}).json()
    assert d["status"] == "ok" and "extra" in d


def test_stop_cancels_queue(exp_client, monkeypatch):
    """«Остановить»: текущая загрузка прерывается, очередь снимается, в базе — прежние данные."""
    import asyncio
    import time
    from app import database, experiment

    async def slow_load(url, progress=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(experiment, "run_load", slow_load)
    c = exp_client
    c.__enter__()  # один event loop на все запросы — задачи загрузки живут между ними
    try:
        c.post("/api/links", json={"url": URL})
        c.post("/api/links", json={"url": URL + "?node-id=10-0"})
        assert c.post("/api/db/load-all").json()["queued"] == 2
        time.sleep(0.2)
        states = sorted(i["job"]["state"] for i in c.get("/api/db/status").json()["items"].values())
        assert states == ["queued", "running"]
        assert c.post("/api/db/stop").json()["stopped"] == 2
        time.sleep(0.2)
        states = [i["job"]["state"] for i in c.get("/api/db/status").json()["items"].values()]
        assert states == ["stopped", "stopped"] and not database._tasks
        # после остановки можно запустить снова
        assert c.post("/api/db/load-all").json()["queued"] == 2
        c.post("/api/db/stop")
    finally:
        c.__exit__(None, None, None)
