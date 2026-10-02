import httpx
import pytest
from fastapi.testclient import TestClient

from app import figma_client, main
from app.config import ConfigError, parse_file_key, parse_node_id
from app.figma_client import FigmaError, fetch_file
from app.search import build_index, find_candidates, find_instances

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
            node("10:1", "INSTANCE", "Button", componentId="1:1"),
            node("10:2", "INSTANCE", "Submit CTA", componentId="1:2"),              # переименован
            node("10:3", "GROUP", "Hidden group", visible=False, children=[
                node("10:4", "INSTANCE", "Button", componentId="1:1"),              # скрыт через родителя
            ]),
            node("10:5", "INSTANCE", "Card", componentId="2:0", children=[
                node("I10:5;2:1", "INSTANCE", "Button", componentId="1:2"),         # вложен в инстанс
            ]),
            node("10:6", "FRAME", "Button"),                                        # detached / обычный слой
            node("10:7", "TEXT", "Button"),
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
    }


@pytest.fixture
def index():
    return build_index(FILE_KEY, make_file())


def test_component_set_finds_all_variants_everywhere(index):
    [cand] = find_candidates(index, "  Button  ")
    assert cand.kind == "component_set" and cand.component_ids == {"1:1", "1:2"}
    ids = [i.id for i in find_instances(index, cand)]
    assert ids == ["2:1", "10:1", "10:2", "10:4", "I10:5;2:1", "20:1"]
    by_id = {i.id: i for i in find_instances(index, cand)}
    assert by_id["10:2"].name == "Submit CTA"
    assert by_id["10:4"].hidden and by_id["20:1"].hidden and not by_id["10:1"].hidden
    assert by_id["I10:5;2:1"].parent_instance_id == "10:5"
    assert by_id["2:1"].inside_master == "Card"
    assert by_id["10:4"].page == "Screens" and by_id["10:4"].path == ["Login", "Hidden group"]


def test_single_variant_by_full_name(index):
    [cand] = find_candidates(index, "Size=Large")
    assert [i.id for i in find_instances(index, cand)] == ["10:1", "10:4", "20:1"]


def test_ambiguous_names(index):
    cands = find_candidates(index, "Card")
    assert len(cands) == 2
    counts = sorted(len(find_instances(index, c)) for c in cands)
    assert counts == [0, 1]


def test_remote_component_merged_by_key(index):
    [cand] = find_candidates(index, "Icon")
    assert cand.remote and cand.component_ids == {"99:1", "99:2"}
    assert [i.id for i in find_instances(index, cand)] == ["20:2"]


def test_case_sensitive_exact_match(index):
    assert find_candidates(index, "button") == []
    assert find_candidates(index, "Butt") == []


def test_parse_file_key():
    assert parse_file_key("https://www.figma.com/design/AbC123xyz0/Name?node-id=1-2") == "AbC123xyz0"
    assert parse_file_key("figma.com/file/AbC123xyz0/Name") == "AbC123xyz0"
    assert parse_file_key("https://www.figma.com/design/AbC123xyz0/branch/BrAnCh9999/Name") == "BrAnCh9999"
    for bad in ["https://example.com/design/AbC123xyz0", "https://www.figma.com/files/recent", "hello"]:
        with pytest.raises(ConfigError):
            parse_file_key(bad)


@pytest.mark.parametrize("status,code", [
    (403, "forbidden"), (404, "not_found_file"), (429, "rate_limited"), (500, "figma_unavailable"), (400, "bad_request"),
])
async def test_http_errors(status, code):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, headers={"Retry-After": "30"}, json={}))
    with pytest.raises(FigmaError) as e:
        await fetch_file(FILE_KEY, "secret-token", transport=transport)
    assert e.value.code == code and "secret-token" not in e.value.message
    if status == 429:
        assert "30" in e.value.message


async def test_network_error_and_incomplete_response():
    def boom(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(FigmaError) as e:
        await fetch_file(FILE_KEY, "t", transport=httpx.MockTransport(boom))
    assert e.value.code == "network"

    with pytest.raises(FigmaError) as e:
        await fetch_file(FILE_KEY, "t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "x"})))
    assert e.value.code == "incomplete"


async def test_sends_token_header():
    seen = {}
    def handler(request):
        seen["token"] = request.headers.get("X-Figma-Token")
        seen["path"] = request.url.path
        return httpx.Response(200, json=make_file())
    await fetch_file(FILE_KEY, "tok", transport=httpx.MockTransport(handler))
    assert seen == {"token": "tok", "path": f"/v1/files/{FILE_KEY}"}


# --- API ---

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("FIGMA_SEARCH_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("FIGMA_FILE_URL", f"https://www.figma.com/design/{FILE_KEY}/Test")
    monkeypatch.setenv("FIGMA_TOKEN", "secret-token")
    main._cache.clear()
    calls = {"n": 0}

    async def fake_fetch(key, token, transport=None):
        calls["n"] += 1
        return make_file()
    monkeypatch.setattr(figma_client, "fetch_file", fake_fetch)
    c = TestClient(main.app)
    c.calls = calls
    return c


def test_api_ok(client):
    r = client.post("/api/search", json={"name": " Button "}).json()
    assert r["status"] == "ok" and r["count"] == 6
    first = r["instances"][0]
    assert first["url"] == f"https://www.figma.com/design/{FILE_KEY}/?node-id=2-1"
    nested = next(i for i in r["instances"] if i["id"] == "I10:5;2:1")
    assert nested["url"].endswith("node-id=I10-5%3B2-1") and nested["parent_instance"]["name"] == "Card"
    renamed = next(i for i in r["instances"] if i["id"] == "10:2")
    assert renamed["renamed"] and renamed["variant"] == "Size=Small"
    assert "secret-token" not in str(r)


def test_api_not_found_vs_no_instances(client):
    assert client.post("/api/search", json={"name": "Nope"}).json()["status"] == "not_found"
    r = client.post("/api/search", json={"name": "Card"}).json()
    assert r["status"] == "ambiguous" and len(r["candidates"]) == 2
    empty = next(c for c in r["candidates"] if c["node_id"] == "20:4")
    r2 = client.post("/api/search", json={"name": "Card", "candidate_id": empty["id"]}).json()
    assert r2["status"] == "ok" and r2["count"] == 0
    assert client.calls["n"] == 1  # выбор кандидата использует кэш


def test_api_unresolved_library(client):
    r = client.post("/api/search", json={"name": "Ghost"}).json()
    assert r["status"] == "unresolved"


def test_api_figma_error(client, monkeypatch):
    async def fail(key, token, transport=None):
        raise FigmaError("rate_limited", "Превышен лимит", 429)
    monkeypatch.setattr(figma_client, "fetch_file", fail)
    r = client.post("/api/search", json={"name": "Button"})
    assert r.status_code == 429 and r.json()["status"] == "error" and "instances" not in r.json()


def test_api_missing_config(client, monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN")
    r = client.post("/api/search", json={"name": "Button"})
    assert r.status_code == 400 and r.json()["code"] == "config"


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


def nodes_transport(seen):
    """Фейковый Figma API: /nodes отдаёт поддерево, depth=2 — список страниц."""
    full = make_file()
    by_id = {}
    stack = [full["document"]]
    while stack:
        n = stack.pop()
        by_id[n["id"]] = n
        stack.extend(n.get("children", []))

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/nodes"):
            nid = request.url.params["ids"]
            entry = None
            if nid in by_id:
                # как в Figma: только компоненты, определённые или использованные в поддереве
                used, st = set(), [by_id[nid]]
                while st:
                    n = st.pop()
                    if n["type"] == "COMPONENT":
                        used.add(n["id"])
                    if n["type"] == "INSTANCE":
                        used.add(n["componentId"])
                    st.extend(n.get("children", []))
                comps = {k: v for k, v in full["components"].items() if k in used}
                sets = {k: v for k, v in full["componentSets"].items()
                        if any(c.get("componentSetId") == k for c in comps.values())}
                entry = {"document": by_id[nid], "components": comps, "componentSets": sets}
            return httpx.Response(200, json={"name": "Test file", "nodes": {nid: entry}})
        if request.url.params.get("depth") == "2":
            pages = [{**p, "children": [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in p["children"]]}
                     for p in full["document"]["children"]]
            return httpx.Response(200, json={"name": "Test file", "document": {**full["document"], "children": pages}})
        return httpx.Response(200, json=full)
    return httpx.MockTransport(handler)


@pytest.fixture
def scoped_client(monkeypatch, tmp_path):
    monkeypatch.setenv("FIGMA_SEARCH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("FIGMA_FILE_URL", raising=False)
    monkeypatch.setenv("FIGMA_TOKEN", "secret-token")
    main._cache.clear()
    seen = []
    transport = nodes_transport(seen)
    real_file, real_node = figma_client.fetch_file, figma_client.fetch_node
    monkeypatch.setattr(figma_client, "fetch_file", lambda k, t: real_file(k, t, transport=transport))
    monkeypatch.setattr(figma_client, "fetch_node", lambda k, t, n: real_node(k, t, n, transport=transport))
    c = TestClient(main.app)
    c.seen = seen
    return c


URL = f"https://www.figma.com/design/{FILE_KEY}/Test"


@pytest.mark.parametrize("link,label,ids", [
    (URL, "весь файл", ["2:1", "10:1", "10:2", "10:4", "I10:5;2:1", "20:1"]),
    (URL + "?node-id=0-3", "страница «Archive»", ["20:1"]),
    (URL + "?node-id=10-0", "фрейм «Login» на странице «Screens»", ["10:1", "10:2", "10:4", "I10:5;2:1"]),
    (URL + "?node-id=10-3", "группа «Hidden group»", ["10:4"]),
])
def test_scope_follows_link(scoped_client, link, label, ids):
    r = scoped_client.post("/api/search", json={"name": "Button", "file_url": link}).json()
    assert r["status"] == "ok", r
    assert r["file"]["scope_label"] == label
    assert [i["id"] for i in r["instances"]] == ids
    paths = [p for p, _ in scoped_client.seen]
    if "node-id" in link:
        assert any(p.endswith("/nodes") for p in paths)
        assert all(q.get("depth") == "2" or p.endswith("/nodes") for p, q in scoped_client.seen)  # весь файл не грузим


def test_section_scope_and_page_path(scoped_client, monkeypatch):
    # секция на странице: путь начинается со страницы, хотя грузится только секция
    r = scoped_client.post("/api/search", json={"name": "Button", "file_url": URL + "?node-id=10-0"}).json()
    login = next(i for i in r["instances"] if i["id"] == "10:4")
    assert login["page"] == "Screens" and login["path"] == ["Login", "Hidden group"]


def test_scope_not_found_and_missing_node(scoped_client):
    r = scoped_client.post("/api/search", json={"name": "Card", "file_url": URL + "?node-id=0-3"}).json()
    assert r["status"] == "ok" and r["count"] == 0  # мастер «Card» лежит в Archive, инстансов там нет
    r = scoped_client.post("/api/search", json={"name": "Nope", "file_url": URL + "?node-id=0-3"}).json()
    assert r["status"] == "not_found" and "страница «Archive»" in r["message"]
    r = scoped_client.post("/api/search", json={"name": "Button", "file_url": URL + "?node-id=999-1"})
    assert r.status_code == 404 and r.json()["code"] == "node_not_found"


def test_last_link_is_remembered(scoped_client):
    scoped_client.post("/api/search", json={"name": "Button", "file_url": URL + "?node-id=0-3"})
    assert scoped_client.get("/api/status").json()["file_url"].endswith("node-id=0-3")
    r = scoped_client.post("/api/search", json={"name": "Button"}).json()
    assert r["file"]["scope_label"] == "страница «Archive»"


def test_timing_reported(scoped_client):
    r = scoped_client.post("/api/search", json={"name": "Button", "file_url": URL + "?node-id=10-0"}).json()
    t = r["timing"]
    assert t["from_cache"] is False and t["load_s"] >= 0 and t["search_s"] >= 0 and t["instances_scanned"] == 5
    r2 = scoped_client.post("/api/search", json={"name": "Nope", "file_url": URL + "?node-id=10-0"}).json()
    assert r2["status"] == "not_found" and r2["timing"]["from_cache"] is True


def test_exclude_hidden(client):
    r = client.post("/api/search", json={"name": "Button", "exclude_hidden": True}).json()
    ids = [i["id"] for i in r["instances"]]
    assert "10:4" not in ids and "20:1" not in ids  # скрыт через родителя и скрыт сам
    assert r["count"] == 4 and r["hidden_skipped"] == 2
    r = client.post("/api/search", json={"name": "Button"}).json()
    assert r["count"] == 6 and r["hidden_skipped"] == 0




def test_figma_plugin_bridge(client):
    main._bridge.update(seq=0, request=None, result=None, plugin_seen=0.0)
    r = client.post("/api/figma/select", json={"file_key": FILE_KEY, "node_ids": ["I10:5;2:1"]}).json()
    assert r["seq"] == 1 and r["plugin_connected"] is False
    poll = client.get("/api/figma/poll").json()
    assert poll["request"]["node_ids"] == ["I10:5;2:1"]
    assert client.get("/api/figma/status").json()["plugin_connected"] is True
    client.post("/api/figma/result", json={"seq": 0, "ok": True, "message": "старый"})  # чужой seq игнорируется
    assert client.get("/api/figma/status").json()["result"] is None
    client.post("/api/figma/result", json={"seq": 1, "ok": True, "message": "Выделено: 1"})
    assert client.get("/api/figma/status").json()["result"]["message"] == "Выделено: 1"


def test_variant2_plugin_search(monkeypatch):
    """Вариант 2: сервер ставит задачу, «плагин» (поток) забирает её и отвечает."""
    import threading
    from app import plugin_bridge
    plugin_bridge._tasks.clear()
    plugin_bridge._state.update(seq=0, plugin_seen=0.0)
    monkeypatch.setattr(plugin_bridge, "SEARCH_TIMEOUT_SECONDS", 5)
    # Один цикл событий на все запросы — как на настоящем сервере.
    c = TestClient(main.app)
    c.__enter__()

    r = c.post("/api/p2/search", json={"name": "Button", "file_url": URL})
    assert r.status_code == 409 and r.json()["code"] == "plugin_offline"

    seen = {}

    def fake_plugin():
        after = c.get("/api/p2/poll").json()["seq"]  # первый опрос: плагин «подключился»
        for _ in range(200):
            tasks = c.get(f"/api/p2/poll?after={after}").json()["tasks"]
            if tasks:
                t = tasks[0]
                seen.update(t["payload"])
                c.post("/api/p2/result", json={"seq": t["seq"], "ok": True, "data": {
                    "status": "ok", "file_name": "Test file",
                    "scope": {"node_id": "10:0", "type": "FRAME", "name": "Login", "page": "Screens"},
                    "component": {"id": "component_set:kset", "kind": "component_set", "name": "Button",
                                  "remote": False, "variant_count": 2, "node_id": "1:0", "page": "Components", "path": []},
                    "count": 1, "hidden_skipped": 0,
                    "instances": [{"id": "I10:5;2:1", "name": "Button", "page": "Screens", "path": ["Login", "Card"],
                                   "hidden": False, "renamed": False, "variant": "Size=Small",
                                   "parent_instance": {"id": "10:5", "name": "Card"}, "inside_master": None}],
                    "timing": {"load_ms": 20, "search_ms": 30, "instances_scanned": 5},
                }})
                return
            time.sleep(0.02)

    import time
    th = threading.Thread(target=fake_plugin)
    th.start()
    time.sleep(0.1)  # плагин успел отметиться
    r = c.post("/api/p2/search", json={"name": " Button ", "file_url": URL + "?node-id=10-0", "exclude_hidden": True}).json()
    th.join(5)
    assert seen == {"fileKey": FILE_KEY, "nodeId": "10:0", "name": "Button", "candidateId": None, "excludeHidden": True}
    assert r["status"] == "ok" and r["count"] == 1
    assert r["file"]["scope_label"] == "фрейм «Login» на странице «Screens»"
    assert r["instances"][0]["url"].endswith("node-id=I10-5%3B2-1")
    assert r["instances"][0]["parent_instance"]["url"].endswith("node-id=10-5")
    assert r["timing"]["mode"] == "plugin" and r["timing"]["load_s"] == 0.02 and r["timing"]["search_s"] == 0.03
    c.__exit__(None, None, None)


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


def test_heartbeat_and_bye_endpoints(client):
    from app import lifecycle
    lifecycle._pages.clear()
    client.post("/api/heartbeat", json={"page": "tab-1"})
    assert lifecycle.pages_open() == 1
    assert client.get("/api/p2/poll").json()["page_open"] is True
    client.post("/api/bye", content='{"page": "tab-1"}', headers={"Content-Type": "text/plain"})
    assert lifecycle.pages_open() == 0


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
                i: {"document": by_id[i], "components": full["components"], "componentSets": full["componentSets"]}
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
