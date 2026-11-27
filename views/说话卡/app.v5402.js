// 教我说话 · 说话卡面板
// 通过 AstrBot 插件视图 bridge 与插件后端通信（window.AstrBotPluginView）。

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  status: "active",
  keyword: "",
  items: [],
  page: 1,
  pageSize: 9,
  total: 0,
  sort: "time",
  direction: "desc",
  requestId: 0,
  editing: null,
  tagClassifyId: 0,
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
async function load({ scroll = false } = {}) {
  const requestId = ++state.requestId;
  listEl.setAttribute("aria-busy", "true");
  $("card-prev").disabled = true;
  $("card-next").disabled = true;
  try {
    const data = await bridge.apiGet("cards", {
      status: state.status,
      q: state.keyword,
      limit: state.pageSize,
      page: state.page,
      sort: state.sort,
      direction: state.direction,
    });
    if (requestId !== state.requestId) return;
    state.items = data.items || [];
    state.total = data.total ?? state.items.length;
    state.page = data.page || 1;
    renderStats(data.stats || {});
    renderList();
    renderPagination();
    if (scroll) $("cards-title").scrollIntoView({ block: "start" });
  } catch (error) {
    if (requestId !== state.requestId) return;
    listEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "empty";
    box.textContent = "读取失败：" + (error && error.message ? error.message : error);
    listEl.appendChild(box);
    $("card-range").textContent = "";
    $("card-page").textContent = "—";
    toast("读取失败", true);
  } finally {
    if (requestId === state.requestId) listEl.setAttribute("aria-busy", "false");
  }
}

function renderPagination() {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  const start = (state.page - 1) * state.pageSize;
  $("card-range").textContent = state.total
    ? `${start + 1}–${start + state.items.length} / 共 ${state.total} 张`
    : "共 0 张";
  $("card-page").textContent = `${state.page} / ${pages}`;
  $("card-prev").disabled = state.page <= 1;
  $("card-next").disabled = state.page >= pages;
}

function renderStats(stats) {
  const chips = [
    ["生效中", stats.active ?? 0],
    ["已归档", stats.archived ?? 0],
    ["注入总次数", stats.uses ?? 0],
    ["平均权重", stats.avg_weight ?? 0],
    ["反馈 👍/👎", `${stats.feedback_up ?? 0}/${stats.feedback_down ?? 0}`],
  ];
  const settingsButton = $("cfg-btn");
  statsEl.replaceChildren(settingsButton);
  for (const [label, value] of chips) {
    const chip = document.createElement("div");
    chip.className = "chip";
    const b = document.createElement("b");
    b.textContent = String(value);
    const s = document.createElement("span");
    s.textContent = label;
    chip.append(b, s);
    settingsButton.before(chip);
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
function setTagAutoError(message = "") {
  const errEl = $("f-tags-err");
  const input = $("f-tags");
  if (input) input.classList.toggle("invalid", Boolean(message));
  if (errEl) {
    errEl.textContent = message;
    errEl.classList.toggle("hidden", !message);
  }
}

function openEditor(card) {
  state.editing = card || null;
  state.tagClassifyId += 1;
  $("dlg-title").textContent = card ? `编辑说话卡 #${card.id}` : "新建说话卡";
  $("f-scene").value = card ? card.scene || "" : "";
  $("f-bad").value = card ? card.bad || "" : "";
  $("f-good").value = card ? card.good || "" : "";
  $("f-principle").value = card ? card.principle || "" : "";
  $("f-tags").value = card ? (card.tags || []).join(",") : "";
  $("f-weight").value = card ? Number(card.weight || 1.5).toFixed(1) : "1.5";
  $("dlg-err").textContent = "";
  setTagAutoError("");
  const autoBtn = $("f-tags-auto");
  if (autoBtn) {
    autoBtn.disabled = false;
    autoBtn.textContent = "自动识别";
  }
  $("mask").classList.remove("hidden");
  setTimeout(() => $("f-scene").focus(), 30);
}

function closeEditor() {
  state.tagClassifyId += 1;
  $("mask").classList.add("hidden");
  state.editing = null;
}

async function autoClassifyTags() {
  const scene = $("f-scene").value.trim();
  const bad = $("f-bad").value.trim();
  const good = $("f-good").value.trim();
  const principle = $("f-principle").value.trim();
  $("dlg-err").textContent = "";
  if (!scene && !bad && !good && !principle) {
    setTagAutoError("说话卡内容为空，请先填写场景、反例、正例或要点后再识别。");
    return;
  }
  setTagAutoError("");
  const btn = $("f-tags-auto");
  const reqId = ++state.tagClassifyId;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "识别中…";
  }
  try {
    const payload = { scene, bad, good, principle };
    if (state.editing && state.editing.id) payload.id = state.editing.id;
    const result = await bridge.apiPost("cards/classify_tags", payload);
    if (reqId !== state.tagClassifyId) return;
    const tags = Array.isArray(result && result.tags)
      ? result.tags.map((t) => String(t || "").trim()).filter(Boolean)
      : [];
    if (!tags.length) {
      setTagAutoError("向量模型未返回有效标签，请检查向量模型配置或补充卡片内容。");
      return;
    }
    $("f-tags").value = tags.join(",");
    setTagAutoError("");
    toast(`已识别标签：${tags.join("、")}`);
  } catch (error) {
    if (reqId !== state.tagClassifyId) return;
    setTagAutoError((error && error.message) || "向量模型分类失败，请检查向量模型配置。");
  } finally {
    if (reqId === state.tagClassifyId && btn) {
      btn.disabled = false;
      btn.textContent = "自动识别";
    }
  }
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
const cfgState = {
  fields: [],
  values: {},
  providers: [],
  chatProviders: [],
  defaultChatProvider: "",
};

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

  // ② 平台向量模型下拉（不选则跟随 AstrBot 默认）
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
    const defaultEmb = cfgState.providers[0];
    addOption(
      "",
      defaultEmb
        ? `跟随 AstrBot 默认（${defaultEmb.label}）`
        : "跟随 AstrBot 默认（平台未配置 embedding 模型）",
    );
    for (const prov of cfgState.providers) addOption(prov.id, prov.label);
    const current = String(value || "");
    select.value = cfgState.providers.some((p) => p.id === current) ? current : "";
    select.addEventListener("change", () => {
      const prov = select.value
        ? cfgState.providers.find((p) => p.id === select.value)
        : cfgState.providers[0];
      const dimField = document.querySelector('[data-key="emb_dim"]');
      if (dimField && prov && prov.dim) dimField.value = String(prov.dim);
      syncConditionals();
    });
    label.append(select);
    return label;
  }

  // ③ 提炼模型下拉（不选则跟随 AstrBot 默认）
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
    const defaultChat = cfgState.defaultChatProvider;
    addOption(
      "",
      defaultChat
        ? `跟随 AstrBot 默认（${defaultChat}）`
        : cfgState.chatProviders.length
          ? "跟随 AstrBot 默认"
          : "跟随 AstrBot 默认（平台未配置对话模型）",
    );
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
  if (field.role) input.dataset.role = field.role;
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
  if (field.role === "id-list") {
    const errEl = el("span", "field-err hidden");
    errEl.setAttribute("aria-live", "polite");
    label.append(errEl);
    input.addEventListener("input", () => checkIdListField(input));
    input.addEventListener("blur", () => checkIdListField(input));
  }
  return label;
}

function validateIdList(raw) {
  const text = raw === undefined || raw === null ? "" : String(raw);
  if (text === "") return "";
  const reasons = [];
  if (/\s/.test(text)) reasons.push("不能包含空格或换行");
  if (text.includes("，")) reasons.push("请使用英文逗号 ,（不要用中文逗号 ，）");
  if (/[、；;|/]/.test(text)) reasons.push("只允许使用英文逗号 , 分隔");
  if (text.startsWith(",") || text.endsWith(",")) reasons.push("开头或结尾不能有多余逗号");
  if (text.includes(",,")) reasons.push("存在连续逗号（空项）");
  const tokens = text.split(/[,，、；;|/\s]+/).filter(Boolean);
  const bad = tokens.filter((t) => !/^[0-9]+$/.test(t));
  if (bad.length) reasons.push(`号码必须为纯半角数字（无效项：${bad.slice(0, 3).join("、")}）`);
  const seen = new Set();
  const dups = [];
  for (const t of tokens) {
    if (/^[0-9]+$/.test(t)) {
      if (seen.has(t) && !dups.includes(t)) dups.push(t);
      seen.add(t);
    }
  }
  if (dups.length) reasons.push(`存在重复号码（${dups.slice(0, 3).join("、")}）`);
  if (!reasons.length && !/^[0-9]+(?:,[0-9]+)*$/.test(text)) {
    reasons.push("格式不符合英文逗号分隔的纯数字规范");
  }
  return reasons.length ? `格式错误：${reasons.join("；")}。规范示例：123456,789012` : "";
}

function checkIdListField(input) {
  const field = input.closest(".field");
  const errEl = field ? field.querySelector(".field-err") : null;
  const err = validateIdList(input.value);
  input.classList.toggle("invalid", Boolean(err));
  input.setAttribute("aria-invalid", err ? "true" : "false");
  if (errEl) {
    errEl.textContent = err;
    errEl.classList.toggle("hidden", !err);
  }
  return err;
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
  cfgState.defaultChatProvider = payload.default_chat_provider || "";
  const body = $("cfg-body");
  body.innerHTML = "";

  const emb = payload.embedding || {};
  const banner = el("div", "cfg-banner");
  const modeText = emb.mode === "manual" || emb.mode === "api" ? "独立 API" : "AstrBot 默认接口";
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
  for (const input of body.querySelectorAll('input[data-role="id-list"]')) {
    checkIdListField(input);
  }
  body.append(
    el(
      "p",
      "hint",
      "模型下拉选择「跟随 AstrBot 默认」即使用平台当前默认模型。改完保存后立刻生效，无需重启。",
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
  let firstInvalid = null;
  let firstErr = "";
  for (const input of $("cfg-body").querySelectorAll('input[data-role="id-list"]')) {
    const err = checkIdListField(input);
    const holder = input.closest("[data-show-key]");
    if (err && !firstInvalid && (!holder || !holder.classList.contains("hidden"))) {
      firstInvalid = input;
      firstErr = err;
    }
  }
  if (firstInvalid) {
    $("cfg-err").textContent = firstErr;
    firstInvalid.focus();
    firstInvalid.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
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

/* ------------------------------------------------------------ 人类伪装指南 · 连续书页 */
const bookState = {
  sheets: [], // 展示用的“纸”：按纸面容量连续排版
  sheet: 0,
  open: false,
  scope: "all",
  closeTimer: null,
  confirmAction: null,
  searchId: 0,
  loadId: 0,
  revision: null,
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

// 一条占几行：去掉 [strike] 标签后按每行 30 字估算
function itemLines(item) {
  const plain = String(item && item.text !== undefined ? item.text : item || "");
  return plain.split(/\r?\n/).reduce((sum,line) => sum + Math.max(1, Math.ceil(line.length / CHARS_PER_LINE)), 0);
}

// 按行容量将全书条目连续排入纸页（长条目整条挪到下一页，不切断）
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

// 后端给的是结构化行（带 tag）；老数据没有 rows 时退回解析 content 快照
function buildSheets(entries) {
  const pages = paginate(entries);
  const sheets = [];
  let startNo = 0;
  for (let i = 0; entries.length && i < pages.length; i += 2) {
    const left = pages[i], right = pages[i + 1] || [];
    const rows = [...left, ...right];
    sheets.push({left, right, rows, startNo});
    startNo += countItems(rows);
  }
  return sheets;
}

async function loadBook({focusDay = "", focusId = null} = {}) {
  const requestId = ++bookState.loadId;
  ++bookState.searchId;
  $("book-search-hit").classList.add("hidden");
  try {
    const data = await bridge.apiGet("journal", {});
    if (requestId !== bookState.loadId) return;
    if (!Array.isArray(data.entries)) throw new Error("请先重载教我说话插件。");
    const previous = bookState.sheet;
    const wasAtEnd = previous >= bookState.sheets.length - 1;
    const anchor = bookState.sheets[previous]?.rows[0]?.id;
    bookState.sheets = buildSheets(data.entries);
    bookState.revision = data.revision;
    bookState.scope = data.scope;
    $("guide-toggle").checked = data.enabled;
    const sheets = bookState.sheets;
    const count = countItems(data.entries);
    $("book-cover-sub").textContent = sheets.length ? '共 ' + count + ' 条 · ' + sheets.length + ' 张' : "还没写过";
    $("guide-meta").textContent = '最近 ' + data.inject_entries + ' 条观察及其间的批注 · 最多召回 ' + data.recall_entries + ' 条旧观察 · 上限 ' + data.limit + ' 字';
    const latest = data.entries.reduce((t,r) => Math.max(t,r.updated_at),0);
    $("book-updated").textContent = latest ? '最后编辑于 ' + fmtDateTime(latest) : "";
    if (!sheets.length) { renderEmptySheet(); return; }
    const start = sheets.findIndex(sheet => sheet.rows.some(r => r.id === anchor));
    const target = focusId ? sheets.findIndex(sheet => sheet.rows.some(r => r.id === focusId))
      : focusDay ? sheets.findLastIndex(sheet => sheet.rows.some(r => r.day === focusDay)) : -1;
    if (target < 0) {
      showSheet(bookState.open ? (start < 0 ? previous : start) : sheets.length - 1);
    } else if (bookState.open && !wasAtEnd && start >= 0) {
      showSheet(start);
      showSheet(target, true);
    } else {
      showSheet(target);
      openBook();
    }
  } catch (error) {
    if (requestId === bookState.loadId) $("guide-meta").textContent = "读取失败：" + error.message;
  }
}

function basePage(side) {
  return $("book-spread").querySelector(`:scope > .book-page.${side}`);
}

function fillPageInto(root, side, items, sheet, pageNo, startNo) {
  const list = root.querySelector(".book-items");
  const foot = root.querySelector(".book-page-foot");
  list.innerHTML = "";
  if (!items || !items.length) {
    const empty = document.createElement("div");
    empty.className = "book-empty";
    empty.textContent = sheet ? "" : "这本子还是空的。点「重写今日」写下今天，等她自己学也行。";
    list.appendChild(empty);
  } else {
    let n = startNo || 0;
    for (const entry of [...items.filter(r => r.kind !== "note"), ...items.filter(r => r.kind === "note")]) {
      const row = document.createElement("div");
      row.dataset.entryDay = entry.day;
      row.title = entry.day;
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

function fillPage(side, items, sheet, pageNo, startNo) {
  fillPageInto(basePage(side), side, items, sheet, pageNo, startNo);
}

// 把第 index 张纸的左/右面画到 root 上
function paintSide(root, side, index) {
  const sheet = bookState.sheets[index];
  if (!sheet) {
    fillPageInto(root, side, [], null, 0);
    return;
  }
  if (side === "left") {
    fillPageInto(root, "left", sheet.left, sheet, index * 2 + 1, sheet.startNo);
  } else {
    fillPageInto(root, "right", sheet.right, sheet, index * 2 + 2,
      (sheet.startNo || 0) + countItems(sheet.left));
  }
}

// 一页独立的纸（翻页时贴在翻动的纸上），不带 id
function pageNode(side, index) {
  const page = basePage(side).cloneNode(true);
  page.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
  paintSide(page, side, index);
  return page;
}

function renderEmptySheet() {
  clearFlip();
  bookState.sheet = 0;
  flipper.leftBase = 0;
  flipper.rightBase = 0;
  fillPage("left", [], null, 0);
  fillPage("right", [], null, 0);
  $("book-indicator").textContent = "—";
}

/* ------------------------------------------------------------ 翻页引擎
 * 每次点击立刻掀起一张独立的纸，不用等上一张落下：连点就是一沓纸接连翻过去。
 * 纸以书脊为轴，被切成若干竖条串成一条可以弯曲的曲面：
 *   - 掀起时外缘先离开纸面，书脊一侧后跟上；落下时书脊先贴住，外缘最后飘落；
 *   - 每一条按自身倾角打光，翻起的纸在底下的页面上投下随高度变化的影子；
 *   - 翻到一半反向点击，会抓住空中那张纸把它翻回去，而不是再起一张。
 * 状态模型：flipper.leaves 按“书里的先后”排列（前面的纸更靠前），
 *   p = 0 平躺在右边，p = 1 平躺在左边；任何时刻 p 沿数组单调不增，纸不会互相穿过。
 *   leftBase / rightBase 是空中所有纸底下露出来的左页 / 右页。
 */
const FLIP_STRIPS = 16;
const FLIP_DURATION = 860; // 单张纸翻过去的基准时长（ms），连翻时会自动加速
const FLIP_PERSPECTIVE = 3200; // 与 .flip-leaf 的 CSS perspective 保持一致，用来精确判断正反面朝向
const flipper = {
  leaves: [],
  leftBase: 0,
  rightBase: 0,
  raf: 0,
  last: 0,
  layer: null,
  shadows: null,
};

const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const easeInOutSine = (t) => (1 - Math.cos(Math.PI * t)) / 2;
// 弯曲量：前段外缘领先（正），后段外缘滞后（负），首尾为 0 保证纸能平躺
const curlOf = (q) => 1.9 * Math.sin(Math.PI * q) * (1 - 1.5 * q);
const SHADE_RGB = "62, 44, 18";

function flipLayers() {
  const spread = $("book-spread");
  if (!flipper.layer || flipper.layer.parentNode !== spread) {
    flipper.shadows = document.createElement("div");
    flipper.shadows.className = "flip-shadows";
    flipper.shadows.setAttribute("aria-hidden", "true");
    flipper.layer = document.createElement("div");
    flipper.layer.className = "flip-layer";
    flipper.layer.setAttribute("aria-hidden", "true");
    spread.append(flipper.shadows, flipper.layer);
  }
}

function clearFlip() {
  cancelAnimationFrame(flipper.raf);
  flipper.raf = 0;
  for (const leaf of flipper.leaves) disposeLeaf(leaf);
  flipper.leaves = [];
}

function disposeLeaf(leaf) {
  leaf.el.remove();
  leaf.shadow.remove();
}

function makeFace(page, offset, pageWidth, back, edge) {
  const face = document.createElement("div");
  face.className = `flip-face${back ? " back" : ""}${edge ? " edge" : ""}`;
  const copy = page.cloneNode(true);
  copy.style.width = `${pageWidth}px`;
  copy.style.left = `${offset}px`;
  const shade = document.createElement("div");
  shade.className = "flip-shade";
  face.append(copy, shade);
  return { face, shade };
}

// front：纸躺在右边时朝上的那面（frontIdx 的右页）；back：翻到左边后朝上的那面（backIdx 的左页）
function createLeaf(frontIdx, backIdx, p, dir, delay = 0) {
  flipLayers();
  const pageWidth = $("book-spread").clientWidth / 2;
  const w = pageWidth / FLIP_STRIPS;
  const el = document.createElement("div");
  el.className = "flip-leaf";
  const front = pageNode("right", frontIdx);
  const back = pageNode("left", backIdx);
  const strips = [];
  for (let i = 0; i < FLIP_STRIPS; i += 1) {
    const edge = i === FLIP_STRIPS - 1;
    // 相邻竖条多叠 0.8px 盖住抗锯齿缝；无 preserve-3d 按 DOM 顺序从脊向外绘制，不会触发 3D BSP 切割断裂
    const pad = edge ? 0 : 0.8;
    const strip = document.createElement("div");
    strip.className = "flip-strip";
    strip.style.width = `${w + pad}px`;
    const f = makeFace(front, -i * w, pageWidth, false, edge);
    // 背面用 2D scaleX(-1) 水平翻转，需补上 pad 偏移让接缝精确对齐
    const b = makeFace(back, -(FLIP_STRIPS - 1 - i) * w + pad, pageWidth, true, edge);
    b.face.style.display = "none";
    strip.append(f.face, b.face);
    el.append(strip);
    strips.push({
      el: strip,
      frontFace: f.face,
      backFace: b.face,
      frontShade: f.shade,
      backShade: b.shade,
      isFront: true,
    });
  }
  const shadow = document.createElement("div");
  shadow.className = "flip-shadow";
  flipper.shadows.append(shadow);
  flipper.layer.append(el);
  const leaf = {
    el, shadow, strips, pageWidth, w,
    front: frontIdx, back: backIdx,
    p, dir, s: dir, v: dir * (1000 / FLIP_DURATION), delay,
    angles: new Float64Array(FLIP_STRIPS),
  };
  renderLeaf(leaf);
  return leaf;
}

function renderLeaf(leaf) {
  const n = FLIP_STRIPS;
  const { p, s, w, angles } = leaf;
  const base = Math.PI * easeInOutSine(p);
  // s 在 ±1 之间平滑过渡：正向翻是“外缘领先”，反向翻镜像过来，中途反向不会跳形
  const curl = ((1 + s) / 2) * curlOf(p) - ((1 - s) / 2) * curlOf(1 - p);
  let sum = 0;
  for (let i = 0; i < n; i += 1) {
    const f = (i + 0.5) / n;
    const a = base + curl * (Math.pow(f, 1.15) - 0.3);
    angles[i] = a < 0 ? 0 : a > Math.PI ? Math.PI : a;
    sum += angles[i];
  }
  const shadeFront = (a) => Math.min(0.42, 0.3 * (1 - Math.cos(a)) + 0.04 * Math.sin(a * 2));
  const shadeBack = (a) => Math.min(0.42, 0.3 * (1 + Math.cos(a)) + 0.04 * Math.sin(a * 2) * -1);
  let x = 0;
  let z = 0;
  for (let i = 0; i < n; i += 1) {
    const a = angles[i];
    const cosA = Math.cos(a);
    const sinA = Math.sin(a);
    const xm = x + 0.5 * w * cosA;
    const zm = z + 0.5 * w * sinA;
    // 用解析法判断这一条正面还是背面朝向镜头（替代 backface-visibility，彻底避免浏览器 3D 背面剔除错漏）
    const isFront = xm * sinA + (FLIP_PERSPECTIVE - zm) * cosA >= 0;
    const strip = leaf.strips[i];
    if (strip.isFront !== isFront) {
      strip.isFront = isFront;
      strip.frontFace.style.display = isFront ? "" : "none";
      strip.backFace.style.display = isFront ? "none" : "";
    }
    strip.el.style.transform = `translate3d(${x.toFixed(2)}px, 0, ${z.toFixed(2)}px) rotateY(${(-a).toFixed(4)}rad)`;
    // 用竖条两端的倾角做渐变，光影连续，不会一条一条地跳
    const a0 = i ? (angles[i - 1] + a) / 2 : a;
    const a1 = i < n - 1 ? (angles[i + 1] + a) / 2 : a;
    if (isFront) {
      strip.frontShade.style.background =
        `linear-gradient(90deg, rgba(${SHADE_RGB}, ${shadeFront(a0).toFixed(3)}), rgba(${SHADE_RGB}, ${shadeFront(a1).toFixed(3)}))`;
    } else {
      strip.backShade.style.background =
        `linear-gradient(90deg, rgba(${SHADE_RGB}, ${shadeBack(a1).toFixed(3)}), rgba(${SHADE_RGB}, ${shadeBack(a0).toFixed(3)}))`;
    }
    x += w * cosA;
    z += w * sinA;
  }
  // 越接近竖直的纸越靠上：右半边是先翻的压着后翻的，左半边是后落的压着先落的
  const mean = sum / n;
  leaf.el.style.zIndex = String(1000 - Math.round(Math.abs(mean - Math.PI / 2) * 200));

  // 投影：落在纸外缘之外，纸抬得越高影子越宽越淡
  const lift = Math.max(z, leaf.pageWidth * Math.sin(mean) * 0.6);
  const strength = Math.min(1, lift / 140) * 0.3;
  const spread = 14 + lift * 0.28;
  const sh = leaf.shadow.style;
  if (strength < 0.01) {
    sh.opacity = "0";
    return;
  }
  sh.opacity = "1";
  const reach = Math.abs(x);
  const dirDeg = x >= 0 ? 90 : 270;
  sh.width = `${(reach + spread).toFixed(1)}px`;
  sh.left = `${(x >= 0 ? leaf.pageWidth : leaf.pageWidth - reach - spread).toFixed(1)}px`;
  sh.background =
    `linear-gradient(${dirDeg}deg, rgba(${SHADE_RGB}, ${(strength * 0.55).toFixed(3)}) 0, ` +
    `rgba(${SHADE_RGB}, ${strength.toFixed(3)}) ${reach.toFixed(1)}px, ` +
    `rgba(${SHADE_RGB}, 0) ${(reach + spread).toFixed(1)}px)`;
}

function paintBases() {
  paintSide(basePage("left"), "left", flipper.leftBase);
  paintSide(basePage("right"), "right", flipper.rightBase);
}

function flipTick(now) {
  const dt = Math.min(0.05, Math.max(0, (now - flipper.last) / 1000));
  flipper.last = now;
  const leaves = flipper.leaves;
  // 空中纸越多翻得越快，连点时像捻着书页一路翻过去
  const speed = (1000 / FLIP_DURATION) * Math.min(2.4, 1 + 0.35 * (leaves.length - 1));
  const kv = 1 - Math.exp(-dt * 14);
  const ks = 1 - Math.exp(-dt * 9);
  for (const leaf of leaves) {
    leaf.s += (leaf.dir - leaf.s) * ks;
    if (leaf.delay > 0) {
      leaf.delay -= dt;
      continue;
    }
    leaf.v += (leaf.dir * speed - leaf.v) * kv;
    const p = leaf.p + leaf.v * dt;
    if (p <= 0 || p >= 1) leaf.v = 0;
    leaf.p = clamp01(p);
  }
  // 后面的纸不能越过前面的纸
  for (let i = 1; i < leaves.length; i += 1) {
    if (leaves[i].p > leaves[i - 1].p) leaves[i].p = leaves[i - 1].p;
  }
  let landed = false;
  while (leaves.length && leaves[0].dir > 0 && leaves[0].p >= 1) {
    const leaf = leaves.shift();
    flipper.leftBase = leaf.back;
    disposeLeaf(leaf);
    landed = true;
  }
  while (leaves.length && leaves[leaves.length - 1].dir < 0 && leaves[leaves.length - 1].p <= 0) {
    const leaf = leaves.pop();
    flipper.rightBase = leaf.front;
    disposeLeaf(leaf);
    landed = true;
  }
  if (landed) paintBases();
  for (const leaf of leaves) renderLeaf(leaf);
  flipper.raf = leaves.length ? requestAnimationFrame(flipTick) : 0;
}

function startFlipLoop() {
  if (flipper.raf) return;
  flipper.last = performance.now();
  flipper.raf = requestAnimationFrame(flipTick);
}

function setCurrent(index) {
  const sheet = bookState.sheets[index];
  if (!sheet) return;
  bookState.sheet = index;
  $("book-indicator").textContent = "第 " + (index * 2 + 1) + "–" + (index * 2 + 2) + " 页";
}

function applySheet(index) {
  flipper.leftBase = index;
  flipper.rightBase = index;
  paintBases();
  setCurrent(index);
}

function flipForward(delay = 0) {
  const leaves = flipper.leaves;
  const back = leaves.find((leaf) => leaf.dir < 0);
  if (back) {
    // 正在往回落的纸：抓住它重新翻过去
    back.dir = 1;
    back.delay = 0;
    setCurrent(back.back);
    startFlipLoop();
    return true;
  }
  const next = flipper.rightBase + 1;
  if (next >= bookState.sheets.length) return false;
  const leaf = createLeaf(flipper.rightBase, next, 0, 1, delay);
  leaves.push(leaf);
  flipper.rightBase = next;
  paintSide(basePage("right"), "right", next);
  setCurrent(next);
  startFlipLoop();
  return true;
}

function flipBackward(delay = 0) {
  const leaves = flipper.leaves;
  let fwd = null;
  for (let i = leaves.length - 1; i >= 0; i -= 1) {
    if (leaves[i].dir > 0) {
      fwd = leaves[i];
      break;
    }
  }
  if (fwd) {
    fwd.dir = -1;
    fwd.delay = 0;
    setCurrent(fwd.front);
    startFlipLoop();
    return true;
  }
  const prev = flipper.leftBase - 1;
  if (prev < 0) return false;
  const leaf = createLeaf(prev, flipper.leftBase, 1, -1, delay);
  leaves.unshift(leaf);
  flipper.leftBase = prev;
  paintSide(basePage("left"), "left", prev);
  setCurrent(prev);
  startFlipLoop();
  return true;
}

function showSheet(index, animate = false) {
  const total = bookState.sheets.length;
  if (!total) {
    renderEmptySheet();
    return;
  }
  const target = Math.max(0, Math.min(index, total - 1));
  if (!animate || !bookState.open || reducedMotion()) {
    clearFlip();
    applySheet(target);
    return;
  }
  const diff = target - bookState.sheet;
  if (!diff) return;
  if (Math.abs(diff) <= 4) {
    // 近处：一张张错开翻过去
    for (let i = 0; i < Math.abs(diff); i += 1) {
      if (diff > 0) flipForward(i * 0.09);
      else flipBackward(i * 0.09);
    }
    return;
  }
  // 远跳：先落定，再用一张纸直接翻到目标页
  const from = bookState.sheet;
  clearFlip();
  applySheet(from);
  if (diff > 0) {
    flipper.leaves.push(createLeaf(from, target, 0, 1));
    flipper.rightBase = target;
    paintSide(basePage("right"), "right", target);
  } else {
    flipper.leaves.push(createLeaf(target, from, 1, -1));
    flipper.leftBase = target;
    paintSide(basePage("left"), "left", target);
  }
  setCurrent(target);
  startFlipLoop();
}

// 封面背面印第一页：把左页当前内容抄到封面的背面去
function syncCoverBack() {
  const back = document.querySelector(".book-cover .cover-face.back");
  const left = basePage("left");
  if (!back || !left) return;
  back.innerHTML = "";
  const page = document.createElement("div");
  page.className = "book-page left";
  page.innerHTML = left.innerHTML;
  page.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
  back.appendChild(page);
}

function openBook() {
  if (bookState.open) return;
  clearTimeout(bookState.closeTimer);
  $("book").classList.remove("closing");
  syncCoverBack();
  bookState.open = true;
  $("book").classList.add("open");
  if (!bookState.sheets.length) renderEmptySheet();
  else showSheet(bookState.sheet);
}

function closeBook() {
  if (!bookState.open) return;
  showSheet(bookState.sheet);
  syncCoverBack();
  bookState.open = false;
  $("book").classList.add("closing");
  $("book").classList.remove("open");
  clearTimeout(bookState.closeTimer);
  bookState.closeTimer = setTimeout(() => {
    $("book").classList.remove("closing");
  }, 900);
}

function stepSheet(dir) {
  if (!bookState.open) {
    openBook();
    return;
  }
  const total = bookState.sheets.length;
  if (!total) return;
  let ok;
  if (reducedMotion()) {
    const next = bookState.sheet + dir;
    ok = next >= 0 && next < total;
    if (ok) showSheet(next);
  } else {
    ok = dir > 0 ? flipForward() : flipBackward();
  }
  if (!ok) toast(dir > 0 ? "已经是最后一张纸了" : "已经是第一张纸了");
}

async function searchBook() {
  const word = $("book-search").value.trim();
  const hitBox = $("book-search-hit");
  const searchId = ++bookState.searchId;
  hitBox.innerHTML = "";
  hitBox.classList.add("hidden");
  if (!word) return;

  try {
    const dateMatch = /^(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})$/.exec(word);
    if (dateMatch) {
      const [, year = todayStr().slice(0, 4), month, dayOfMonth] = dateMatch;
      const day = `${year}-${month.padStart(2, "0")}-${dayOfMonth.padStart(2, "0")}`;
      const date = new Date(`${day}T00:00:00Z`);
      if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== day) {
        hitBox.classList.remove("hidden");
        hitBox.append(el("span", "book-hit-empty", `「${word}」不是有效日期`));
        return;
      }

      const index = bookState.sheets.findIndex(sheet => sheet.rows.some(r => r.day === day));
      if (index < 0) {
        hitBox.classList.remove("hidden");
        hitBox.append(el("span", "book-hit-empty", `${day} 没有记录`));
        return;
      }
      showSheet(index, bookState.open);
      openBook();
      const matched = bookState.sheets[index].rows.filter(r => r.day === day);
      hitBox.classList.remove("hidden");
      hitBox.append(el("span", "book-hit-label", day + " · " + matched[0].text.slice(0, 40)));
      return;
    }

    const data = await bridge.apiGet("journal/search", { q: word });
    if (searchId !== bookState.searchId) return;
    const entries = data.entries || [];
    hitBox.classList.remove("hidden");
    if (!entries.length) hitBox.append(el("span", "book-hit-empty", "没有匹配的观察条目。"));
    for (const entry of entries) {
      const btn = el("button", "book-hit", entry.day + " · " + entry.text.slice(0, 24));
      btn.type = "button";
      btn.addEventListener("click", () => {
        const index = bookState.sheets.findIndex(sheet => sheet.rows.some(r => r.id === entry.id));
        if (index >= 0) { showSheet(index, bookState.open); openBook(); }
      });
      hitBox.append(btn);
    }
  } catch (error) {
    if (searchId !== bookState.searchId) return;
    toast((error && error.message) || "查询失败", true);
  }
}

let spreadEdit = null;

function addEditorRow(container, entry) {
  const field = el("div", "field");
  const header = el("div", "guide-row-head");
  const type = document.createElement("select");
  type.setAttribute("aria-label", "记录类型");
  for (const [value, label] of [["item","条目"],["struck","划掉"],["note","批注"]]) {
    type.add(new Option(label,value));
  }
  type.value = entry.kind;
  const remove = el("button", "act danger", "移除");
  remove.type = "button";
  remove.onclick = () => field.remove();
  header.append(type, el("span", "hint", entry.day || "新增"), remove);
  const input = document.createElement("textarea");
  input.rows = 2;
  input.value = entry.text;
  input.setAttribute("aria-label", "记录内容");
  if (entry.id) field.dataset.id = entry.id;
  field.append(header,input);
  container.append(field);
  return input;
}

function openPageEditor() {
  if (bookState.scope === "interaction") { toast("请先切换到「所有」再编辑书页。",true); return; }
  if (bookState.revision == null) return;
  const index = bookState.sheet;
  if (bookState.open) showSheet(index);
  const sheet = bookState.sheets[index] || {left:[],right:[],rows:[]};
  spreadEdit = {ids:sheet.rows.map(r => r.id), revision:bookState.revision};
  $("guide-dlg-title").textContent = "编辑第 " + (2*index+1) + "–" + (2*index+2) + " 页";
  for (const side of ["left","right"]) {
    const container = $("guide-" + side);
    container.replaceChildren();
    for (const entry of sheet[side]) addEditorRow(container,entry);
  }
  $("guide-err").textContent = "";
  $("guide-mask").classList.remove("hidden");
}

function closeGuideEditor() {
  $("guide-mask").classList.add("hidden");
  spreadEdit = null;
}

async function savePage() {
  if ($("guide-save").disabled || !spreadEdit) return;
  const entries = [];
  for (const field of $("guide-mask").querySelectorAll(".guide-editor-rows > .field")) {
    const kind = field.querySelector("select").value;
    const value = field.querySelector("textarea").value.trim();
    if (!value) { $("guide-err").textContent = "内容为空，请填写文字或移除这一行。"; return; }
    const lines = kind === "note" ? value.split(/\r?\n/).map(s => s.trim()).filter(Boolean) : [value];
    for (const [index,text] of lines.entries()) {
      entries.push({id:index === 0 && field.dataset.id ? Number(field.dataset.id) : null,kind,text});
    }
  }
  $("guide-save").disabled = true;
  try {
    const result = await bridge.apiPost("journal/save", {...spreadEdit,entries});
    closeGuideEditor();
    toast("已保存");
    await loadBook({focusId:result.ids?.[0]});
  } catch (error) {
    $("guide-err").textContent = error.message || "保存失败";
  } finally { $("guide-save").disabled = false; }
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
    const day = todayStr();
    const result = await bridge.apiPost("journal/write_today", { day });
    if (!result?.ok) {
      toast((result && result.message) || "这次没写出来", true);
      return;
    }
    toast(
      result.overwritten
        ? `今天的记录已重写（${result.items} 条）`
        : `今天的记录写好了（${result.items} 条）`,
    );
    await loadBook({ focusDay: result.day || day });
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
  $("book-edit").addEventListener("click", openPageEditor);
  for (const side of ["left","right"]) {
    $("guide-add-" + side).onclick = () => addEditorRow($("guide-" + side),{kind:"item",text:""}).focus();
  }
  $("book-review").addEventListener("click", askWriteToday);
  $("confirm-ok").addEventListener("click", runConfirm);
  $("confirm-cancel").addEventListener("click", closeConfirm);
  $("confirm-close").addEventListener("click", closeConfirm);
  $("confirm-mask").addEventListener("click", (event) => {
    if (event.target === $("confirm-mask")) closeConfirm();
  });
  $("book-stage").addEventListener("click", (event) => {
    // 翻页会同步重绘被点的那一页，冒泡到这里时 event.target 可能已脱离 DOM，
    // 所以用派发时记录的路径判断是不是点在书上
    if (event.composedPath().includes($("book"))) return;
    closeBook();
  });
  // 点一下翻一页，连点就连续翻；编辑走工具栏的「编辑」按钮
  $("book-spread").addEventListener("mousedown", (event) => {
    if (event.detail > 1) event.preventDefault(); // 连点时别把文字选中
  });
  $("book-spread").querySelectorAll(":scope > .book-page").forEach((el2) => {
    const dir = el2.classList.contains("left") ? -1 : 1;
    el2.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!bookState.open) {
        openBook();
        return;
      }
      if (dir < 0 && bookState.sheet === 0 && !flipper.leaves.length) {
        closeBook();
        return;
      }
      stepSheet(dir);
    });
  });
  $("book-search").addEventListener("input", () => {
    bookState.searchId += 1;
    $("book-search-hit").classList.add("hidden");
  });
  $("book-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
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
  $("f-tags-auto").addEventListener("click", autoClassifyTags);
  for (const id of ["f-scene", "f-bad", "f-good", "f-principle", "f-tags"]) {
    $(id).addEventListener("input", () => setTagAutoError(""));
  }
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

  const sizeKey = "speech-lessons.card-page-size";
  const sizeInput = $("card-page-size");
  try {
    const savedSize = localStorage.getItem(sizeKey);
    if (Array.from(sizeInput.options).some((option) => option.value === savedSize)) {
      sizeInput.value = savedSize;
      state.pageSize = Number(savedSize);
    }
  } catch {
    // The embedded view may have storage disabled.
  }
  sizeInput.addEventListener("change", () => {
    state.pageSize = Number(sizeInput.value);
    state.page = 1;
    try {
      localStorage.setItem(sizeKey, sizeInput.value);
    } catch {
      // Keep the selected size for the current view.
    }
    load();
  });
  $("card-sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    state.page = 1;
    load();
  });
  $("card-sort-direction").addEventListener("click", (event) => {
    const button = event.currentTarget;
    state.direction = state.direction === "asc" ? "desc" : "asc";
    state.page = 1;
    button.dataset.direction = state.direction;
    button.title = state.direction === "asc" ? "当前正序，点击切换倒序" : "当前倒序，点击切换正序";
    button.setAttribute("aria-label", button.title);
    load();
  });
  $("card-prev").addEventListener("click", () => {
    state.page -= 1;
    load({ scroll: true });
  });
  $("card-next").addEventListener("click", () => {
    state.page += 1;
    load({ scroll: true });
  });

  let searchTimer = null;
  $("q").addEventListener("input", (event) => {
    state.keyword = event.target.value.trim();
    state.page = 1;
    state.requestId += 1;
    $("card-prev").disabled = true;
    $("card-next").disabled = true;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, 220);
  });

  $("seg").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-status]");
    if (!btn || btn.classList.contains("on")) return;
    for (const b of $("seg").querySelectorAll("button")) b.classList.remove("on");
    btn.classList.add("on");
    state.status = btn.dataset.status;
    state.page = 1;
    clearTimeout(searchTimer);
    load();
  });

  await load();
  await loadBook();
}

main();
