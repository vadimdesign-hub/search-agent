// Вариант 2 Search Agent: поиск инстансов прямо в открытом файле, без скачивания через REST API.
// Только чтение: плагин ничего не меняет в макете (при «Выделить» — только страница, выделение и вид).

figma.showUI(__html__, { width: 320, height: 170, themeColors: true });
// Искать и в скрытых слоях внутри инстансов.
figma.skipInvisibleInstanceChildren = false;

function pageOf(node) {
  let p = node;
  while (p && p.type !== "PAGE") p = p.parent;
  return p;
}

// Путь, видимость, ближайший родитель-инстанс и мастер-компонент, внутри которого лежит узел.
function describe(node) {
  const path = [];
  let hidden = node.visible === false;
  let parentInstance = null;
  let insideMaster = null;
  for (let p = node.parent; p && p.type !== "PAGE" && p.type !== "DOCUMENT"; p = p.parent) {
    path.unshift(p.name);
    if (p.visible === false) hidden = true;
    if (!parentInstance && p.type === "INSTANCE") parentInstance = { id: p.id, name: p.name };
    if (p.type === "COMPONENT" || p.type === "COMPONENT_SET") insideMaster = p.name;  // остаётся самый внешний
  }
  const page = pageOf(node);
  return { page: page ? page.name : "", path, hidden, parentInstance, insideMaster };
}

function fail(code, message) {
  return { ok: false, message, data: { code } };
}

async function search({ fileKey, nodeId, name, candidateId, excludeHidden }) {
  const t0 = Date.now();
  if (fileKey && figma.fileKey && figma.fileKey !== fileKey) {
    return fail("other_file", `В Figma открыт другой файл («${figma.root.name}»). Откройте файл из ссылки и запустите плагин в нём.`);
  }

  // 1. Область поиска: элемент из ссылки или весь файл.
  let roots;
  let scope = null;
  if (nodeId) {
    const node = await figma.getNodeByIdAsync(nodeId);
    if (!node) return fail("node_not_found", `Элемент из ссылки (node-id ${nodeId}) не найден в открытом файле.`);
    if (node.type === "DOCUMENT") {
      await figma.loadAllPagesAsync();
      roots = figma.root.children;
    } else {
      if (node.type === "PAGE") await node.loadAsync();
      roots = [node];
      const page = pageOf(node);
      scope = { node_id: node.id, type: node.type === "PAGE" ? "CANVAS" : node.type, name: node.name, page: page ? page.name : "" };
    }
  } else {
    await figma.loadAllPagesAsync();
    roots = figma.root.children;
  }
  const t1 = Date.now();

  // 2. Все инстансы в области (включая вложенные в другие инстансы и скрытые).
  const instances = [];
  const masters = [];
  for (const r of roots) {
    if (r.type === "INSTANCE") instances.push(r);
    if ((r.type === "COMPONENT" || r.type === "COMPONENT_SET") && r.name === name) masters.push(r);
    if ("findAllWithCriteria" in r) {
      for (const n of r.findAllWithCriteria({ types: ["INSTANCE"] })) instances.push(n);
      for (const n of r.findAllWithCriteria({ types: ["COMPONENT", "COMPONENT_SET"] })) if (n.name === name) masters.push(n);
    }
  }

  // 3. Кандидаты: мастер-компоненты и наборы вариантов с таким именем.
  const cands = new Map();
  const candOf = (kind, node) => {
    const id = `${kind}:${node.key || node.id}`;
    if (!cands.has(id)) {
      const local = !node.remote && pageOf(node);
      const d = local ? describe(node) : null;
      cands.set(id, {
        id, kind, name, remote: !!node.remote, description: node.description || "",
        node_id: local ? node.id : null, page: d ? d.page : null, path: d ? d.path : [],
        variants: new Set(), instances: [],
      });
      if (kind === "component_set" && local) for (const c of node.children) if (c.type === "COMPONENT") cands.get(id).variants.add(c.id);
    }
    return cands.get(id);
  };
  for (const m of masters) candOf(m.type === "COMPONENT_SET" ? "component_set" : "component", m);

  // Мастер каждого инстанса — связь через mainComponent, имя слоя не важно (переименованные тоже).
  const BATCH = 400;
  for (let i = 0; i < instances.length; i += BATCH) {
    const part = instances.slice(i, i + BATCH);
    const mains = await Promise.all(part.map((n) => n.getMainComponentAsync().catch(() => null)));
    part.forEach((inst, k) => {
      const main = mains[k];
      if (!main) return;
      const set = main.parent && main.parent.type === "COMPONENT_SET" ? main.parent : null;
      if (set && set.name === name) {
        const c = candOf("component_set", set);
        c.variants.add(main.id);
        c.instances.push({ inst, variant: main.name });
      }
      if (main.name === name) candOf("component", main).instances.push({ inst, variant: null });
    });
  }
  const t2 = Date.now();

  const list = [...cands.values()].sort((a, b) =>
    (a.kind !== "component_set") - (b.kind !== "component_set") || a.remote - b.remote || String(a.page).localeCompare(String(b.page)));
  const toCand = (c) => ({
    id: c.id, kind: c.kind, name: c.name, remote: c.remote, description: c.description,
    variant_count: c.kind === "component_set" ? c.variants.size : null,
    page: c.page, path: c.path, node_id: c.node_id,
  });
  const base = {
    file_name: figma.root.name, scope,
    timing: { load_ms: t1 - t0, search_ms: t2 - t1, instances_scanned: instances.length },
  };
  const where = scope ? "в области поиска" : "в файле";

  if (!list.length) {
    return { ok: true, data: { ...base, status: "not_found",
      message: `Компонент «${name}» не найден ${where}: нет ни мастер-компонента, ни его инстансов.` } };
  }
  let chosen;
  if (candidateId) {
    chosen = list.find((c) => c.id === candidateId);
    if (!chosen) return fail("stale_choice", "Выбранный компонент больше не найден — запустите поиск заново.");
  } else if (list.length > 1) {
    return { ok: true, data: { ...base, status: "ambiguous",
      message: `Найдено несколько разных компонентов с названием «${name}». Выберите нужный.`,
      candidates: list.map(toCand) } };
  } else {
    chosen = list[0];
  }

  let found = chosen.instances.map(({ inst, variant }) => {
    const d = describe(inst);
    return {
      id: inst.id, name: inst.name, page: d.page, path: d.path, hidden: d.hidden,
      renamed: inst.name !== name && inst.name !== variant, variant,
      parent_instance: d.parentInstance, inside_master: d.insideMaster,
    };
  });
  let hiddenSkipped = 0;
  if (excludeHidden) {
    const visible = found.filter((f) => !f.hidden);
    hiddenSkipped = found.length - visible.length;
    found = visible;
  }
  return { ok: true, data: { ...base, status: "ok", component: toCand(chosen),
    count: found.length, hidden_skipped: hiddenSkipped, instances: found } };
}

// --- Выделение (как в варианте 1) ---
const GUARD_MS = 15000;
let guard = null;

const sameIds = (nodes) => nodes.map((n) => n.id).sort().join(",");
function isAncestor(node, target) {
  for (let p = target.parent; p; p = p.parent) if (p.id === node.id) return true;
  return false;
}
async function applyGuard() {
  if (!guard) return;
  if (figma.currentPage !== guard.page) await figma.setCurrentPageAsync(guard.page);
  guard.page.selection = guard.nodes;
  figma.viewport.scrollAndZoomIntoView(guard.nodes);
}
// После перехода по ссылке Figma может выделить родительский фрейм — возвращаем выделение.
figma.on("selectionchange", () => {
  if (!guard || Date.now() > guard.until) { guard = null; return; }
  if (figma.currentPage !== guard.page) return;
  const sel = figma.currentPage.selection;
  if (sameIds(sel) === guard.ids) return;
  const overridden = sel.length === 0 || sel.every((n) => guard.nodes.some((t) => isAncestor(n, t)));
  if (overridden) applyGuard(); else guard = null;
});
figma.on("currentpagechange", () => {
  if (guard && Date.now() <= guard.until && figma.currentPage !== guard.page) applyGuard();
});

async function select({ nodeIds }) {
  const nodes = [];
  for (const id of nodeIds) {
    const n = await figma.getNodeByIdAsync(id);
    if (n && n.type !== "DOCUMENT" && n.type !== "PAGE") nodes.push(n);
  }
  if (!nodes.length) return { ok: false, message: "Объекты не найдены в этом файле. Откройте в Figma файл, по которому искали." };
  const page = pageOf(nodes[0]);
  const onPage = nodes.filter((n) => pageOf(n) === page);
  guard = { page, nodes: onPage, ids: sameIds(onPage), until: Date.now() + GUARD_MS };
  await applyGuard();
  const other = nodes.length - onPage.length;
  let message = `Выделено: ${onPage.length} на странице «${page.name}»`;
  if (other) message += ` · ещё ${other} на других страницах`;
  return { ok: true, message };
}

figma.ui.onmessage = async (msg) => {
  if (msg.type !== "task") return;
  let result;
  try {
    result = msg.task.type === "search" ? await search(msg.task.payload) : await select(msg.task.payload);
  } catch (e) {
    result = { ok: false, message: "Ошибка в плагине: " + (e && e.message ? e.message : String(e)) };
  }
  figma.ui.postMessage({ type: "result", seq: msg.task.seq, kind: msg.task.type, ...result });
};
