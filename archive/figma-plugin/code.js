// Выделяет в открытом файле объекты, найденные на странице Search Agent.
// Сам ничего не меняет в макете: только переключает страницу, выделение и вид.

figma.showUI(__html__, { width: 300, height: 150, themeColors: true });

const GUARD_MS = 15000;  // сколько после запроса удерживать выделение на нужном объекте
let guard = null;        // { page, nodes, ids, until }

function sameIds(nodes) {
  return nodes.map((n) => n.id).sort().join(",");
}

// true, если node — один из родителей target (так Figma выделяет фрейм при переходе по ссылке).
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

// Ссылка из браузера доходит до Figma с задержкой, и после перехода Figma выделяет
// родительский фрейм. Пока действует guard, возвращаем выделение на вложенный объект.
// Если пользователь сам выделил что-то постороннее — перестаём вмешиваться.
figma.on("selectionchange", () => {
  if (!guard || Date.now() > guard.until) { guard = null; return; }
  if (figma.currentPage !== guard.page) return;  // переключение страницы обработает currentpagechange
  const sel = figma.currentPage.selection;
  if (sameIds(sel) === guard.ids) return;
  const overridden = sel.length === 0 || sel.every((n) => guard.nodes.some((t) => isAncestor(n, t)));
  if (overridden) applyGuard();
  else guard = null;
});

figma.on("currentpagechange", () => {
  if (guard && Date.now() <= guard.until && figma.currentPage !== guard.page) applyGuard();
});

function pageOf(node) {
  let p = node;
  while (p && p.type !== "PAGE") p = p.parent;
  return p;
}

async function select(nodeIds) {
  const nodes = [];
  for (const id of nodeIds) {
    const n = await figma.getNodeByIdAsync(id);
    if (n && n.type !== "DOCUMENT" && n.type !== "PAGE") nodes.push(n);
  }
  if (!nodes.length) {
    return { ok: false, message: "Объекты не найдены в этом файле. Откройте в Figma файл, по которому искали." };
  }
  // Выделение в Figma — в пределах одной страницы: берём страницу первого объекта.
  const page = pageOf(nodes[0]);
  const onPage = nodes.filter((n) => pageOf(n) === page);
  guard = { page, nodes: onPage, ids: sameIds(onPage), until: Date.now() + GUARD_MS };
  await applyGuard();

  const other = nodes.length - onPage.length;
  const missing = nodeIds.length - nodes.length;
  let message = `Выделено: ${onPage.length} на странице «${page.name}»`;
  if (other) message += ` · ещё ${other} на других страницах`;
  if (missing) message += ` · не найдено: ${missing}`;
  return { ok: true, message };
}

figma.ui.onmessage = async (msg) => {
  if (msg.type !== "select") return;
  let result;
  try {
    result = await select(msg.nodeIds);
  } catch (e) {
    result = { ok: false, message: "Не удалось выделить: " + (e && e.message ? e.message : String(e)) };
  }
  figma.notify(result.message, { error: !result.ok });
  figma.ui.postMessage({ type: "result", seq: msg.seq, ...result });
};
