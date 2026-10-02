"""Индексация ответа GET /v1/files и поиск инстансов мастер-компонента.

Связь инстанса с мастер-компонентом определяется только через `componentId`,
поэтому имя слоя инстанса значения не имеет (переименованные находятся),
обычные слои и отсоединённые (detached) экземпляры — это не INSTANCE и в
результаты не попадают.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote


@dataclass
class InstanceInfo:
    id: str
    name: str
    component_id: str
    page: str
    path: list[str]            # имена контейнеров между страницей и инстансом
    hidden: bool               # скрыт сам слой или кто-то из родителей
    parent_instance_id: str | None
    parent_instance_name: str | None
    inside_master: str | None  # имя мастер-компонента, внутри которого лежит инстанс


@dataclass
class LocalNode:
    id: str
    type: str
    name: str
    page: str
    path: list[str]
    child_component_ids: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    cid: str
    kind: str  # "component_set" | "component"
    name: str
    component_ids: set[str]
    remote: bool
    description: str = ""
    node_id: str | None = None
    page: str | None = None
    path: list[str] = field(default_factory=list)


@dataclass
class FileIndex:
    file_key: str
    file_name: str
    last_modified: str
    version: str
    components: dict[str, dict]
    component_sets: dict[str, dict]
    local_nodes: dict[str, LocalNode]
    instances: list[InstanceInfo]
    scope: dict | None = None  # None — весь файл, иначе узел из ссылки


def build_index(file_key: str, data: dict) -> FileIndex:
    local_nodes: dict[str, LocalNode] = {}
    instances: list[InstanceInfo] = []

    # Итеративный обход: деревья бывают очень глубокими.
    # Элемент стека: (узел, страница, путь, скрыт, (id, имя) ближайшего инстанса, имя мастера)
    stack: list[tuple] = []
    for page in reversed(data["document"].get("children", [])):
        stack.append((page, None, [], False, None, None))

    while stack:
        node, page, path, hidden, parent_inst, inside_master = stack.pop()
        ntype = node.get("type")
        nid = node.get("id", "")
        name = node.get("name", "")

        if ntype == "CANVAS":
            page_name, child_path, child_hidden = name, [], False
        else:
            page_name = page
            is_hidden = hidden or node.get("visible", True) is False
            if ntype == "INSTANCE":
                instances.append(InstanceInfo(
                    id=nid,
                    name=name,
                    component_id=node.get("componentId", ""),
                    page=page_name,
                    path=list(path),
                    hidden=is_hidden,
                    parent_instance_id=parent_inst[0] if parent_inst else None,
                    parent_instance_name=parent_inst[1] if parent_inst else None,
                    inside_master=inside_master,
                ))
                parent_inst = (nid, name)
            elif ntype in ("COMPONENT", "COMPONENT_SET"):
                ln = LocalNode(nid, ntype, name, page_name, list(path))
                if ntype == "COMPONENT_SET":
                    ln.child_component_ids = [c["id"] for c in node.get("children", []) if c.get("type") == "COMPONENT"]
                local_nodes[nid] = ln
                if ntype == "COMPONENT_SET":
                    inside_master = inside_master or name
                elif inside_master is None:
                    inside_master = name
            child_path, child_hidden = path + [name], is_hidden

        for child in reversed(node.get("children", []) or []):
            stack.append((child, page_name, child_path, child_hidden, parent_inst, inside_master))

    return FileIndex(
        file_key=file_key,
        file_name=data.get("name", ""),
        last_modified=data.get("lastModified", ""),
        version=str(data.get("version", "")),
        components=data.get("components") or {},
        component_sets=data.get("componentSets") or {},
        local_nodes=local_nodes,
        instances=instances,
        scope=data.get("scope"),
    )


def find_candidates(index: FileIndex, raw_name: str) -> list[Candidate]:
    """Мастер-компоненты и наборы вариантов с точным именем (пробелы по краям игнорируются).

    Записи с одинаковым `key` — это один и тот же компонент (например, разные
    версии библиотечного компонента), они объединяются в одного кандидата.
    """
    name = raw_name.strip()
    if not name:
        return []
    groups: dict[str, Candidate] = {}

    def add(kind: str, node_id: str, meta: dict, member_ids: set[str]) -> None:
        gkey = f"{kind}:{meta.get('key') or node_id}"
        cand = groups.get(gkey)
        if cand is None:
            cand = groups[gkey] = Candidate(
                cid=gkey, kind=kind, name=name, component_ids=set(),
                remote=bool(meta.get("remote")), description=meta.get("description") or "",
            )
        cand.component_ids |= member_ids
        local = index.local_nodes.get(node_id)
        if local and cand.node_id is None:
            cand.node_id, cand.page, cand.path = local.id, local.page, local.path
            cand.remote = False

    # Наборы вариантов: инстансы всех вариантов набора.
    set_members: dict[str, set[str]] = {}
    for cid, meta in index.components.items():
        if meta.get("componentSetId"):
            set_members.setdefault(meta["componentSetId"], set()).add(cid)
    set_ids = {sid for sid, meta in index.component_sets.items() if meta.get("name") == name}
    set_ids |= {n.id for n in index.local_nodes.values() if n.type == "COMPONENT_SET" and n.name == name}
    for sid in set_ids:
        members = set(set_members.get(sid, set()))
        if sid in index.local_nodes:
            members |= set(index.local_nodes[sid].child_component_ids)
        add("component_set", sid, index.component_sets.get(sid, {}), members)

    # Отдельные компоненты (в т.ч. вариант, если ввели его полное имя).
    comp_ids = {cid for cid, meta in index.components.items() if meta.get("name") == name}
    comp_ids |= {n.id for n in index.local_nodes.values() if n.type == "COMPONENT" and n.name == name}
    for cid in comp_ids:
        add("component", cid, index.components.get(cid, {}), {cid})

    return sorted(groups.values(), key=lambda c: (c.kind != "component_set", c.remote, c.page or "", c.cid))


def find_instances(index: FileIndex, cand: Candidate) -> list[InstanceInfo]:
    return [i for i in index.instances if i.component_id in cand.component_ids]


def unresolved_instances_named(index: FileIndex, raw_name: str) -> list[InstanceInfo]:
    """Инстансы с таким именем слоя, чей мастер-компонент отсутствует в ответе API."""
    name = raw_name.strip()
    known = index.components.keys() | index.local_nodes.keys()
    return [i for i in index.instances if i.name == name and i.component_id not in known]


def node_url(file_key: str, node_id: str) -> str:
    return f"https://www.figma.com/design/{file_key}/?node-id={quote(node_id.replace(':', '-'), safe='-')}"


def candidate_to_dict(index: FileIndex, cand: Candidate) -> dict:
    return {
        "id": cand.cid,
        "kind": cand.kind,
        "name": cand.name,
        "remote": cand.remote,
        "description": cand.description,
        "variant_count": len(cand.component_ids) if cand.kind == "component_set" else None,
        "page": cand.page,
        "path": cand.path,
        "node_id": cand.node_id,
        "url": node_url(index.file_key, cand.node_id) if cand.node_id else None,
    }


def instance_to_dict(index: FileIndex, cand: Candidate, inst: InstanceInfo) -> dict:
    variant = None
    if cand.kind == "component_set":
        variant = (index.components.get(inst.component_id) or {}).get("name") or (
            index.local_nodes[inst.component_id].name if inst.component_id in index.local_nodes else None
        )
    return {
        "id": inst.id,
        "name": inst.name,
        "page": inst.page,
        "path": inst.path,
        "hidden": inst.hidden,
        "renamed": inst.name != cand.name and inst.name != variant,
        "variant": variant,
        "parent_instance": (
            {"id": inst.parent_instance_id, "name": inst.parent_instance_name,
             "url": node_url(index.file_key, inst.parent_instance_id)}
            if inst.parent_instance_id else None
        ),
        "inside_master": inst.inside_master,
        "url": node_url(index.file_key, inst.id),
    }
