// 教我说话 · 说话卡面板
// 通过 AstrBot 插件视图 bridge 与插件后端通信（window.AstrBotPluginView）。

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  status: "active",
  keyword: "",
  items: [],
  editing: null,
  pendingDelete: 0,
  pendingTimer: null,
};

const listEl = $("list");
const statsEl = $("stats");
const toastEl = $("toast");

/* ------------------------------------------------------------ 工具函数 */
function toast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    toastEl.className = "toast" + (isError ? " error" : "");
  }, 2400);
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function parseTags(raw) {
  return String(raw || "")
    .replace(/，/g, ",")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/* ------------------------------------------------------------ 数据 */
async function load() {
  listEl.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await bridge.apiGet("cards", {
      status: state.status,
      q: state.keyword,
      limit: 300,
    });
    state.items = (data && data.items) || [];
    renderStats((data && data.stats) || {});
    renderList();
  } catch (error) {
    listEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "empty";
    box.textContent = "读取失败：" + (error && error.message ? error.message : error);
    listEl.appendChild(box);
    toast("读取失败", true);
  }
}

function renderStats(stats) {
  const chips = [
    ["生效中", stats.active ?? 0],
    ["已归档", stats.archived ?? 0],
    ["注入总次数", stats.uses ?? 0],
    ["平均权重", stats.avg_weight ?? 0],
    ["反馈 👍/👎", `${stats.feedback_up ?? 0}/${stats.feedback_down ?? 0}`],
  ];
  statsEl.innerHTML = "";
  for (const [label, value] of chips) {
    const chip = document.createElement("div");
    chip.className = "chip";
    const b = document.createElement("b");
    b.textContent = String(value);
    const s = document.createElement("span");
    s.textContent = label;
    chip.append(b, s);
    statsEl.appendChild(chip);
  }
}

/* ------------------------------------------------------------ 渲染 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function renderList() {
  listEl.innerHTML = "";
  if (!state.items.length) {
    const empty = el("div", "empty");
    if (state.keyword) {
      empty.textContent = "没搜到匹配的说话卡。";
    } else if (state.status === "archived") {
      empty.textContent = "还没有归档的卡。";
    } else {
      empty.append(
        document.createTextNode("还没有说话卡。私聊里教它一句，例如："),
      );
      const code = el("code", "", "这样不对，AI 味太重了，应该说……");
      empty.append(code, document.createElement("br"));
      empty.append(document.createTextNode("也可以点右上角「＋ 新建卡片」手动写一张。"));
    }
    listEl.appendChild(empty);
    return;
  }
  for (const card of state.items) listEl.appendChild(renderCard(card));
}

function renderCard(card) {
  const box = el("div", "card" + (card.status === "archived" ? " archived" : ""));
  box.dataset.id = String(card.id);

  const head = el("div", "card-head");
  head.append(el("span", "card-id", `#${card.id}`));
  const scene = el("div", "scene" + (card.scene ? "" : " muted"), card.scene || "通用场景");
  head.append(scene);
  if (card.status === "archived") head.append(el("span", "tag", "已归档"));
  box.append(head);

  if (card.bad) {
    const line = el("div", "line bad");
    line.append(el("span", "mark", "✗"));
    line.append(el("span", "val", card.bad));
    box.append(line);
  }
  if (card.good) {
    const line = el("div", "line good");
    line.append(el("span", "mark", "✓"));
    line.append(el("span", "val", card.good));
    box.append(line);
  }
  if (card.principle) box.append(el("div", "principle", "要点：" + card.principle));

  const foot = el("div", "card-foot");
  const metaBits = [
    `权重 ${Number(card.weight || 0).toFixed(1)}`,
    `注入 ${card.use_count || 0} 次`,
    card.review_reps ? `复习 ${card.review_reps} 次` : "未复习",
    fmtTime(card.created_at),
  ];
  foot.append(el("div", "meta", metaBits.join(" · ")));
  for (const tag of (card.tags || []).slice(0, 3)) foot.append(el("span", "tag", tag));

  const editBtn = el("button", "act", "编辑");
  editBtn.addEventListener("click", () => openEditor(card));
  foot.append(editBtn);

  const toggleBtn = el(
    "button",
    "act",
    card.status === "archived" ? "恢复" : "归档",
  );
  toggleBtn.addEventListener("click", async () => {
    const action = card.status === "archived" ? "restore" : "archive";
    await callAction(card.id, action, card.status === "archived" ? "已恢复" : "已归档");
  });
  foot.append(toggleBtn);

  const delBtn = el("button", "act danger", "删除");
  delBtn.addEventListener("click", async () => {
    if (state.pendingDelete !== card.id) {
      state.pendingDelete = card.id;
      delBtn.textContent = "确认删除";
      delBtn.classList.add("confirm");
      clearTimeout(state.pendingTimer);
      state.pendingTimer = setTimeout(() => {
        if (state.pendingDelete === card.id) {
          state.pendingDelete = 0;
          delBtn.textContent = "删除";
          delBtn.classList.remove("confirm");
        }
      }, 4000);
      return;
    }
    state.pendingDelete = 0;
    delBtn.textContent = "删除";
    delBtn.classList.remove("confirm");
    await callAction(card.id, "delete", "已删除");
  });
  foot.append(delBtn);

  box.append(foot);
  return box;
}

async function callAction(id, action, okText) {
  try {
    await bridge.apiPost(`cards/${id}/${action}`, {});
    toast(okText);
    await load();
  } catch (error) {
    toast((error && error.message) || "操作失败", true);
  }
}

/* ------------------------------------------------------------ 编辑器 */
function openEditor(card) {
  state.editing = card || null;
  $("dlg-title").textContent = card ? `编辑说话卡 #${card.id}` : "新建说话卡";
  $("f-scene").value = card ? card.scene || "" : "";
  $("f-bad").value = card ? card.bad || "" : "";
  $("f-good").value = card ? card.good || "" : "";
  $("f-principle").value = card ? card.principle || "" : "";
  $("f-tags").value = card ? (card.tags || []).join(",") : "";
  $("f-weight").value = card ? Number(card.weight || 1.5).toFixed(1) : "1.5";
  $("dlg-err").textContent = "";
  $("mask").classList.remove("hidden");
  setTimeout(() => $("f-scene").focus(), 30);
}

function closeEditor() {
  $("mask").classList.add("hidden");
  state.editing = null;
}

async function save() {
  const payload = {
    scene: $("f-scene").value.trim(),
    bad: $("f-bad").value.trim(),
    good: $("f-good").value.trim(),
    principle: $("f-principle").value.trim(),
    tags: parseTags($("f-tags").value),
    weight: Number($("f-weight").value || 1.5),
  };
  if (!payload.bad && !payload.good) {
    $("dlg-err").textContent = "反例和正例至少要填一个。";
    return;
  }
  if (state.editing) {
    payload.id = state.editing.id;
    payload.status = state.editing.status;
  }
  $("dlg-save").disabled = true;
  try {
    const result = await bridge.apiPost("cards/save", payload);
    closeEditor();
    toast(result && result.created === false ? "已更新" : "已保存");
    await load();
  } catch (error) {
    $("dlg-err").textContent =
      (error && error.message) || "保存失败，请检查内容后重试。";
  } finally {
    $("dlg-save").disabled = false;
  }
}

/* ------------------------------------------------------------ 事件绑定 */
async function main() {
  if (!bridge) {
    listEl.innerHTML = '<div class="empty">没有检测到 AstrBot 插件视图 bridge。</div>';
    return;
  }
  try {
    await bridge.ready();
  } catch {
    /* 拿不到上下文也能用，忽略 */
  }

  $("new-btn").addEventListener("click", () => openEditor(null));
  $("dlg-close").addEventListener("click", closeEditor);
  $("dlg-cancel").addEventListener("click", closeEditor);
  $("dlg-save").addEventListener("click", save);
  $("mask").addEventListener("click", (event) => {
    if (event.target === $("mask")) closeEditor();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("mask").classList.contains("hidden")) closeEditor();
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      if (!$("mask").classList.contains("hidden")) save();
    }
  });

  let searchTimer = null;
  $("q").addEventListener("input", (event) => {
    state.keyword = event.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, 220);
  });

  $("seg").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-status]");
    if (!btn || btn.classList.contains("on")) return;
    for (const b of $("seg").querySelectorAll("button")) b.classList.remove("on");
    btn.classList.add("on");
    state.status = btn.dataset.status;
    load();
  });

  await load();
}

main();
