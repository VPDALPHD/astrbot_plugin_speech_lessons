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

/* ------------------------------------------------------------ 设置面板 */
const cfgState = { fields: [], values: {}, providers: [], chatProviders: [] };

function embProviderSelect(currentProvider) {
  const select = document.createElement("select");
  select.className = "emb-select";
  select.dataset.role = "emb-provider";
  const add = (value, text) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    select.append(option);
  };
  add("", "手动填写（独立 API / 自定义模型名）");
  for (const prov of cfgState.providers) add(prov.id, prov.label);
  select.value = cfgState.providers.some((p) => p.id === currentProvider)
    ? currentProvider
    : "";
  select.addEventListener("change", () => {
    const pid = select.value;
    const provField = document.querySelector('[data-key="emb_provider_id"]');
    const dimField = document.querySelector('[data-key="emb_dim"]');
    const baseField = document.querySelector('[data-key="emb_api_base"]');
    const keyField = document.querySelector('[data-key="emb_api_key"]');
    const modelField = document.querySelector('[data-key="emb_model"]');
    const prov = cfgState.providers.find((p) => p.id === pid);
    if (pid) {
      if (provField) provField.value = pid;
      if (dimField && prov && prov.dim) dimField.value = String(prov.dim);
      // Base/Key 留空 → 仍走 AstrBot 接口；只把模型名填上做展示，避免看起来"没填"
      if (baseField) baseField.value = "";
      if (keyField) keyField.value = "";
      if (modelField) modelField.value = (prov && prov.model) || "";
    } else if (provField) {
      provField.value = "";
    }
    if (modelField) modelField.disabled = Boolean(pid);
  });
  return select;
}

// 纯勾选开关（没有下拉/角色）—— 这类统一排到每栏最后。
function isToggleField(field) {
  return field.type === "bool" && !field.role && !Array.isArray(field.options);
}

function cfgField(field, value) {
  const label = el("label", "field");
  const title = el("span", "f-title", field.label || field.key);
  if (field.hint) title.append(el("em", "", field.hint));
  // 开关行自己拼标题，避免 title 和开关挤成两行
  if (!isToggleField(field)) label.append(title);

  const options = Array.isArray(field.options) ? field.options : null;
  // 长字段（Base URL、QQ 号等）独占一整行，别挤在半格里
  if (field.wide && !isToggleField(field)) label.classList.add("wide-field");

  // ① 显式下拉（带 options）
  if (options) {
    const select = document.createElement("select");
    select.className = "emb-select";
    select.dataset.key = field.key;
    select.dataset.type = field.type === "bool" ? "bool" : "text";
    for (const opt of options) {
      const option = document.createElement("option");
      option.value = String(opt.value === undefined ? "" : opt.value);
      option.textContent = String(opt.label === undefined ? opt.value : opt.label);
      select.append(option);
    }
    select.value = String(value === undefined || value === null ? "" : value);
    select.addEventListener("change", syncConditionals);
    label.append(select);
    return label;
  }

  // ② 平台向量模型下拉（列表为空也照样渲染，给占位项）
  if (field.role === "embedding-provider") {
    const select = document.createElement("select");
    select.className = "emb-select";
    select.dataset.key = field.key;
    select.dataset.type = "text";
    const addOption = (optionValue, text) => {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = text;
      select.append(option);
    };
    addOption("", "不选（使用下面手动填写的 API）");
    if (!cfgState.providers.length) {
      addOption("__none__", "平台没有已配置的 embedding 模型");
    }
    for (const prov of cfgState.providers) addOption(prov.id, prov.label);
    const current = String(value || "");
    select.value = cfgState.providers.some((p) => p.id === current) ? current : "";
    select.addEventListener("change", () => {
      const prov = cfgState.providers.find((p) => p.id === select.value);
      const dimField = document.querySelector('[data-key="emb_dim"]');
      if (dimField && prov && prov.dim) dimField.value = String(prov.dim);
      syncConditionals();
    });
    label.append(select);
    return label;
  }

  // ③ 提炼模型下拉（同样空列表也渲染）
  if (field.role === "chat-provider") {
    const select = document.createElement("select");
    select.className = "emb-select";
    select.dataset.key = field.key;
    select.dataset.type = "text";
    const addOption = (optionValue, text) => {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = text;
      select.append(option);
    };
    addOption("", "不选（使用下面手动填写的 API）");
    if (!cfgState.chatProviders.length) {
      addOption("__none__", "平台没有已配置的对话模型");
    }
    for (const prov of cfgState.chatProviders) addOption(prov.id, prov.label);
    const current = String(value || "");
    select.value = cfgState.chatProviders.some((p) => p.id === current) ? current : "";
    select.addEventListener("change", syncConditionals);
    label.append(select);
    return label;
  }

  // ④ 布尔 → 滑动开关（标签在左、开关在右）
  if (field.type === "bool") {
    label.classList.add("switch");
    const text = el("span", "switch-text");
    text.append(el("span", "switch-name", field.label || field.key));
    if (field.hint) text.append(el("em", "", field.hint));
    const ctl = el("span", "ctl");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(value);
    box.dataset.key = field.key;
    box.dataset.type = "bool";
    const track = el("span", "track");
    ctl.append(box, track);
    label.append(text, ctl);
    return label;
  }

  // ⑤ 普通输入
  const input = document.createElement("input");
  input.dataset.key = field.key;
  input.dataset.type = field.type || "text";
  if (field.type === "password") {
    input.type = "password";
    input.autocomplete = "new-password";
  } else if (field.type === "int" || field.type === "float") {
    input.type = "number";
    input.step = field.type === "float" ? "0.01" : "1";
    if (field.min !== undefined) input.min = String(field.min);
    if (field.max !== undefined) input.max = String(field.max);
  } else {
    input.type = "text";
  }
  if (field.placeholder) input.placeholder = field.placeholder;
  input.value = value === undefined || value === null ? "" : String(value);
  if (field.wide) input.classList.add("wide");
  label.append(input);
  return label;
}

function syncConditionals() {
  for (const node of $("cfg-body").querySelectorAll("[data-show-key]")) {
    const key = node.dataset.showKey;
    const want = node.dataset.showVal;
    const control = $("cfg-body").querySelector(`[data-key="${key}"]`);
    let current = control ? String(control.value ?? "") : "";
    if (current === "__none__") current = "";
    node.classList.toggle("hidden", current !== want);
  }
}

function renderConfig(payload) {
  cfgState.fields = payload.fields || [];
  cfgState.values = payload.values || {};
  cfgState.providers = payload.providers || [];
  cfgState.chatProviders = payload.chat_providers || [];
  const body = $("cfg-body");
  body.innerHTML = "";

  const emb = payload.embedding || {};
  const banner = el("div", "cfg-banner");
  const modeText = emb.mode === "api" ? "独立 API" : "AstrBot 自带接口";
  banner.append(el("b", "", `向量：${emb.enabled ? "已启用" : "已关闭"}`));
  banner.append(
    el(
      "span",
      "",
      ` · ${modeText} · ${emb.model || "未指定"} · 维度 ${emb.dim || "?"} · 已存向量 ${payload.vector_cards ?? 0} 张`,
    ),
  );
  if (cfgState.providers.length) {
    banner.append(
      el("span", "", ` · 平台已配置 ${cfgState.providers.length} 个 embedding 模型`),
    );
  }
  if (emb.last_error) banner.append(el("div", "cfg-warn", "上次错误：" + emb.last_error));
  body.append(banner);

  const groups = new Map();
  for (const field of cfgState.fields) {
    const key = field.group || "其他";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(field);
  }
  for (const [name, fields] of groups) {
    const section = el("section", "cfg-group");
    const head = el("div", "cfg-group-head");
    head.append(el("h3", "", name));
    const count = el("span", "cfg-group-count", `${fields.length} 项`);
    head.append(count);
    section.append(head);

    // 先选择/填写，再开关：纯勾选开关统一收进最后一组，不再和输入框交错
    const plains = fields.filter((field) => !isToggleField(field));
    const toggles = fields.filter((field) => isToggleField(field));

    const bindCondition = (node, field) => {
      if (field.show_when && field.show_when.key) {
        node.dataset.showKey = String(field.show_when.key);
        node.dataset.showVal = String(field.show_when.value ?? "");
      }
      return node;
    };

    if (plains.length) {
      const grid = el("div", "cfg-grid");
      for (const field of plains) {
        grid.append(bindCondition(cfgField(field, cfgState.values[field.key]), field));
      }
      section.append(grid);
    }
    if (toggles.length) {
      const list = el("div", "cfg-switches");
      for (const field of toggles) {
        list.append(bindCondition(cfgField(field, cfgState.values[field.key]), field));
      }
      section.append(list);
    }

    if (name === "向量") {
      // 「测试向量」放在向量设置区里，就近验证
      const actions = el("div", "cfg-actions");
      const testBtn = el("button", "ghost", "测试向量");
      testBtn.addEventListener("click", (event) => testEmbedding(event));
      actions.append(testBtn);
      section.append(actions);
    }
    body.append(section);
  }
  syncConditionals();
  body.append(
    el(
      "p",
      "hint",
      "留空 Embedding Base URL 与模型名 = 用 AstrBot 自带的 embedding 接口。改完保存后立刻生效，无需重启。",
    ),
  );
}

async function openConfig() {
  $("cfg-err").textContent = "";
  $("cfg-body").innerHTML = '<p class="hint">加载中…</p>';
  $("cfg-mask").classList.remove("hidden");
  try {
    const payload = await bridge.apiGet("config", {});
    renderConfig(payload || {});
  } catch (error) {
    $("cfg-body").innerHTML = "";
    $("cfg-err").textContent =
      (error && error.message) || "读取配置失败";
  }
}

function closeConfig() {
  $("cfg-mask").classList.add("hidden");
}

function collectConfig() {
  const out = {};
  for (const input of $("cfg-body").querySelectorAll("[data-key]")) {
    const key = input.dataset.key;
    const kind = input.dataset.type;
    if (kind === "bool") {
      // 布尔项可能是 checkbox，也可能是「启用/禁用」下拉
      if (input.tagName === "SELECT") out[key] = input.value === "true";
      else out[key] = input.checked;
    } else if (kind === "int" || kind === "float") {
      out[key] = input.value === "" ? 0 : Number(input.value);
    } else {
      out[key] = input.value === "__none__" ? "" : input.value;
    }
  }
  return out;
}

async function saveConfig() {
  $("cfg-save").disabled = true;
  $("cfg-err").textContent = "";
  try {
    const result = await bridge.apiPost("config/save", collectConfig());
    toast(`已保存 ${((result && result.saved) || []).length} 项`);
    closeConfig();
    await load();
  } catch (error) {
    $("cfg-err").textContent =
      (error && error.message) || "保存失败，请检查填写内容。";
  } finally {
    $("cfg-save").disabled = false;
  }
}

async function testEmbedding(event) {
  const btn = (event && event.currentTarget) || null;
  if (btn) btn.disabled = true;
  $("cfg-err").textContent = "";
  try {
    const result = await bridge.apiPost("embedding/test", {});
    if (result && result.ok) {
      toast(`向量 OK：维度 ${result.dim}，耗时 ${result.elapsed}s`);
      $("cfg-err").textContent = `维度 ${result.dim} · ${result.elapsed}s · 前几维 ${JSON.stringify(result.preview)}`;
    } else {
      $("cfg-err").textContent =
        (result && result.message) || "没有拿到向量";
    }
  } catch (error) {
    $("cfg-err").textContent = (error && error.message) || "测试失败";
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------ 人类伪装指南 · 这本子（按天） */
const bookState = {
  pages: [], // 按日期正序：{ id, day, content }
  sheets: [], // 展示用的“纸”：一天写多了就多占几张
  sheet: 0,
  pageId: null,
  editing: null,
  open: false,
  enabled: true,
  limit: 8000,
  injectDays: 2,
  recallDays: 2,
  flipTimer: null,
  closeTimer: null,
  confirmAction: null,
};

const LINES_PER_PAGE = 19; // 每页固定 19 行（行高 27px）
const CHARS_PER_LINE = 30; // 一行大约放 30 个汉字，用来估算一条占几行

function fmtDateTime(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 一行分三类：item（编号条目）/ note（批注）/ struck（被划掉的旧条目）
function parseLines(text) {
  const out = [];
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    let m = /^(?:\d+[.、)）]\s*)?\[strike\]([\s\S]*?)\[\/strike\]$/.exec(line);
    if (m) {
      out.push({ kind: "struck", text: m[1].trim() });
      continue;
    }
    m = /^(\d+)[.、)）]\s*(.+)$/.exec(line);
    if (m) {
      out.push({ kind: "item", text: m[2].trim() });
      continue;
    }
    out.push({ kind: "note", text: line });
  }
  return out;
}

function appendRichText(node, text) {
  const re = /\[strike\]([\s\S]*?)\[\/strike\]/g;
  let last = 0;
  let match;
  while ((match = re.exec(text))) {
    if (match.index > last) {
      node.append(document.createTextNode(text.slice(last, match.index)));
    }
    const struck = document.createElement("s");
    struck.className = "strike";
    struck.textContent = match[1];
    node.append(struck);
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    node.append(document.createTextNode(text.slice(last)));
  }
}

// 一条占几行：去掉 [strike] 标签后按每行 30 字估算
function itemLines(item) {
  const plain = String(item && item.text !== undefined ? item.text : item || "");
  return Math.max(1, Math.ceil(plain.length / CHARS_PER_LINE));
}

// 按行容量把一天的条目切成若干页（长条目整条挪到下一页，不切断）
function paginate(items) {
  const pages = [[]];
  let used = 0;
  for (const item of items) {
    const need = itemLines(item);
    const current = pages[pages.length - 1];
    if (current.length && used + need > LINES_PER_PAGE) {
      pages.push([]);
      used = 0;
    }
    pages[pages.length - 1].push(item);
    used += need;
  }
  return pages;
}

function countItems(list) {
  return (list || []).filter((entry) => entry.kind !== "note").length;
}

function buildSheets() {
  const sheets = [];
  for (const page of bookState.pages) {
    const items = parseLines(page.content || "");
    if (!items.length) {
      sheets.push({ day: page.day, cont: 0, pageId: page.id, left: [], right: [] });
      continue;
    }
    const pagesOfDay = paginate(items);
    let cont = 0;
    let dayStart = 0;
    for (let i = 0; i < pagesOfDay.length; i += 2) {
      const left = pagesOfDay[i] || [];
      const right = pagesOfDay[i + 1] || [];
      sheets.push({
        day: page.day,
        cont,
        pageId: page.id,
        left,
        right,
        startNo: dayStart,
      });
      dayStart += countItems(left) + countItems(right);
      cont += 1;
    }
  }
  return sheets;
}

async function loadBook() {
  try {
    const data = await bridge.apiGet("journal", {});
    const pages = ((data && data.pages) || [])
      .slice()
      .sort((a, b) => String(a.day).localeCompare(String(b.day)));
    bookState.pages = pages;
    bookState.sheets = buildSheets();
    bookState.enabled = !(data && data.enabled === false);
    bookState.limit = (data && data.limit) || 8000;
    bookState.injectDays = Number((data && data.inject_days) || 2);
    bookState.recallDays = Number((data && data.recall_days) || 0);
    $("guide-toggle").checked = bookState.enabled;
    $("book-cover-sub").textContent = pages.length
      ? `共 ${pages.length} 天 · ${bookState.sheets.length} 张`
      : "还没写过";
    const stats = (data && data.stats) || {};
    if (pages.length) {
      const recall = bookState.recallDays
        ? ` + 按关键词翻 ${bookState.recallDays} 天的旧记录`
        : "";
      $("guide-meta").textContent =
        `共 ${stats.pages} 天 / ${stats.chars} 字 · 每轮读最近 ${bookState.injectDays} 天${recall}` +
        ` · 上限 ${bookState.limit} 字`;
      const edited = fmtDateTime(stats.latest_at);
      $("book-updated").textContent = edited ? `最后编辑于 ${edited}` : "";
      if (!bookState.open) bookState.sheet = bookState.sheets.length - 1;
      bookState.sheet = Math.min(bookState.sheet, bookState.sheets.length - 1);
      await showSheet(bookState.sheet);
    } else {
      renderEmptySheet();
      $("book-updated").textContent = "";
    }
  } catch (error) {
    $("guide-meta").textContent =
      "读取失败：" + ((error && error.message) || error);
  }
}

function fillPage(side, items, sheet, pageNo, startNo) {
  const no = $(`book-page-no-${side}`);
  const day = $(`book-page-day-${side}`);
  const list = $(`book-items-${side}`);
  const foot = $(`book-page-foot-${side}`);
  no.textContent = "";
  // 当天的页码按「页」算：一张纸两页，左 1 右 2，下一张 3/4
  const dayPageNo = sheet ? sheet.cont * 2 + (side === "left" ? 1 : 2) : 0;
  day.textContent = sheet ? `${sheet.day}（${dayPageNo}）` : "";
  list.innerHTML = "";
  if (!items || !items.length) {
    const empty = document.createElement("div");
    empty.className = "book-empty";
    empty.textContent = sheet ? "" : "这本子还是空的。点「重写今日」写下今天，等她自己学也行。";
    list.appendChild(empty);
  } else {
    let n = startNo || 0;
    for (const entry of items) {
      const row = document.createElement("div");
      if (entry.kind === "struck") {
        n += 1;
        row.className = "line item struck";
        const num = document.createElement("span");
        num.className = "num";
        num.textContent = `${n}.`;
        row.appendChild(num);
        const s = document.createElement("s");
        s.className = "strike";
        s.textContent = entry.text;
        row.appendChild(s);
      } else if (entry.kind === "item") {
        n += 1;
        row.className = "line item";
        const num = document.createElement("span");
        num.className = "num";
        num.textContent = `${n}.`;
        row.appendChild(num);
        row.appendChild(document.createTextNode(entry.text));
      } else {
        row.className = "line note";
        row.textContent = entry.text;
      }
      list.appendChild(row);
    }
  }
  foot.textContent = sheet && pageNo ? `— ${pageNo} —` : "";
}

function renderEmptySheet() {
  fillPage("left", [], null, 0);
  fillPage("right", [], null, 0);
  $("book-indicator").textContent = "—";
}

// 真实翻纸：右页那张纸绕书脊翻到左边（背面就是新的左页），反向同理
function playFlip({ forward, front, back, onMid }) {
  const spreadEl = document.querySelector(".book-spread");
  if (!spreadEl) return;
  const sheet = document.createElement("div");
  sheet.className = "flip-sheet " + (forward ? "fwd" : "back");

  const makeFace = (html, side, extra) => {
    const face = document.createElement("div");
    face.className = "flip-face" + (extra ? " " + extra : "");
    const page = document.createElement("div");
    page.className = "book-page " + side;
    page.innerHTML = html;
    face.appendChild(page);
    return face;
  };

  sheet.appendChild(makeFace(front, forward ? "right" : "left", ""));
  sheet.appendChild(makeFace(back, forward ? "left" : "right", "back"));
  spreadEl.appendChild(sheet);
  void sheet.offsetWidth;
  sheet.classList.add("run");

  clearTimeout(bookState.flipTimer);
  const midTimer = setTimeout(() => {
    if (typeof onMid === "function") onMid();
  }, 300);
  bookState.flipTimer = setTimeout(() => {
    clearTimeout(midTimer);
    sheet.remove();
  }, 720);
}

async function showSheet(index, animate = false, direction = 1) {
  if (!bookState.sheets.length) {
    renderEmptySheet();
    return;
  }
  const total = bookState.sheets.length;
  const i = Math.max(0, Math.min(index, total - 1));
  const sheet = bookState.sheets[i];

  const leftEl = document.querySelector(".book-page.left");
  const rightEl = document.querySelector(".book-page.right");
  const snapshot =
    animate && leftEl && rightEl
      ? { left: leftEl.innerHTML, right: rightEl.innerHTML }
      : null;

  const apply = () => {
    bookState.sheet = i;
    bookState.pageId = sheet.pageId;
    fillPage("left", sheet.left, sheet, i * 2 + 1, sheet.startNo);
    fillPage(
      "right",
      sheet.right,
      sheet,
      i * 2 + 2,
      (sheet.startNo || 0) + countItems(sheet.left),
    );
    $("book-indicator").textContent = `${sheet.day}（${sheet.cont * 2 + 1}–${sheet.cont * 2 + 2}）`;
  };

  if (!snapshot) {
    apply();
    return;
  }

  apply();
  const freshLeft = leftEl.innerHTML;
  const freshRight = rightEl.innerHTML;

  if (direction > 0) {
    // 下一张：旧右页翻到左边，背面 = 新左页；旧左页先留着，纸掠过 90° 时再换
    leftEl.innerHTML = snapshot.left;
    playFlip({
      forward: true,
      front: snapshot.right,
      back: freshLeft,
      onMid: () => {
        leftEl.innerHTML = freshLeft;
      },
    });
  } else {
    // 上一张：旧左页翻到右边，背面 = 新右页
    rightEl.innerHTML = snapshot.right;
    playFlip({
      forward: false,
      front: snapshot.left,
      back: freshRight,
      onMid: () => {
        rightEl.innerHTML = freshRight;
      },
    });
  }
}

// 把当前左页克隆到封面背面：封面翻到左边时看到的就是真正的那一页
function syncCoverBack() {
  const src = document.querySelector(".book-spread > .book-page.left");
  const back = $("cover-back");
  if (!back) return;
  const page = document.createElement("div");
  page.className = "book-page left";
  page.innerHTML = src ? src.innerHTML : "";
  back.replaceChildren(page);
}

function openBook() {
  if (bookState.open) return;
  bookState.open = true;
  if (!bookState.sheets.length) renderEmptySheet();
  else showSheet(bookState.sheet);
  syncCoverBack();
  $("book").classList.add("open");
}

function closeBook() {
  if (!bookState.open) return;
  bookState.open = false;
  syncCoverBack();
  $("book").classList.add("closing");
  $("book").classList.remove("open");
  clearTimeout(bookState.closeTimer);
  bookState.closeTimer = setTimeout(() => {
    $("book").classList.remove("closing");
  }, 800);
}

async function stepSheet(dir) {
  if (!bookState.open) {
    openBook();
    return;
  }
  if (!bookState.sheets.length) return;
  const next = bookState.sheet + dir;
  if (next < 0 || next >= bookState.sheets.length) {
    toast(dir > 0 ? "已经是最后一张纸了" : "已经是第一张纸了");
    return;
  }
  await showSheet(next, true, dir);
}

function currentPage() {
  if (!bookState.pageId) return null;
  return bookState.pages.find((p) => p.id === bookState.pageId) || null;
}

async function searchBook() {
  const word = $("book-search").value.trim();
  const hitBox = $("book-search-hit");
  hitBox.innerHTML = "";
  if (!word) {
    hitBox.classList.add("hidden");
    return;
  }
  try {
    const data = await bridge.apiGet("journal/search", { q: word });
    const pages = (data && data.pages) || [];
    hitBox.classList.remove("hidden");
    if (!pages.length) {
      hitBox.append(el("span", "book-hit-empty", `书里没有和「${word}」相关的记录`));
      return;
    }
    hitBox.append(el("span", "book-hit-label", `找到 ${pages.length} 天：`));
    for (const page of pages) {
      const btn = el(
        "button",
        "book-hit",
        `${page.day}（${page.chars} 字）${page.excerpt.slice(0, 18)}`,
      );
      btn.type = "button";
      btn.addEventListener("click", () => {
        const idx = bookState.sheets.findIndex((s) => s.pageId === page.id && s.cont === 0);
        if (idx < 0) return;
        openBook();
        showSheet(idx, true);
      });
      hitBox.append(btn);
    }
  } catch (error) {
    toast((error && error.message) || "查询失败", true);
  }
}

function openPageEditor(page) {
  bookState.editing = page || null;
  $("guide-dlg-title").textContent = page && page.day
    ? `编辑 ${page.day}`
    : "编辑今天";
  $("guide-day").value = (page && page.day) || todayStr();
  $("guide-text").value = page ? page.content || "" : "";
  $("guide-err").textContent = "";
  updateGuideCount();
  $("guide-mask").classList.remove("hidden");
  setTimeout(() => $("guide-text").focus(), 30);
}

function closeGuideEditor() {
  $("guide-mask").classList.add("hidden");
  bookState.editing = null;
}

function updateGuideCount() {
  const text = $("guide-text").value || "";
  const lineCount = parseLines(text).length;
  $("guide-count").textContent = `${text.length} 字 · ${lineCount} 条`;
}

async function savePage() {
  const content = $("guide-text").value.trim();
  const day = $("guide-day").value || todayStr();
  $("guide-save").disabled = true;
  try {
    await bridge.apiPost("journal/save", { day, content });
    closeGuideEditor();
    toast("已保存");
    await loadBook();
    if (!bookState.open) openBook();
    const idx = bookState.sheets.findIndex((s) => s.day === day && s.cont === 0);
    if (idx >= 0) await showSheet(idx);
  } catch (error) {
    $("guide-err").textContent =
      (error && error.message) || "保存失败，请检查内容后重试。";
  } finally {
    $("guide-save").disabled = false;
  }
}

async function toggleGuideInject(event) {
  const box = event.currentTarget;
  const enabled = Boolean(box.checked);
  box.disabled = true;
  try {
    await bridge.apiPost("journal/toggle", { enabled });
    toast(enabled ? "指南书注入已开启" : "指南书注入已关闭（她仍可自己续写）");
    await loadBook();
  } catch (error) {
    box.checked = !enabled;
    toast((error && error.message) || "切换失败", true);
  } finally {
    box.disabled = false;
  }
}

function openConfirm(text, action) {
  bookState.confirmAction = typeof action === "function" ? action : null;
  $("confirm-text").textContent = text;
  $("confirm-mask").classList.remove("hidden");
}

function closeConfirm() {
  $("confirm-mask").classList.add("hidden");
  bookState.confirmAction = null;
}

async function runConfirm() {
  const action = bookState.confirmAction;
  closeConfirm();
  if (action) await action();
}

async function writeTodayNow() {
  const btn = $("book-review");
  btn.disabled = true;
  btn.textContent = "写着…";
  try {
    const result = await bridge.apiPost("journal/write_today", { day: todayStr() });
    if (result && result.ok) {
      toast(
        result.overwritten
          ? `今天的记录已重写（${result.items} 条）`
          : `今天的记录写好了（${result.items} 条）`,
      );
    } else {
      toast((result && result.message) || "这次没写出来", true);
    }
    await loadBook();
    if (!bookState.open) openBook();
    const idx = bookState.sheets.findIndex((s) => s.day === todayStr() && s.cont === 0);
    if (idx >= 0) await showSheet(idx);
  } catch (error) {
    toast((error && error.message) || "重写今日失败", true);
  } finally {
    btn.disabled = false;
    btn.textContent = "重写今日";
  }
}

function askWriteToday() {
  openConfirm("确定重写今日记录吗？", writeTodayNow);
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
  $("book-cover").addEventListener("click", openBook);
  $("book-cover").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openBook();
    }
  });
  $("book-prev").addEventListener("click", () => stepSheet(-1));
  $("book-next").addEventListener("click", () => stepSheet(1));
  $("book-edit").addEventListener("click", () => {
    const page = currentPage();
    openPageEditor(page || null);
  });
  $("book-review").addEventListener("click", askWriteToday);
  $("confirm-ok").addEventListener("click", runConfirm);
  $("confirm-cancel").addEventListener("click", closeConfirm);
  $("confirm-close").addEventListener("click", closeConfirm);
  $("confirm-mask").addEventListener("click", (event) => {
    if (event.target === $("confirm-mask")) closeConfirm();
  });
  $("book-stage").addEventListener("click", (event) => {
    if (event.target.closest(".book")) return;
    closeBook();
  });
  let clickTimer = null;
  document.querySelectorAll(".book-page").forEach((el2) => {
    const dir = el2.classList.contains("left") ? -1 : 1;
    el2.addEventListener("click", () => {
      clearTimeout(clickTimer);
      clickTimer = setTimeout(() => {
        if (!bookState.open) {
          openBook();
          return;
        }
        if (dir < 0 && bookState.sheet === 0) {
          closeBook();
          return;
        }
        stepSheet(dir);
      }, 220);
    });
    el2.addEventListener("dblclick", () => {
      clearTimeout(clickTimer);
      const page = currentPage();
      if (!page) return;
      openPageEditor(page);
    });
  });
  $("book-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchBook();
    }
  });
  $("guide-toggle").addEventListener("change", toggleGuideInject);
  $("guide-dlg-close").addEventListener("click", closeGuideEditor);
  $("guide-mask").addEventListener("click", (event) => {
    if (event.target === $("guide-mask")) closeGuideEditor();
  });
  $("guide-cancel").addEventListener("click", closeGuideEditor);
  $("guide-save").addEventListener("click", savePage);
  $("guide-text").addEventListener("input", updateGuideCount);
  $("cfg-btn").addEventListener("click", openConfig);
  $("cfg-close").addEventListener("click", closeConfig);
  $("cfg-cancel").addEventListener("click", closeConfig);
  $("cfg-save").addEventListener("click", saveConfig);
  $("cfg-mask").addEventListener("click", (event) => {
    if (event.target === $("cfg-mask")) closeConfig();
  });
  $("dlg-close").addEventListener("click", closeEditor);
  $("dlg-cancel").addEventListener("click", closeEditor);
  $("dlg-save").addEventListener("click", save);
  $("mask").addEventListener("click", (event) => {
    if (event.target === $("mask")) closeEditor();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        if (!$("mask").classList.contains("hidden")) save();
        else if (!$("cfg-mask").classList.contains("hidden")) saveConfig();
        else if (!$("guide-mask").classList.contains("hidden")) savePage();
      }
      return;
    }
    if (!$("mask").classList.contains("hidden")) closeEditor();
    if (!$("cfg-mask").classList.contains("hidden")) closeConfig();
    if (!$("confirm-mask").classList.contains("hidden")) closeConfirm();
    if (!$("guide-mask").classList.contains("hidden")) closeGuideEditor();
    
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
  await loadBook();
}

main();
