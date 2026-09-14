/* nexus-studio 编排工作台前端（无构建、无框架）
 * 顶部三选项卡：#/auto 自动编排 | #/flow 流程编排 | #/test 模版测试（默认）
 * 约定：数据经 /api/v1/studio/*（响应包裹 {code,message,status,data}）；
 *       模版测试复用核心 API（/api/v1/launch + /chat + /sessions）；
 *       鉴权复用 NEXUS_API_KEY（X-API-Key 头，密钥与 /console 共享 localStorage）。
 */
"use strict";

/* ============================== 基础工具 ============================== */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function uuid() {
  return (crypto.randomUUID ? crypto.randomUUID()
          : "id-" + Date.now() + "-" + Math.random().toString(16).slice(2));
}

function toast(message, ok) {
  const el = document.createElement("div");
  el.className = "toast " + (ok ? "ok" : "err");
  el.textContent = message;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), ok ? 2800 : 4600);
}

/* ============================== API 封装 ============================== */

const state = {
  key: localStorage.getItem("nexus_console_key") || "",
};

function setApiStatus(cls) {
  const dot = $("#api-status");
  dot.className = "status-dot" + (cls ? " " + cls : "");
}

function authHeaders() {
  return state.key ? { "X-API-Key": state.key } : {};
}

async function api(path, { method = "GET", params, body } = {}) {
  let url = path;
  if (params) {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null));
    if (qs.toString()) url += "?" + qs.toString();
  }
  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers: Object.assign(
        { "Content-Type": "application/json" }, authHeaders()),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    setApiStatus("err");
    throw new Error("网络错误或服务不可达：" + e.message);
  }
  let payload = null;
  try { payload = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (resp.status === 401) {
    setApiStatus("err");
    throw new Error("API key 校验失败（请检查 NEXUS_API_KEY）");
  }
  if (!payload) {
    setApiStatus("err");
    throw new Error(`HTTP ${resp.status}（非 JSON 响应）`);
  }
  if (payload.code !== "0" || payload.status !== true) {
    if (resp.status >= 500) setApiStatus("err");
    throw new Error(payload.message || `HTTP ${resp.status}`);
  }
  setApiStatus("ok");
  return payload.data;
}

/** 消费 POST SSE 流（data: {...}\n\n 分帧），onEvent 收到逐个 JSON 事件。 */
async function streamSse(path, body, onEvent) {
  let resp;
  try {
    resp = await fetch(path, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" },
                             authHeaders()),
      body: JSON.stringify(body),
    });
  } catch (e) {
    setApiStatus("err");
    throw new Error("网络错误或服务不可达：" + e.message);
  }
  if (!resp.ok || !resp.body) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).message || msg; } catch (_) {}
    throw new Error(msg);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data:")) {
          try { onEvent(JSON.parse(line.slice(5))); } catch (_) {}
        }
      }
    }
  }
}

/* ============================== mermaid / js-yaml 按需加载 ============================== */

let _mermaidPromise = null;
let _mermaidSeq = 0;

function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = new Promise((resolve) => {
      const sources = [
        "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
        "https://unpkg.com/mermaid@11/dist/mermaid.min.js",
        "https://registry.npmmirror.com/mermaid/latest/files/dist/mermaid.min.js",
      ];
      let i = 0;
      const tryNext = () => {
        if (i >= sources.length) return resolve(null);
        const script = document.createElement("script");
        script.src = sources[i++];
        script.onload = () => {
          try {
            window.mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });
            resolve(window.mermaid);
          } catch (e) { resolve(null); }
        };
        script.onerror = tryNext;
        document.head.appendChild(script);
      };
      tryNext();
    });
  }
  return _mermaidPromise;
}

async function renderMermaid(container, code) {
  container.innerHTML = "";
  if (!code) return;
  const mermaid = await loadMermaid();
  if (!mermaid) {
    const pre = document.createElement("pre");
    pre.className = "code";
    pre.textContent = code;
    container.appendChild(pre);
    return;
  }
  try {
    const { svg } = await mermaid.render("mmd" + (++_mermaidSeq), code);
    container.innerHTML = svg;
  } catch (e) {
    const pre = document.createElement("pre");
    pre.className = "code";
    pre.textContent = code;
    container.appendChild(pre);
  }
}

let _yamlPromise = null;

function loadYaml() {
  if (!_yamlPromise) {
    _yamlPromise = new Promise((resolve) => {
      const sources = [
        "https://cdn.jsdelivr.net/npm/js-yaml@4/dist/js-yaml.min.js",
        "https://unpkg.com/js-yaml@4/dist/js-yaml.min.js",
        "https://registry.npmmirror.com/js-yaml/latest/files/dist/js-yaml.min.js",
      ];
      let i = 0;
      const tryNext = () => {
        if (i >= sources.length) return resolve(null);
        const script = document.createElement("script");
        script.src = sources[i++];
        script.onload = () => resolve(window.jsyaml || null);
        script.onerror = tryNext;
        document.head.appendChild(script);
      };
      tryNext();
    });
  }
  return _yamlPromise;
}

function dumpYaml(obj) {
  if (window.jsyaml) {
    return window.jsyaml.dump(obj, { noRefs: true, lineWidth: -1 });
  }
  return JSON.stringify(obj, null, 2);
}

async function parseYaml(text) {
  const y = await loadYaml();
  if (!y) throw new Error("js-yaml 加载失败（网络不可达），请使用 YAML 接口校验");
  return y.load(text);
}

/* ============================== 弹窗 ============================== */

function closeModal() {
  $("#modal-mask").hidden = true;
  $("#modal").innerHTML = "";
}

function openModal({ title, body, submitText = "保存", onSubmit }) {
  const mask = $("#modal-mask"), modal = $("#modal");
  modal.innerHTML = `
    <h3>${esc(title)}</h3>
    <form id="modal-form">${body}
      <div class="form-error" id="modal-error"></div>
      <div class="form-actions">
        <button type="button" class="btn" id="modal-cancel">取消</button>
        <button type="submit" class="btn btn-primary">${esc(submitText)}</button>
      </div>
    </form>`;
  mask.hidden = false;
  $("#modal-cancel").onclick = closeModal;
  mask.onclick = (e) => { if (e.target === mask) closeModal(); };
  $("#modal-form").onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    $$("#modal-form [name]", modal).forEach((el) => {
      if (el.type === "checkbox") values[el.name] = el.checked;
      else values[el.name] = el.value;
    });
    const btn = $("#modal-form button[type=submit]");
    btn.disabled = true;
    try {
      await onSubmit(values);
      closeModal();
    } catch (err) {
      $("#modal-error").textContent = err.message || String(err);
      btn.disabled = false;
    }
  };
}

function confirmDialog(text, onOk) {
  openModal({
    title: "确认操作",
    body: `<div class="field"><label>${esc(text)}</label></div>`,
    submitText: "确认",
    onSubmit: async () => { await onOk(); },
  });
}

/* ============================== 路由 ============================== */

const routes = {
  "/auto": { title: "自动编排", render: renderAuto },
  "/flow": { title: "流程编排", render: renderFlow },
  "/test": { title: "模版测试", render: renderTest },
  "/plugins": { title: "系统插件", render: renderSystem },
};

function currentHashPath() {
  const hash = location.hash.replace(/^#/, "") || "/test";
  if (hash === "/" || hash === "") return "/test";
  return hash;
}

async function route() {
  const path = currentHashPath();
  const base = "/" + (path.split("/")[1] || "test");
  const entry = routes[base] || routes["/test"];
  $$("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === base.slice(1)));
  document.title = `nexus-studio · ${entry.title}`;
  const main = $("#main");
  main.innerHTML = `<div class="panel loading">加载中…</div>`;
  try {
    await entry.render(path);
  } catch (e) {
    main.innerHTML = `<div class="panel"><div class="notice warn">${esc(e.message)}</div></div>`;
  }
}

/* ============================== 模版测试 ============================== */

/* 各应用的默认 task_info（选中模版时预填；与各 app route 声明的 task_info
 * 契约一致——install/repair 同形，customer/xianyu 渠道侧字段。值全为字符串
 * 走键值行；含数组（available_slots）自动切 JSON 模式。研究类应用不消费
 * task_info，不预填） */
const DEFAULT_TASK_INFO = {
  install_booking_agent: {
    user_name: "王先生",
    order_id: "SO-20260912-0031",
    product_name: "佩尼尔书桌（白色 120×60cm）",
    address: "北京市朝阳区望京街道 8 号院 3 单元 502",
    available_slots: [
      "2026-09-15 09:00-11:00",
      "2026-09-16 14:00-16:00",
      "2026-09-17 09:00-11:00",
    ],
  },
  repair_booking_agent: {
    user_name: "李女士",
    order_id: "SO-20260910-0087",
    product_name: "变频滚筒洗衣机 10kg（不脱水）",
    address: "北京市海淀区中关村大街 27 号 2 单元 1103",
    available_slots: [
      "2026-09-15 14:00-16:00",
      "2026-09-16 09:00-11:00",
    ],
  },
  customer_agent: {
    channel: "闲鱼",
    account_id: "xy_seller_2024",
  },
  xianyu_agent: {
    account_id: "xy_seller_2024",
    product_name: "iPhone 15 256G 白色",
    price_floor: "5200",
  },
};

const testState = {
  patterns: [], code: "", detail: null,
  taskRows: [{ k: "", v: "" }], jsonMode: false, jsonText: "",
  taskTouched: false,   // 用户编辑过 task_info 后不再被默认值覆盖
  session: null, sending: false, msgCount: 0,
  log: [],              // 聊天记录模型：切页签只重建 DOM 不丢数据（新会话清空）
};

const CHAT_HINT = "发起会话后开始多轮对话测试；事件逐行打印（节点/工具/扇出），回复文本流式输出。";

function resetChatLog() {
  testState.log = [{ kind: "system", text: CHAT_HINT }];
}

function prefillTaskInfo(code) {
  const def = DEFAULT_TASK_INFO[code];
  if (!def || testState.taskTouched) return;
  if (Object.values(def).every((v) => typeof v === "string")) {
    testState.taskRows = Object.entries(def).map(([k, v]) => ({ k, v }));
    testState.jsonMode = false;
  } else {
    testState.jsonMode = true;
    testState.jsonText = JSON.stringify(def, null, 2);
  }
}

async function renderTest() {
  testState.patterns = (await api("/api/v1/studio/patterns")).patterns;
  $("#main").innerHTML = `
    <div class="page-root">
      <div class="page-body split">
        <aside class="side-panel panel">
          <h3>选择模版</h3>
          <input class="list-search" id="test-search" placeholder="搜索 code / 名称">
          <div class="pattern-list" id="test-list"></div>
          <div id="test-detail" style="margin-top:12px"></div>
          <div id="test-taskinfo" style="margin-top:12px"></div>
        </aside>
        <section class="work-panel panel chat-panel">
          <div class="chat-head">
            <h3 style="margin:0">会话</h3>
            <span class="session-info" id="session-info">${
              testState.session
                ? `${testState.session.id} · 第 ${testState.session.turn} 轮`
                : "未发起会话"}</span>
            <button class="btn btn-sm" id="btn-new-session" style="margin-left:auto">新会话</button>
          </div>
          <div class="chat-log" id="chat-log"></div>
          <div class="chat-input">
            <textarea id="chat-text" placeholder="输入用户消息，Enter 发送（Shift+Enter 换行）"
              ${testState.session ? "" : "disabled"}></textarea>
            <button class="btn btn-primary" id="btn-send" ${testState.session && !testState.sending ? "" : "disabled"}>发送</button>
          </div>
        </section>
      </div>
    </div>`;
  if (testState.log.length === 0) resetChatLog();
  renderChatLog();
  $("#test-search").oninput = (e) => renderTestList(e.target.value.trim());
  $("#btn-new-session").onclick = () => {
    testState.session = null;
    resetChatLog();
    renderTest();
  };
  $("#btn-send").onclick = sendChat;
  $("#chat-text").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  };
  renderTestList("");
  if (testState.code) selectTestPattern(testState.code);
}

function renderTestList(query) {
  const items = testState.patterns.filter((p) =>
    !query || (p.code + p.name + (p.description || "")).includes(query));
  $("#test-list").innerHTML = items.map((p) => `
    <div class="pattern-item ${p.code === testState.code ? "selected" : ""}"
         data-code="${esc(p.code)}">
      <div class="p-title">${esc(p.name || p.code)}
        <span class="badge ${p.source}">${p.source}</span>
        <span class="badge code">${esc(p.pattern_type || "?")}</span>
        ${p.load_error ? '<span class="badge err">装载失败</span>' : ""}
      </div>
      <div class="p-code">${esc(p.code)}</div>
      ${p.description ? `<div class="p-desc">${esc(p.description)}</div>` : ""}
    </div>`).join("") || '<div class="notice warn">无匹配模版</div>';
  $$("#test-list .pattern-item").forEach((el) =>
    el.onclick = () => selectTestPattern(el.dataset.code));
}

async function selectTestPattern(code) {
  testState.code = code;
  renderTestList($("#test-search").value.trim());
  const box = $("#test-detail");
  box.innerHTML = '<div class="loading">加载详情…</div>';
  try {
    testState.detail = await api(`/api/v1/studio/patterns/${encodeURIComponent(code)}`);
  } catch (e) {
    box.innerHTML = `<div class="notice warn">${esc(e.message)}</div>`;
    return;
  }
  const meta = testState.detail.meta;
  box.innerHTML = `
    <h3>结构图 <span class="sub">${esc(meta.code)} · ${meta.node_count} 节点</span></h3>
    <div class="mermaid-box" id="test-mermaid"></div>`;
  renderMermaid($("#test-mermaid"), testState.detail.mermaid);
  prefillTaskInfo(code);
  renderTaskInfoEditor();
}

function renderTaskInfoEditor() {
  if (!testState.code) return;
  const box = $("#test-taskinfo");
  box.innerHTML = `
    <h3>任务信息（task_info）</h3>
    <div class="kv-rows" id="kv-rows"></div>
    <div style="display:flex; gap:8px; margin-top:8px">
      <button class="btn btn-sm" id="kv-add">+ 加一行</button>
      <button class="btn btn-sm" id="kv-json">${testState.jsonMode ? "键值模式" : "JSON 模式"}</button>
    </div>
    <div id="kv-json-wrap" style="margin-top:8px; ${testState.jsonMode ? "" : "display:none"}">
      <textarea class="code" id="kv-json-text" style="min-height:100px">${esc(testState.jsonText || '{\n  "key": "value"\n}')}</textarea>
    </div>
    <button class="btn btn-primary" id="btn-launch" style="width:100%; margin-top:10px">
      发起会话${testState.session ? "（重新发起）" : ""}
    </button>`;
  const rowsBox = $("#kv-rows");
  const drawRows = () => {
    rowsBox.innerHTML = testState.taskRows.map((row, i) => `
      <div class="kv-row">
        <input placeholder="key" value="${esc(row.k)}" data-i="${i}" data-f="k">
        <input placeholder="value" value="${esc(row.v)}" data-i="${i}" data-f="v">
        <button class="btn btn-ghost btn-sm" data-i="${i}" data-del="1">✕</button>
      </div>`).join("");
    $$("#kv-rows input").forEach((el) =>
      el.oninput = () => {
        testState.taskRows[+el.dataset.i][el.dataset.f] = el.value;
        testState.taskTouched = true;
      });
    $$("#kv-rows [data-del]").forEach((el) =>
      el.onclick = () => {
        testState.taskRows.splice(+el.dataset.i, 1);
        drawRows();
      });
  };
  drawRows();
  $("#kv-add").onclick = () => { testState.taskRows.push({ k: "", v: "" }); drawRows(); };
  $("#kv-json").onclick = () => {
    testState.jsonMode = !testState.jsonMode;
    renderTaskInfoEditor();
  };
  if (testState.jsonMode) {
    $("#kv-json-text").oninput = (e) => {
      testState.jsonText = e.target.value;
      testState.taskTouched = true;
    };
  }
  $("#btn-launch").onclick = async () => {
    let taskInfo = {};
    try {
      if (testState.jsonMode) {
        taskInfo = JSON.parse($("#kv-json-text").value || "{}");
      } else {
        testState.taskRows.forEach(({ k, v }) => {
          if (k.trim()) taskInfo[k.trim()] = v;
        });
      }
    } catch (e) {
      toast("task_info JSON 非法：" + e.message, false);
      return;
    }
    const sessionId = "studio-" + uuid().slice(0, 13);
    try {
      await api("/api/v1/launch", { method: "POST", body: {
        request_id: uuid(), session_id: sessionId,
        pattern_code: testState.code, task_info: taskInfo,
      } });
    } catch (e) {
      toast("发起会话失败：" + e.message, false);
      return;
    }
    testState.session = { id: sessionId, turn: 0 };
    testState.msgCount = 0;
    resetChatLog();
    toast(`会话已发起: ${sessionId}`, true);
    renderTest();
  };
}

/* trace 事件 → 状态行标签（data 键名与引擎发射点一一对应） */
function traceLabel(t) {
  const d = t.data || {};
  const n = t.node_code ? ` · ${t.node_code}` : "";
  const b = t.branch_id ? `（${t.branch_id}）` : "";
  switch (t.event) {
    case "node_start": return `▶ 执行节点${n}`;
    case "node_end": return `✓ 节点完成${n}`;
    case "node_jump": return `→ 节点跳转 ${d.from_node || "?"} ⇒ ${t.node_code}`;
    case "graph_compile": return "图运行开始";
    case "graph_wait": return `⏸ 等待用户输入${n}`;
    case "graph_resume": return `图从挂起恢复${n}`;
    case "graph_done": return `图结束（${d.reason || ""}）`;
    case "fanout_start": return `⑂ 扇出 ${Array.isArray(d.branch_ids) ? d.branch_ids.length : ""} 实例${n}`;
    case "branch_start": return `⑂ 分支执行 ${t.branch_id}`;
    case "branch_end": return `⑂ 分支完成 ${t.branch_id}`;
    case "fanout_join": return `⑂ 汇聚${n}`;
    case "tool_call": {
      const args = d.args ? JSON.stringify(d.args) : "";
      const short = args.length > 60 ? args.slice(0, 60) + "…" : args;
      return `🔧 调用工具 ${d.tool_name || ""}${b} ${short}`.trim();
    }
    case "tool_result": {
      const r = String(d.result || "");
      const shown = r.length > 80 ? r.slice(0, 80) + "…" : r;
      return `🔧 ${d.tool_name || ""}${d.synthetic ? "（拦截回填）" : ""}${b} → ${shown}`;
    }
    case "conversation_end": return "会话结束（终态节点）";
    case "turn_error": return "⚠ 对话处理异常";
    default: return t.event;
  }
}

async function sendChat() {
  const box = $("#chat-text");
  const query = box.value.trim();
  if (!query || !testState.session || testState.sending) return;
  testState.sending = true;
  $("#btn-send").disabled = true;
  box.value = "";
  appendMsg("user", query);

  // 模型化的流式助手条目：事件逐行 + 文本流式 + 过程轮折叠都写入
  // testState.log（切页签重渲染后仍能续画，聊天记录不丢）
  const entry = { kind: "assistant", text: "", events: [], rounds: [],
                  streaming: true, error: false };
  testState.log.push(entry);
  chatEntryEl(entry);
  chatScroll();

  let buf = "";        // 当前轮乐观转发的累计 delta（done 时被权威文本替换）
  let roundIdx = 0;
  let sawError = false;
  try {
    await streamSse("/api/v1/chat/stream", {
      request_id: uuid(), session_id: testState.session.id, query,
    }, (ev) => {
      if (ev.kind === "delta") {
        buf += ev.text || "";
        entry.text = buf;
        setChatText(entry, buf);
      } else if (ev.kind === "round") {
        // 乐观转发语义：非最终轮的文本属于过程（如工具调用轮），有内容才
        // 折叠成段——空轮不刷屏（工具活动由事件区的 trace 行呈现）
        const ri = ev.round_info || {};
        if (ri.outcome && ri.outcome !== "final" && buf) {
          roundIdx += 1;
          const label = `⟨过程 · 第 ${roundIdx} 轮（${ri.outcome}）⟩ ${buf}`;
          entry.rounds.push(label);
          const el = chatEntryEl(entry);
          const seg = document.createElement("div");
          seg.className = "msg-round";
          seg.textContent = label;
          el.insertBefore(seg, el.querySelector(".msg-text"));
          buf = "";
          entry.text = "";
          setChatText(entry, "");
        }
      } else if (ev.kind === "trace") {
        // 事件依次打印（持久）：每条 trace 一行，工具调用/返回突出显示
        const trace = ev.trace || {};
        const label = traceLabel(trace);
        if (label) {
          const cls = "ev"
            + (trace.event === "tool_call" || trace.event === "tool_result"
              ? " ev-tool" : "")
            + (trace.event === "turn_error" ? " ev-err" : "");
          entry.events.push({ cls, text: label });
          const el = chatEntryEl(entry);
          const eventsBox = el.querySelector(".msg-events");
          if (eventsBox) {
            const line = document.createElement("div");
            line.className = cls;
            line.textContent = label;
            line.title = label;
            eventsBox.appendChild(line);
            eventsBox.scrollTop = eventsBox.scrollHeight;
          }
          chatScroll();
          if (trace.event === "turn_error") sawError = true;
        }
      } else if (ev.kind === "done") {
        buf = (ev.result && ev.result.text) || buf || "（空回复）";
        entry.text = buf;
        entry.streaming = false;
        if (sawError) entry.error = true;
        repaintChatEntry(entry);
      } else if (ev.kind === "error") {
        sawError = true;
        entry.text = ev.message || "对话处理异常，请稍后重试";
        entry.streaming = false;
        entry.error = true;
        repaintChatEntry(entry);
      }
    });
    if (!sawError) {
      testState.session.turn += 1;
      appendTrace(testState.session.id);
    }
  } catch (e) {
    entry.streaming = false;
    entry.error = true;
    entry.text = e.message;
    repaintChatEntry(entry);
  }
  testState.sending = false;
  const sendBtn = $("#btn-send");
  if (sendBtn) sendBtn.disabled = false;
  const input = $("#chat-text");
  if (input) { input.disabled = false; input.focus(); }
}

/* -------- 聊天记录渲染（模型 testState.log → DOM，切页签可重建） -------- */

function buildChatEntryEl(entry) {
  if (entry.kind === "trace") {
    const details = document.createElement("details");
    details.className = "trace";
    details.innerHTML =
      "<summary>审计轨迹（本轮消息级）</summary><div>加载中…</div>";
    wireTraceDetails(details, entry.sessionId);
    entry._el = details;
    return details;
  }
  if (entry.kind !== "assistant") {
    const el = document.createElement("div");
    el.className = "msg " + (entry.kind === "user" ? "user" : "system");
    el.textContent = entry.text;
    entry._el = el;
    return el;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg assistant"
    + (entry.streaming ? " streaming" : "")
    + (entry.error ? " error" : "");
  const eventsBox = document.createElement("div");
  eventsBox.className = "msg-events";
  for (const e of entry.events) {
    const line = document.createElement("div");
    line.className = e.cls;
    line.textContent = e.text;
    line.title = e.text;
    eventsBox.appendChild(line);
  }
  wrap.appendChild(eventsBox);
  for (const label of entry.rounds) {
    const seg = document.createElement("div");
    seg.className = "msg-round";
    seg.textContent = label;
    wrap.appendChild(seg);
  }
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  textEl.textContent = entry.text;
  wrap.appendChild(textEl);
  entry._el = wrap;
  return wrap;
}

/** 取条目当前 DOM 节点；被重渲染冲掉（页签切换）时重建并接回日志末尾。 */
function chatEntryEl(entry) {
  if (!(entry._el && entry._el.isConnected)) {
    const el = buildChatEntryEl(entry);
    const log = $("#chat-log");
    if (log) log.appendChild(el);
  }
  return entry._el;
}

function renderChatLog() {
  const log = $("#chat-log");
  if (!log) return;
  log.innerHTML = "";
  for (const entry of testState.log) log.appendChild(buildChatEntryEl(entry));
  log.scrollTop = log.scrollHeight;
}

function chatScroll() {
  const log = $("#chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function setChatText(entry, text) {
  const el = chatEntryEl(entry);
  const textEl = el.querySelector(".msg-text");
  if (textEl) textEl.textContent = text;
  chatScroll();
}

/** 状态变化（done/error）后整体重画该条目。 */
function repaintChatEntry(entry) {
  const old = entry._el;
  const fresh = buildChatEntryEl(entry);
  if (old && old.isConnected) old.replaceWith(fresh);
  else {
    const log = $("#chat-log");
    if (log) log.appendChild(fresh);  // 尚未挂载（异常路径）→ 接回日志末尾
  }
  chatScroll();
}

function appendMsg(role, text) {
  const entry = { kind: role === "user" ? "user" : "system", text };
  testState.log.push(entry);
  chatEntryEl(entry);
  chatScroll();
  return entry;
}

function appendTrace(sessionId) {
  const entry = { kind: "trace", sessionId };
  testState.log.push(entry);
  chatEntryEl(entry);
  chatScroll();
  updateSessionInfo();
}

function wireTraceDetails(details, sessionId) {
  details.ontoggle = async () => {
    if (!details.open || details.dataset.loaded) return;
    try {
      const data = await api(`/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`);
      const rows = (data.messages || []).map((m) => `
        <tr>
          <td class="mono">${esc(m.role)}</td>
          <td class="mono">${esc(m.stage || "-")}</td>
          <td>${esc((m.content || "").slice(0, 120))}</td>
          <td class="mono">${esc(m.action && m.action.type ? m.action.type : "")}</td>
        </tr>`).join("");
      details.querySelector("div").innerHTML = `
        <table><thead><tr><th>role</th><th>stage</th><th>content</th><th>action</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4">无记录</td></tr>'}</tbody></table>`;
      details.dataset.loaded = "1";
    } catch (e) {
      details.querySelector("div").textContent = e.message;
    }
  };
}

function updateSessionInfo() {
  if (!testState.session) return;
  const info = $("#session-info");
  if (info) {
    info.textContent =
      `${testState.session.id} · 第 ${testState.session.turn} 轮`;
  }
}

/* ============================== 自动编排 ============================== */

const autoState = {
  result: null, generating: false, tab: "explanation", autoApply: true,
  stream: null,  // 生成过程模型 {status, steps:[], text}：切页签重渲染不丢
  form: { name: "", code: "", background: "", features: "", examples: "", extra: "" },
};

async function renderAuto() {
  const f = autoState.form;
  $("#main").innerHTML = `
    <div class="auto-layout">
      <section class="auto-form panel">
        <h3>生成需求</h3>
        <div class="field">
          <label>模版名称 / code <span class="sub">（可选，留空由 agent 命名）</span></label>
          <div style="display:flex; gap:8px">
            <input type="text" id="gen-name" placeholder="名称，如 门店预约助手" value="${esc(f.name)}">
            <input type="text" id="gen-code" placeholder="code，如 shop_booking" value="${esc(f.code)}">
          </div>
        </div>
        <div class="field">
          <label class="req">流程背景</label>
          <textarea id="gen-background" placeholder="业务背景、目标用户、使用场景、渠道…">${esc(f.background)}</textarea>
        </div>
        <div class="field">
          <label class="req">功能清单（一行一个功能点）</label>
          <textarea id="gen-features" placeholder="咨询商品信息&#10;预约到店时间&#10;查询预约状态">${esc(f.features)}</textarea>
        </div>
        <div class="field">
          <label>实现案例（参考对话样例）</label>
          <textarea id="gen-examples" placeholder="用户：你们几点营业？&#10;助手：工作日 9:00-18:00…">${esc(f.examples)}</textarea>
        </div>
        <div class="field">
          <label>补充要求</label>
          <textarea id="gen-extra" placeholder="风格/约束/特殊逻辑…">${esc(f.extra)}</textarea>
        </div>
        <div class="hint" style="margin-bottom:12px">生成引擎：本地 Claude Code（claude -p · 只读探索白名单 Read/Grep/Glob/LS · 当前项目路径执行，产物经围栏文本返回、不经子进程落盘），执行步骤实时流式回传。</div>
        <label class="checkbox-row" style="margin-bottom:12px">
          <input type="checkbox" id="gen-auto-apply" ${autoState.autoApply ? "checked" : ""}>
          校验通过后自动应用（落盘 + 注册）
        </label>
        <button class="btn btn-primary" id="btn-generate" style="width:100%"
          ${autoState.generating ? "disabled" : ""}>生成</button>
        <div class="gen-status" id="gen-status"></div>
      </section>
      <section class="auto-result panel" id="auto-result">
        ${autoState.result ? renderAutoResult()
          : (autoState.stream ? "" : `<div class="notice info">填写左侧表单后点击「生成」：agent 将产出 pattern YAML（以及必要时的自定义插件代码），预览确认后应用。</div>`)}
      </section>
    </div>`;
  // 表单内容写回模型：切页签重渲染不丢
  for (const key of ["name", "code", "background", "features", "examples", "extra"]) {
    const el = $("#gen-" + key);
    if (el) el.oninput = () => { autoState.form[key] = el.value; };
  }
  $("#gen-auto-apply").onchange = (e) => { autoState.autoApply = e.target.checked; };
  $("#btn-generate").onclick = runGenerate;
  if (autoState.result) wireAutoResult();
  else if (autoState.stream) renderAutoStream();
}

/** 由模型（autoState.stream）重建生成过程视图：状态行 + 步骤卡片 + 原始流。 */
function renderAutoStream() {
  const box = $("#auto-result");
  const s = autoState.stream;
  if (!box || !s) return;
  box.innerHTML = `
    <div class="gen-status">${autoState.generating ? '<span class="spin"></span>' : ""}<span id="gen-status-text"></span></div>
    <div class="gen-steps" id="gen-steps"></div>
    <pre class="stream-out" id="gen-stream"></pre>`;
  $("#gen-status-text").textContent = s.status;
  for (const ev of s.steps) appendGenStep(ev);
  const streamEl = $("#gen-stream");
  streamEl.textContent = s.text;
  streamEl.scrollTop = streamEl.scrollHeight;
}

/** Claude Code 执行步骤：每次工具调用一张卡片，返回嵌在对应卡片内。
 * run → 新卡片（进行中转圈）；result → 按 id 找卡片落结果（✓/✕），
 * 点卡片头可展开/收起完整返回摘要。 */
function appendGenStep(ev) {
  const box = $("#gen-steps");
  if (!box) return;
  const scroll = () => { box.scrollTop = box.scrollHeight; };
  const buildCard = () => {
    const card = document.createElement("div");
    card.className = "step-card running";
    card.innerHTML = `
      <div class="step-head">
        <span class="step-icon">🔧</span>
        <span class="step-name">${esc(ev.name || "tool")}</span>
        <span class="step-detail" title="${esc(ev.detail || "")}">${esc(ev.detail || "")}</span>
        <span class="step-state"><span class="spin"></span></span>
      </div>
      <div class="step-body"></div>`;
    card.querySelector(".step-head").onclick = () =>
      card.classList.toggle("open");
    return card;
  };

  if (ev.phase === "run") {
    const card = buildCard();
    if (ev.id) card.dataset.stepId = ev.id;
    box.appendChild(card);
    scroll();
    return;
  }

  // phase === "result"：嵌回对应调用卡片；配不上（如会话恢复）单独成卡
  const card = (ev.id
    && $$("#gen-steps .step-card").find((c) => c.dataset.stepId === ev.id))
    || buildCard();
  if (!card.isConnected) box.appendChild(card);
  card.classList.remove("running");
  card.classList.add(ev.is_error ? "error" : "done");
  card.querySelector(".step-state").innerHTML =
    ev.is_error ? "✕" : "✓";
  card.querySelector(".step-body").textContent = ev.detail || "(空)";
  scroll();
}

async function runGenerate() {
  if (autoState.generating) return;
  const f = autoState.form;
  const body = {
    background: f.background.trim(),
    features: f.features.trim(),
    examples: f.examples.trim(),
    extra: f.extra.trim(),
    name: f.name.trim(),
    code: f.code.trim(),
  };
  if (!body.background || !body.features) {
    toast("流程背景与功能清单为必填项", false);
    return;
  }

  autoState.generating = true;
  autoState.result = null;
  autoState.tab = "explanation";
  autoState.stream = { status: "准备生成…", steps: [], text: "" };
  $("#btn-generate").disabled = true;
  renderAutoStream();
  try {
    await streamSse("/api/v1/studio/generate", body, (ev) => {
      // 一律先写模型（autoState.stream），再尽力刷 DOM——切到其它页签时
      // 元素不在，回来自动重渲染续画，过程不丢
      if (ev.kind === "status") {
        autoState.stream.status = ev.message;
        const t = $("#gen-status-text");
        if (t) t.textContent = ev.message;
      } else if (ev.kind === "step") {
        autoState.stream.steps.push(ev);
        appendGenStep(ev);
      } else if (ev.kind === "delta") {
        autoState.stream.text += ev.text;
        const s = $("#gen-stream");
        if (s) {
          s.append(ev.text.replace(/\u0000/g, ""));
          s.scrollTop = s.scrollHeight;
        }
      } else if (ev.kind === "result") {
        autoState.result = ev.result;
        if (ev.result.ok && autoState.autoApply) {
          applyResult(ev.result);
        }
      } else if (ev.kind === "error") {
        toast("生成失败：" + ev.message, false);
        autoState.stream.status = "失败：" + ev.message;
        const t = $("#gen-status-text");
        if (t) t.textContent = "失败：" + ev.message;
      }
    });
  } catch (e) {
    toast(e.message, false);
    autoState.stream.status = "失败：" + e.message;
  }
  autoState.generating = false;
  const btn = $("#btn-generate");
  if (btn) btn.disabled = false;
  if (autoState.result) {
    autoState.stream = null;  // 成功产出 → 结果视图接管
    const result = $("#auto-result");
    if (result) {
      result.innerHTML = renderAutoResult();
      wireAutoResult();
    }
  } else {
    // 无结果（失败/空手而归）：保留过程现场（状态行去转圈）
    const spin = $("#auto-result .gen-status .spin");
    if (spin) spin.remove();
  }
}

function resultBadge(r) {
  return r.ok
    ? '<span class="badge ok">校验通过</span>'
    : '<span class="badge err">校验未通过</span>';
}

function renderAutoResult() {
  const r = autoState.result;
  const pluginCount = (r.plugins || []).length;
  const tabs = [
    ["explanation", "说明"],
    ["graph", "结构图"],
    ["yaml", "YAML"],
    ["plugins", `插件代码${pluginCount ? "(" + pluginCount + ")" : ""}`],
  ];
  return `
    <div class="result-head">
      ${resultBadge(r)}
      ${r.meta ? `<span class="badge console">${esc(r.meta.code)}</span>
        <span class="badge code">${esc(r.meta.pattern_type)} · ${r.meta.node_count} 节点</span>` : ""}
      <span style="flex:1"></span>
      <button class="btn btn-sm" id="ar-regen">重新生成</button>
      <button class="btn btn-sm" id="ar-edit">去流程编排微调</button>
      <button class="btn btn-primary btn-sm" id="ar-apply" ${r.ok ? "" : "disabled"}>应用（落盘 + 注册）</button>
    </div>
    ${(r.pattern_warnings || []).length ? `<div class="notice warn">工具软警告（不阻塞应用，新工具注册后自动生效）：${esc(r.pattern_warnings.join("；"))}</div>` : ""}
    ${(r.pattern_errors || []).length ? `<ul class="error-list">${r.pattern_errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    ${(r.plugins || []).filter((p) => p.error).length ? `<ul class="error-list">${r.plugins.filter((p) => p.error).map((p) => `<li>插件 ${esc(p.filename || "?")}：${esc(p.error)}</li>`).join("")}</ul>` : ""}
    <div class="subtabs">${tabs.map(([id, label]) =>
      `<button data-tab="${id}" class="${autoState.tab === id ? "active" : ""}">${label}</button>`).join("")}
    </div>
    <div class="result-tabs-body" id="ar-body"></div>`;
}

function wireAutoResult() {
  const r = autoState.result;
  $$("#auto-result .subtabs button").forEach((btn) =>
    btn.onclick = () => { autoState.tab = btn.dataset.tab; renderAutoTab(); });
  $("#ar-regen").onclick = runGenerate;
  $("#ar-apply").onclick = () => applyResult(r);
  $("#ar-edit").onclick = () => {
    if (!r.yaml) return toast("没有可编辑的 YAML", false);
    sessionStorage.setItem("studio_open_yaml", r.yaml);
    location.hash = r.meta && r.meta.code
      ? `#/flow/${encodeURIComponent(r.meta.code)}`
      : "#/flow/new";
  };
  renderAutoTab();
}

function renderAutoTab() {
  const r = autoState.result;
  $$("#auto-result .subtabs button").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.tab === autoState.tab));
  const body = $("#ar-body");
  if (autoState.tab === "explanation") {
    body.innerHTML = `<div class="explanation">${esc(r.explanation || "（无说明文字）")}</div>`;
  } else if (autoState.tab === "graph") {
    body.innerHTML = `<div class="mermaid-box" id="ar-mermaid"></div>`;
    renderMermaid($("#ar-mermaid"), r.mermaid);
  } else if (autoState.tab === "yaml") {
    body.innerHTML = `<pre class="code">${esc(r.yaml || "（未解析出 pattern YAML）")}</pre>`;
  } else if (autoState.tab === "plugins") {
    const plugins = r.plugins || [];
    body.innerHTML = plugins.length ? plugins.map((p, i) => `
      <div class="plugin-card">
        <div class="plugin-head">
          <span>${esc(p.filename || "?")}</span>
          ${p.imported ? '<span class="badge ok">导入验证通过</span>' : '<span class="badge err">验证失败</span>'}
          ${(p.declarations || []).map((d) =>
            `<span class="badge code">${esc(d.kind)}:${esc(d.code)}</span>`).join("")}
        </div>
        <pre class="code">${esc(pluginSource(r, p.stem))}</pre>
      </div>`).join("")
      : '<div class="notice info">本次生成未产出插件（优先复用了已注册插件）。</div>';
  }
}

function pluginBlocks(r) {
  if (r._pluginBlocks) return r._pluginBlocks;
  const chunks = String(r.raw || "").split(/```/);
  const out = [];
  for (let i = 1; i < chunks.length; i += 2) {
    const block = chunks[i].replace(/^[A-Za-z0-9_+-]*\r?\n/, "");
    if (!block.includes("plugin_registry.register")) continue;
    let stem = "";
    const fm = block.match(/^\s*#\s*studio-plugin:\s*file=([A-Za-z0-9_.-]+)/i);
    if (fm) stem = fm[1].replace(/\.py$/, "").toLowerCase();
    if (!stem) {
      const rm = block.match(
        /plugin_registry\.register\(\s*['"]([a-z_]+)['"]\s*,\s*['"]([A-Za-z0-9_-]+)['"]/);
      if (rm) stem = rm[2].toLowerCase();
    }
    out.push({ stem, code: block });
  }
  r._pluginBlocks = out;
  return out;
}

function pluginSource(r, stem) {
  const hit = pluginBlocks(r).find((b) => b.stem === stem);
  return hit ? hit.code : "#（原始输出中未找到该插件代码块）";
}

async function applyResult(r) {
  if (!r.ok || !r.yaml) { toast("校验未通过，不能应用", false); return; }
  const plugins = (r.plugins || [])
    .filter((p) => p.imported && p.stem)
    .map((p) => ({ filename: p.filename, code: pluginSource(r, p.stem) }));
  try {
    const data = await api("/api/v1/studio/apply", { method: "POST", body: {
      yaml: r.yaml, plugins,
    } });
    const n = (data.warnings || []).length;
    toast(n ? `${data.message || "已应用"}（含 ${n} 项工具软警告，新工具注册后生效）`
            : (data.message || "已应用"), true);
  } catch (e) {
    toast("应用失败：" + e.message, false);
  }
}

/* ============================== 流程编排 ============================== */

const flowState = {
  patterns: [], catalog: null, code: "", draft: null,
  selectedNode: "", tab: "nodes", localDraft: null, validating: false,
};

async function renderFlow(path) {
  const parts = path.split("/"); // ["", "flow", code?]
  const codeArg = parts.length > 2 ? decodeURIComponent(parts[2]) : "";
  if (!flowState.catalog) {
    flowState.catalog = await api("/api/v1/studio/catalog");
  }
  flowState.patterns = (await api("/api/v1/studio/patterns")).patterns;

  $("#main").innerHTML = `
    <div class="page-root">
      <div class="page-body split">
        <aside class="side-panel panel">
          <h3>Pattern 列表</h3>
          <input class="list-search" id="flow-search" placeholder="搜索 code / 名称">
          <div class="pattern-list" id="flow-list"></div>
          <button class="btn" id="btn-new-pattern" style="width:100%; margin-top:10px">＋ 新建空白 pattern</button>
        </aside>
        <section class="work-panel panel flow-editor" id="flow-editor"></section>
      </div>
    </div>`;
  $("#flow-search").oninput = (e) => renderFlowList(e.target.value.trim());
  $("#btn-new-pattern").onclick = newPattern;
  renderFlowList("");
  if (codeArg === "new") {
    newPattern();
  } else if (codeArg) {
    await openPattern(codeArg);
  } else {
    $("#flow-editor").innerHTML =
      '<div class="notice info">从左侧选择一个 pattern 开始编辑；代码内置（code）版本只读，可 Fork 为可编辑副本。</div>';
  }
}

function renderFlowList(query) {
  const items = flowState.patterns.filter((p) =>
    !query || (p.code + p.name + (p.description || "")).includes(query));
  $("#flow-list").innerHTML = items.map((p) => `
    <div class="pattern-item ${p.code === flowState.code ? "selected" : ""}" data-code="${esc(p.code)}">
      <div class="p-title">${esc(p.name || p.code)}
        <span class="badge ${p.source}">${p.source}</span>
        ${p.load_error ? '<span class="badge err">装载失败</span>' : ""}
      </div>
      <div class="p-code">${esc(p.code)}</div>
    </div>`).join("") || '<div class="notice warn">无匹配</div>';
  $$("#flow-list .pattern-item").forEach((el) =>
    el.onclick = () => { location.hash = `#/flow/${encodeURIComponent(el.dataset.code)}`; });
}

function draftStorageKey(code) { return `studio_draft_${code}`; }

function saveLocalDraft() {
  if (!flowState.draft || !flowState.draft.code) return;
  try {
    localStorage.setItem(draftStorageKey(flowState.draft.code),
      JSON.stringify({ ts: Date.now(), draft: flowState.draft }));
  } catch (_) {}
}

async function openPattern(code) {
  flowState.code = code;
  flowState.selectedNode = "";
  flowState.tab = "nodes";
  flowState.localDraft = null;
  renderFlowList($("#flow-search") ? $("#flow-search").value.trim() : "");
  // 自动编排页跳转带来的未注册 YAML 草稿（sessionStorage 单次传递）
  const pendingYaml = sessionStorage.getItem("studio_open_yaml");
  if (pendingYaml) sessionStorage.removeItem("studio_open_yaml");
  try {
    let yaml = pendingYaml;
    if (!yaml) {
      yaml = (await api(`/api/v1/studio/patterns/${encodeURIComponent(code)}`)).yaml;
    }
    const saved = localStorage.getItem(draftStorageKey(code));
    if (saved) {
      try { flowState.localDraft = JSON.parse(saved); }
      catch (_) { flowState.localDraft = null; }
    }
    flowState.draft = await parseYaml(yaml);
  } catch (e) {
    flowState.draft = null;
    $("#flow-editor").innerHTML = `<div class="notice warn">加载失败：${esc(e.message)}</div>`;
    return;
  }
  renderEditor();
}

function newPattern() {
  flowState.code = "new";
  flowState.patterns = flowState.patterns; // 列表保持
  flowState.selectedNode = "";
  flowState.tab = "meta";
  flowState.localDraft = null;
  flowState.draft = {
    code: "", name: "", description: "", pattern_type: "agent",
    entry_node_code: "start",
    nodes: [{
      code: "start", name: "开始", description: "", task_description: "",
      sub_nodes: [], config: { base_prompt: "" },
    }],
  };
  renderFlowList("");
  renderEditor();
}

function readOnly() {
  const meta = flowState.patterns.find((p) => p.code === flowState.code);
  return flowState.code !== "new" && meta && meta.source === "code"
    && flowState.localDraft === null;
}

function renderEditor() {
  const d = flowState.draft;
  const editor = $("#flow-editor");
  if (!d) {
    editor.innerHTML = '<div class="notice info">未加载 pattern</div>';
    return;
  }
  const ro = readOnly();
  const nodeCodes = (d.nodes || []).map((n) => n.code);
  const tabs = [["meta", "基本信息"], ["nodes", "节点"], ["yaml", "YAML 源码"], ["graph", "结构图"], ["assist", "AI 助手"]];
  editor.innerHTML = `
    ${flowState.localDraft ? `
      <div class="notice warn" style="display:flex; align-items:center; gap:10px">
        检测到本地未发布草稿（${new Date(flowState.localDraft.ts).toLocaleString()}）
        <button class="btn btn-sm" id="draft-restore">恢复草稿</button>
        <button class="btn btn-sm" id="draft-discard">丢弃</button>
      </div>` : ""}
    <div class="editor-head">
      <div class="field" style="width:170px">
        <label>code</label>
        <input type="text" id="f-code" value="${esc(d.code)}" ${ro || (flowState.code !== "new" && d.code) ? "disabled" : ""} placeholder="my_pattern">
      </div>
      <div class="field" style="width:180px">
        <label>名称</label>
        <input type="text" id="f-name" value="${esc(d.name || "")}" ${ro ? "disabled" : ""}>
      </div>
      <div class="field" style="width:110px">
        <label>类型</label>
        <select id="f-type" ${ro ? "disabled" : ""}>
          <option value="agent" ${d.pattern_type !== "fsm" ? "selected" : ""}>agent</option>
          <option value="fsm" ${d.pattern_type === "fsm" ? "selected" : ""}>fsm</option>
        </select>
      </div>
      <div class="field" style="width:150px">
        <label>入口节点</label>
        <select id="f-entry" ${ro ? "disabled" : ""}>
          ${nodeCodes.map((c) => `<option value="${esc(c)}" ${c === d.entry_node_code ? "selected" : ""}>${esc(c)}</option>`).join("")}
        </select>
      </div>
      <div class="editor-actions">
        ${ro ? `<button class="btn btn-sm" id="btn-fork">Fork 为可编辑副本</button>` : `
          <button class="btn btn-sm" id="btn-validate">校验</button>
          <button class="btn btn-primary btn-sm" id="btn-publish">发布</button>
          ${flowState.code !== "new" ? '<button class="btn btn-danger btn-sm" id="btn-delete">删除</button>' : ""}`}
      </div>
    </div>
    ${ro ? '<div class="notice info">代码内置（code-managed）pattern：只读。Fork 后可全功能编辑，发布为控制台托管版本。</div>' : ""}
    <div class="subtabs">${tabs.map(([id, label]) =>
      `<button data-tab="${id}" class="${flowState.tab === id ? "active" : ""}">${label}</button>`).join("")}
    </div>
    <div class="editor-body" id="editor-body"></div>`;
  wireEditorHead();
  renderEditorTab();
}

function wireEditorHead() {
  const d = flowState.draft;
  const ro = readOnly();
  if ($("#f-code") && !$("#f-code").disabled)
    $("#f-code").oninput = (e) => { d.code = e.target.value.trim(); saveLocalDraft(); };
  if ($("#f-name") && !$("#f-name").disabled)
    $("#f-name").oninput = (e) => { d.name = e.target.value; saveLocalDraft(); };
  if ($("#f-type") && !$("#f-type").disabled)
    $("#f-type").onchange = (e) => { d.pattern_type = e.target.value; touch(); };
  if ($("#f-entry") && !$("#f-entry").disabled)
    $("#f-entry").onchange = (e) => { d.entry_node_code = e.target.value; saveLocalDraft(); };
  if ($("#btn-fork")) $("#btn-fork").onclick = forkCurrent;
  if ($("#btn-validate")) $("#btn-validate").onclick = () => validateDraft();
  if ($("#btn-publish")) $("#btn-publish").onclick = publishDraft;
  if ($("#btn-delete")) $("#btn-delete").onclick = deleteCurrent;
  if ($("#draft-restore")) $("#draft-restore").onclick = () => {
    flowState.draft = flowState.localDraft.draft;
    flowState.localDraft = null;
    renderEditor();
  };
  if ($("#draft-discard")) $("#draft-discard").onclick = () => {
    localStorage.removeItem(draftStorageKey(flowState.code));
    flowState.localDraft = null;
    renderEditor();
  };
  $$("#flow-editor .subtabs button").forEach((btn) =>
    btn.onclick = () => { flowState.tab = btn.dataset.tab; renderEditorTab(); });
}

function touch() { saveLocalDraft(); renderEditor(); }

async function renderEditorTab() {
  $$("#flow-editor .subtabs button").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.tab === flowState.tab));
  const body = $("#editor-body");
  const d = flowState.draft;
  const ro = readOnly();
  if (flowState.tab === "meta") renderMetaTab(body, d, ro);
  else if (flowState.tab === "nodes") renderNodesTab(body, d, ro);
  else if (flowState.tab === "yaml") await renderYamlTab(body, d, ro);
  else if (flowState.tab === "graph") {
    body.innerHTML = `<div class="notice info">结构图在校验/发布后按最新草稿刷新。</div>
      <div class="mermaid-box" id="graph-mermaid"></div>`;
  } else if (flowState.tab === "assist") renderAssistTab(body, d);
}

function renderMetaTab(body, d, ro) {
  const cat = flowState.catalog || {};
  const slotSelect = (slot, kind, current) => `
    <select id="f-plugin-${slot}" ${ro ? "disabled" : ""}>
      <option value="">（默认）</option>
      ${(cat[kind] || []).map((c) =>
        `<option value="${esc(c)}" ${c === current ? "selected" : ""}>${esc(c)}</option>`).join("")}
    </select>`;
  const loopSlot = d.pattern_type === "fsm" ? "fsm" : "loop";
  const plugins = d.plugins || {};
  body.innerHTML = `
    <div class="node-form">
      <div class="form-grid">
        <div class="field span2">
          <label>描述</label>
          <textarea id="f-desc" ${ro ? "disabled" : ""}>${esc(d.description || "")}</textarea>
        </div>
        <div class="field">
          <label>执行器插件（${loopSlot} 槽位）</label>
          ${slotSelect(loopSlot, "executor", plugins[loopSlot])}
        </div>
        <div class="field">
          <label>messages_builder 插件</label>
          ${slotSelect("messages_builder", "messages_builder", plugins.messages_builder)}
        </div>
        <div class="field">
          <label>allow_toolset（逗号分隔，空 = 不授权任何工具集）</label>
          <input type="text" id="f-toolset" value="${esc((d.allow_toolset || []).join(", "))}" ${ro ? "disabled" : ""}>
        </div>
        ${d.pattern_type !== "fsm" ? `
        <div class="field">
          <label>max_steps（agent 每轮最大步数，默认 10）</label>
          <input type="number" id="f-maxsteps" value="${esc(d.max_steps || 10)}" ${ro ? "disabled" : ""}>
        </div>` : `
        <div class="field span2">
          <label>FSM 流水线骨架 stages（JSON）</label>
          <textarea class="code" id="f-stages" style="min-height:90px" ${ro ? "disabled" : ""}>${esc(JSON.stringify(d.stages || [], null, 0))}</textarea>
        </div>`}
        <div class="field span2">
          <label>其它 config（JSON，与顶层字段同名键会被忽略）</label>
          <textarea class="code" id="f-config" style="min-height:90px" ${ro ? "disabled" : ""}>${esc(JSON.stringify(d.config || {}, null, 0))}</textarea>
        </div>
      </div>
    </div>`;
  if (!ro) {
    $("#f-desc").oninput = (e) => { d.description = e.target.value; saveLocalDraft(); };
    $("#f-plugin-" + loopSlot).onchange = (e) => {
      d.plugins = d.plugins || {};
      if (e.target.value) d.plugins[loopSlot] = e.target.value;
      else delete d.plugins[loopSlot];
      saveLocalDraft();
    };
    $("#f-plugin-messages_builder").onchange = (e) => {
      d.plugins = d.plugins || {};
      if (e.target.value) d.plugins.messages_builder = e.target.value;
      else delete d.plugins.messages_builder;
      saveLocalDraft();
    };
    $("#f-toolset").oninput = (e) => {
      d.allow_toolset = e.target.value.split(/[,，\s]+/).filter(Boolean);
      saveLocalDraft();
    };
    const ms = $("#f-maxsteps");
    if (ms) ms.oninput = (e) => {
      d.max_steps = +e.target.value || 10; saveLocalDraft();
    };
    const st = $("#f-stages");
    if (st) st.onchange = (e) => {
      try { d.stages = JSON.parse(e.target.value || "[]"); }
      catch (_) { toast("stages JSON 非法", false); }
      saveLocalDraft();
    };
    $("#f-config").onchange = (e) => {
      try { d.config = JSON.parse(e.target.value || "{}"); }
      catch (_) { toast("config JSON 非法", false); }
      saveLocalDraft();
    };
  }
}

function renderNodesTab(body, d, ro) {
  const nodes = d.nodes || [];
  body.innerHTML = `
    <table class="node-table">
      <thead><tr><th>code</th><th>名称</th><th>后继</th><th>插件</th><th>终态</th><th></th></tr></thead>
      <tbody>
        ${nodes.map((n) => `
          <tr data-code="${esc(n.code)}" class="${n.code === flowState.selectedNode ? "selected" : ""}">
            <td class="mono">${esc(n.code)}${n.code === d.entry_node_code ? ' <span class="badge code">entry</span>' : ""}</td>
            <td>${esc(n.name || "")}</td>
            <td class="mono">${esc((n.sub_nodes || []).join(", ") || "-")}</td>
            <td class="mono">${esc(Object.entries(n.plugins || {}).map(([k, v]) => k + ":" + v).join(" ") || "-")}</td>
            <td>${n.is_end ? "✓" : ""}</td>
            <td>${ro ? "" : `<button class="btn btn-ghost btn-sm" data-del="${esc(n.code)}">删除</button>`}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    ${ro ? "" : '<button class="btn btn-sm" id="btn-add-node" style="margin-top:10px">＋ 新增节点</button>'}
    <div id="node-form-box">${flowState.selectedNode ? renderNodeForm(d, ro) : ""}</div>`;
  $$("#editor-body .node-table tbody tr").forEach((tr) =>
    tr.onclick = (e) => {
      if (e.target.dataset.del) return;
      flowState.selectedNode = tr.dataset.code;
      renderEditorTab();
    });
  $$("#editor-body [data-del]").forEach((btn) =>
    btn.onclick = () => {
      const code = btn.dataset.del;
      d.nodes = d.nodes.filter((n) => n.code !== code);
      (d.nodes || []).forEach((n) =>
        n.sub_nodes = (n.sub_nodes || []).filter((c) => c !== code));
      if (flowState.selectedNode === code) flowState.selectedNode = "";
      touch();
    });
  const addBtn = $("#btn-add-node");
  if (addBtn) addBtn.onclick = () => {
    openModal({
      title: "新增节点",
      body: `<div class="field"><label class="req">节点 code（小写字母/数字/下划线）</label>
        <input type="text" name="code" placeholder="ask_phone"></div>`,
      submitText: "创建",
      onSubmit: async (values) => {
        if (!values.code.trim()) throw new Error("code 必填");
        if ((d.nodes || []).some((n) => n.code === values.code.trim()))
          throw new Error("code 重复");
        d.nodes.push({ code: values.code.trim(), name: values.code,
          description: "", task_description: "", sub_nodes: [],
          config: {} });
        flowState.selectedNode = values.code.trim();
        touch();
      },
    });
  };
  if (flowState.selectedNode) wireNodeForm(d, ro);
}

function renderNodeForm(d, ro) {
  const n = (d.nodes || []).find((x) => x.code === flowState.selectedNode);
  if (!n) return "";
  const others = (d.nodes || []).filter((x) => x.code !== n.code);
  const cat = flowState.catalog || {};
  const isFsm = d.pattern_type === "fsm";
  const loopSlot = isFsm ? "fsm" : "loop";
  const plugins = n.plugins || {};
  const checked = (c) => (n.sub_nodes || []).includes(c) ? "checked" : "";
  return `
    <div class="node-form">
      <h3>节点：${esc(n.code)} <span class="sub">${ro ? "只读" : "编辑后自动存本地草稿"}</span></h3>
      <div class="form-grid">
        <div class="field"><label>名称</label>
          <input type="text" id="n-name" value="${esc(n.name || "")}" ${ro ? "disabled" : ""}></div>
        <div class="field"><label>执行器插件（${loopSlot} 槽位）</label>
          <select id="n-plugin-loop" ${ro ? "disabled" : ""}>
            <option value="">（继承 pattern / 默认）</option>
            ${(cat.executor || []).map((c) =>
              `<option value="${esc(c)}" ${c === plugins[loopSlot] ? "selected" : ""}>${esc(c)}</option>`).join("")}
          </select></div>
        <div class="field span2"><label>场景描述（description，喂 NLG）</label>
          <textarea id="n-desc" ${ro ? "disabled" : ""}>${esc(n.description || "")}</textarea></div>
        <div class="field span2"><label>待办描述（task_description，喂 NLU）</label>
          <textarea id="n-task" ${ro ? "disabled" : ""}>${esc(n.task_description || "")}</textarea></div>
        <div class="field span2"><label>后继节点（sub_nodes：agent = 图邻接边 / fsm = 合法转移）</label>
          <div class="checkbox-row">${others.map((x) =>
            `<label><input type="checkbox" data-sub="${esc(x.code)}" ${checked(x.code)} ${ro ? "disabled" : ""}>${esc(x.code)}</label>`).join("") || "（无其它节点）"}</div></div>
        <div class="field span2"><label>回答范式（answer_examples，一行一条）</label>
          <textarea id="n-examples" ${ro ? "disabled" : ""}>${esc((n.answer_examples || []).join("\n"))}</textarea></div>
        <div class="field span2"><label>base_prompt（config 提示词资产）</label>
          <textarea class="code" id="n-baseprompt" ${ro ? "disabled" : ""}>${esc((n.config || {}).base_prompt || "")}</textarea></div>
        <div class="field"><label>use_tools（逗号分隔，空 = 禁用工具）</label>
          <input type="text" id="n-usetools" value="${esc((n.use_tools || []).join(", "))}" ${ro ? "disabled" : ""}></div>
        <div class="field" style="align-self:end"><label class="checkbox-row">
          <input type="checkbox" id="n-isend" ${n.is_end ? "checked" : ""} ${ro ? "disabled" : ""}> 终态节点（is_end）</label></div>
        ${isFsm ? `
        <div class="field span2"><label>stages 覆写（JSON {槽位: stage_code}）</label>
          <textarea class="code" id="n-stages" style="min-height:70px" ${ro ? "disabled" : ""}>${esc(JSON.stringify(n.stages || {}))}</textarea></div>
        <div class="field span2"><label>slots（JSON {槽位名: 描述}，仅 FSM）</label>
          <textarea class="code" id="n-slots" style="min-height:70px" ${ro ? "disabled" : ""}>${esc(JSON.stringify(n.slots || {}))}</textarea></div>` : ""}
        <div class="field span2"><label>其它 config（JSON）</label>
          <textarea class="code" id="n-config" style="min-height:70px" ${ro ? "disabled" : ""}>${esc(JSON.stringify(Object.assign({}, n.config || {}, { base_prompt: undefined }), null, 0))}</textarea></div>
      </div>
    </div>`;
}

function wireNodeForm(d, ro) {
  const n = (d.nodes || []).find((x) => x.code === flowState.selectedNode);
  if (!n || ro) return;
  $("#n-name").oninput = (e) => { n.name = e.target.value; saveLocalDraft(); };
  $("#n-desc").oninput = (e) => { n.description = e.target.value; saveLocalDraft(); };
  $("#n-task").oninput = (e) => { n.task_description = e.target.value; saveLocalDraft(); };
  $("#n-examples").onchange = (e) => {
    n.answer_examples = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean);
    saveLocalDraft();
  };
  $("#n-baseprompt").oninput = (e) => {
    n.config = n.config || {}; n.config.base_prompt = e.target.value; saveLocalDraft();
  };
  $("#n-usetools").oninput = (e) => {
    n.use_tools = e.target.value.split(/[,，\s]+/).filter(Boolean); saveLocalDraft();
  };
  $("#n-isend").onchange = (e) => { n.is_end = e.target.checked; saveLocalDraft(); };
  $("#n-plugin-loop").onchange = (e) => {
    n.plugins = n.plugins || {};
    const slot = d.pattern_type === "fsm" ? "fsm" : "loop";
    if (e.target.value) n.plugins[slot] = e.target.value;
    else delete n.plugins[slot];
    saveLocalDraft();
  };
  $$("#node-form-box [data-sub]").forEach((cb) =>
    cb.onchange = () => {
      const set = new Set(n.sub_nodes || []);
      if (cb.checked) set.add(cb.dataset.sub); else set.delete(cb.dataset.sub);
      n.sub_nodes = Array.from(set);
      saveLocalDraft();
    });
  const st = $("#n-stages");
  if (st) st.onchange = (e) => {
    try { n.stages = JSON.parse(e.target.value || "{}"); } catch (_) { toast("stages JSON 非法", false); }
    saveLocalDraft();
  };
  const sl = $("#n-slots");
  if (sl) sl.onchange = (e) => {
    try { n.slots = JSON.parse(e.target.value || "{}"); } catch (_) { toast("slots JSON 非法", false); }
    saveLocalDraft();
  };
  $("#n-config").onchange = (e) => {
    try {
      const extra = JSON.parse(e.target.value || "{}");
      n.config = Object.assign({}, n.config, extra);
    } catch (_) { toast("config JSON 非法", false); }
    saveLocalDraft();
  };
}

async function renderYamlTab(body, d, ro) {
  await loadYaml();
  body.innerHTML = ro
    ? `<pre class="code">${esc(dumpYaml(d))}</pre>`
    : `<textarea class="code" id="yaml-edit" style="width:100%; min-height:420px">${esc(dumpYaml(d))}</textarea>
       <div class="hint" style="margin-top:6px">直接编辑 YAML；合法解析后自动回填表单草稿。</div>`;
  if (ro) return;
  const ta = $("#yaml-edit");
  ta.oninput = async () => {
    try {
      const parsed = await parseYaml(ta.value);
      if (parsed && typeof parsed === "object") {
        ta.style.borderColor = "";
        flowState.draft = parsed;
        saveLocalDraft();
      }
    } catch (_) {
      ta.style.borderColor = "var(--err)";
    }
  };
}

async function draftYaml() {
  await loadYaml();
  return dumpYaml(flowState.draft);
}

async function validateDraft() {
  if (flowState.validating) return;
  flowState.validating = true;
  try {
    const yaml = await draftYaml();
    const data = await api("/api/v1/studio/patterns/validate",
      { method: "POST", body: { yaml } });
    const warnings = data.warnings || [];
    if (data.errors && data.errors.length) {
      toast("校验未通过（" + data.errors.length + " 项）", false);
      openModal({
        title: "校验结果（收集式报错）",
        body: `<ul class="error-list">${data.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`,
        submitText: "知道了",
        onSubmit: async () => {},
      });
    } else {
      toast(warnings.length
        ? `校验通过（${warnings.length} 项工具软警告：新工具注册后生效）`
        : "校验通过", true);
      flowState.mermaid = data.mermaid;
      flowState.tab = "graph";
      renderEditorTab();
      const box = $("#graph-mermaid");
      if (box) renderMermaid(box, data.mermaid);
    }
  } catch (e) {
    toast("校验请求失败：" + e.message, false);
  }
  flowState.validating = false;
}

async function publishDraft() {
  const d = flowState.draft;
  if (!d.code || !/^[a-z][a-z0-9_]{0,63}$/.test(d.code)) {
    toast("code 必填（小写字母开头，小写字母/数字/下划线）", false);
    flowState.tab = "meta";
    renderEditorTab();
    return;
  }
  const yaml = await draftYaml();
  confirmDialog(`发布 ${d.code}：校验 → 注册 → 落盘（新会话即生效，运行中会话持旧版跑完本轮）。`, async () => {
    const data = await api("/api/v1/studio/patterns/publish",
      { method: "POST", body: { yaml } });
    toast(data.message || "已发布", true);
    localStorage.removeItem(draftStorageKey(d.code));
    flowState.patterns = (await api("/api/v1/studio/patterns")).patterns;
    flowState.mermaid = data.mermaid;
    const target = `#/flow/${encodeURIComponent(d.code)}`;
    if (location.hash !== target) location.hash = target;  // hashchange 接管刷新
    else await openPattern(d.code);
  });
}

async function forkCurrent() {
  const data = await api("/api/v1/studio/patterns/fork",
    { method: "POST", body: { code: flowState.code } });
  toast(data.message || "已 fork", true);
  flowState.patterns = (await api("/api/v1/studio/patterns")).patterns;
  await openPattern(flowState.code);
}

async function deleteCurrent() {
  const code = flowState.code;
  confirmDialog(`删除控制台托管版本 ${code}（注销注册 + 删文件；若存在代码内置版本，重启/重载后恢复）？`, async () => {
    const data = await api(`/api/v1/studio/patterns/${encodeURIComponent(code)}`,
      { method: "DELETE" });
    toast(data.message || "已删除", true);
    localStorage.removeItem(draftStorageKey(code));
    flowState.patterns = (await api("/api/v1/studio/patterns")).patterns;
    location.hash = "#/flow";
  });
}

/* -------- AI 助手（节点话术 / 回答范式 / 自定义执行器生成） -------- */

function renderAssistTab(body, d) {
  const nodes = (d.nodes || []).map((n) => n.code);
  body.innerHTML = `
    <div class="assist-panel">
      <div class="assist-row">
        <select id="as-node">${nodes.map((c) =>
          `<option value="${esc(c)}" ${c === flowState.selectedNode ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>
        <select id="as-mode">
          <option value="node_prompt">生成节点话术（base_prompt + 回答范式）</option>
          <option value="node_examples">生成回答范式</option>
          <option value="plugin_generate">生成自定义执行器插件</option>
        </select>
        <button class="btn btn-primary btn-sm" id="as-run">生成</button>
      </div>
      <div class="field">
        <label>补充要点（可选）</label>
        <textarea id="as-hints" placeholder="例如：语气亲切；必须先确认手机号；拒绝议价时给出替代方案…"></textarea>
      </div>
      <div class="assist-out" id="as-out"></div>
    </div>`;
  $("#as-run").onclick = runAssist;
}

async function runAssist() {
  const d = flowState.draft;
  const nodeCode = $("#as-node").value;
  const node = (d.nodes || []).find((n) => n.code === nodeCode);
  if (!node) return toast("请先选择节点", false);
  const mode = $("#as-mode").value;
  const out = $("#as-out");
  out.innerHTML = '<div class="gen-status"><span class="spin"></span>生成中…</div>';
  const payload = {
    pattern_code: d.code, node_code: node.code, node_name: node.name,
    description: node.description, task_description: node.task_description,
    sub_nodes: (node.sub_nodes || []).join(", "),
    node_config_prompt: (node.config || {}).base_prompt || "",
    hints: $("#as-hints").value.trim(),
  };
  try {
    const data = await api("/api/v1/studio/assist", { method: "POST", body: { mode, payload } });
    if (mode === "plugin_generate") {
      renderAssistPlugin(out, data, nodeCode);
    } else {
      renderAssistJson(out, data, nodeCode);
    }
  } catch (e) {
    out.innerHTML = `<div class="notice warn">${esc(e.message)}</div>`;
  }
}

function renderAssistJson(out, data, nodeCode) {
  const parsed = data.json;
  out.innerHTML = `
    ${parsed ? "" : '<div class="notice warn">模型输出未解析出 JSON，可查看原文后手动粘贴。</div>'}
    <pre class="code">${esc(data.text || "")}</pre>
    ${parsed ? `
      <div style="display:flex; gap:8px; margin-top:8px">
        <button class="btn btn-primary btn-sm" id="as-apply">填充到节点 ${esc(nodeCode)}</button>
      </div>` : ""}`;
  const applyBtn = $("#as-apply");
  if (applyBtn) applyBtn.onclick = () => {
    const node = (flowState.draft.nodes || []).find((n) => n.code === nodeCode);
    if (!node) return;
    if (parsed.base_prompt) {
      node.config = node.config || {};
      node.config.base_prompt = parsed.base_prompt;
    }
    if (Array.isArray(parsed.answer_examples) && parsed.answer_examples.length) {
      node.answer_examples = parsed.answer_examples;
    }
    saveLocalDraft();
    toast("已填充，可在节点表单核对", true);
    flowState.selectedNode = nodeCode;
    flowState.tab = "nodes";
    renderEditorTab();
  };
}

function renderAssistPlugin(out, data, nodeCode) {
  const plugin = data.plugin;
  if (!plugin) {
    out.innerHTML = `<div class="notice info">${esc(data.text || "（模型判断可直接复用已注册插件，未生成代码）")}</div>`;
    return;
  }
  out.innerHTML = `
    <div class="plugin-card">
      <div class="plugin-head">
        <span>${esc(plugin.filename || "?")}</span>
        ${plugin.declarations.map((x) =>
          `<span class="badge code">${esc(x.kind)}:${esc(x.code)}</span>`).join("")}
      </div>
      <pre class="code">${esc(plugin.code_text)}</pre>
    </div>
    <div style="display:flex; gap:8px">
      <button class="btn btn-primary btn-sm" id="as-apply-plugin">
        应用插件（落盘 + 注册 + 填入节点槽位）
      </button>
    </div>`;
  $("#as-apply-plugin").onclick = async () => {
    const node = (flowState.draft.nodes || []).find((n) => n.code === nodeCode);
    const executorCode = (plugin.declarations.find((x) => x.kind === "executor") || {}).code;
    if (node && executorCode) {
      node.plugins = node.plugins || {};
      node.plugins.loop = executorCode;  // 先填槽位再 dump，apply 的校验才能解析
    }
    try {
      const result = await api("/api/v1/studio/apply", { method: "POST", body: {
        yaml: await draftYaml(),
        plugins: [{ filename: plugin.filename, code: plugin.code_text }],
      } });
      toast(result.message || "已应用", true);
      flowState.patterns = (await api("/api/v1/studio/patterns")).patterns;
    } catch (e) {
      toast("应用失败：" + e.message, false);
      return;
    }
    flowState.tab = "nodes";
    renderEditor();
  };
}

/* ============================== 系统插件 ============================== */
/* 系统面状态 + 选择性热重载：插件按归属模块勾选重载（studio 托管插件按文件
 * 重放；代码插件连其 consumer 按依赖序重放并重绑会话）；MCP 工具跟随连接
 * 生命周期（重读配置 → 断开重连 → 重注册 mcp-* 工具）。kernel 内核实现
 * 不可热重载（与 /reload 的文件 mtime 机制互补）。 */

const systemState = { reloading: false };

async function renderSystem() {
  $("#main").innerHTML = `<div class="panel"><div class="notice info">加载系统面状态…</div></div>`;
  renderSystemView(await api("/api/v1/system/status"));
}

function renderSystemView(data) {
  const plugins = (data && data.plugins) || [];
  const servers = (data && data.servers) || [];
  const toolsets = (data && data.toolsets) || {};
  const pluginRows = plugins.length ? plugins.map((p) => {
    const selectable = p.source === "studio" || p.source === "code";
    return `
    <tr>
      <td>${selectable
        ? `<input type="checkbox" class="plugin-sel" value="${esc(p.kind)}:${esc(p.code)}">`
        : ""}</td>
      <td><code>${esc(p.kind)}</code></td>
      <td><code>${esc(p.code)}</code></td>
      <td><span class="badge ${esc(p.source)}">${esc(p.source)}</span></td>
      <td class="sub">${esc(p.source === "studio"
        ? `host/config/plugins/${p.module}.py` : p.module)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="sub">（空）</td></tr>`;
  const serverRows = servers.length ? servers.map((s) => `
    <tr>
      <td><code>mcp-${esc(s.server)}</code></td>
      <td>${s.ready ? '<span class="badge ok">已连接</span>'
                    : '<span class="badge err">未连接</span>'}</td>
      <td>${(s.tools || []).length}</td>
      <td class="sub">${esc(s.error || "")}</td>
    </tr>`).join("")
    : `<tr><td colspan="4" class="sub">未配置 MCP server（host/config/local_config.yaml · mcp_servers）</td></tr>`;
  const tsEntries = Object.entries(toolsets);
  const tsRows = tsEntries.length ? tsEntries.map(([name, meta]) => {
    const tools = meta.tools || [];
    return `
    <tr>
      <td><code>${esc(name)}</code></td>
      <td>${meta.available ? '<span class="badge ok">可用</span>'
                           : '<span class="badge err">不可用</span>'}</td>
      <td>${tools.length}</td>
      <td class="sub">${esc(tools.slice(0, 6).join("、"))}${tools.length > 6 ? " …" : ""}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="4" class="sub">（空）</td></tr>`;
  const busy = systemState.reloading ? "disabled" : "";
  $("#main").innerHTML = `
    <section class="panel">
      <h3>插件（PluginRegistry · 按归属模块选择性重载）</h3>
      <table>
        <thead><tr><th><input type="checkbox" id="plugin-sel-all" title="全选可重载项"></th><th>kind</th><th>code</th><th>来源</th><th>归属模块</th></tr></thead>
        <tbody>${pluginRows}</tbody>
      </table>
      <div class="field" style="margin-top:16px; margin-bottom:0">
        <button class="btn btn-primary" id="btn-plugins-reload" ${busy}>重载选中插件</button>
        <button class="btn" id="btn-mcp-reload" ${busy}>重载工具面（MCP）</button>
        <span class="sub">studio 插件按文件重放；code 插件连 consumer 依赖序重放并重绑会话；kernel 不可热重载</span>
      </div>
    </section>
    <section class="panel">
      <h3>MCP server（连接生命周期 = 工具注册源头）</h3>
      <table>
        <thead><tr><th>toolset</th><th>状态</th><th>工具数</th><th>错误</th></tr></thead>
        <tbody>${serverRows}</tbody>
      </table>
    </section>
    <section class="panel">
      <h3>工具集（ToolRegistry）</h3>
      <table>
        <thead><tr><th>toolset</th><th>可用</th><th>工具数</th><th>工具</th></tr></thead>
        <tbody>${tsRows}</tbody>
      </table>
    </section>`;
  const all = $("#plugin-sel-all");
  if (all) all.onchange = (e) =>
    $$(".plugin-sel").forEach((c) => { c.checked = e.target.checked; });
  const bp = $("#btn-plugins-reload");
  if (bp) bp.onclick = () => reloadSystem({ plugins: true });
  const bm = $("#btn-mcp-reload");
  if (bm) bm.onclick = () => reloadSystem({ mcp: true });
}

async function reloadSystem({ plugins = false, mcp = false } = {}) {
  if (systemState.reloading) return;
  let pluginCodes = [];
  if (plugins) {
    pluginCodes = $$(".plugin-sel").filter((c) => c.checked).map((c) => c.value);
    if (!pluginCodes.length) { toast("未勾选任何插件", false); return; }
  }
  systemState.reloading = true;
  for (const id of ["btn-plugins-reload", "btn-mcp-reload"]) {
    const b = $("#" + id);
    if (b) b.disabled = true;
  }
  try {
    await api("/api/v1/system/reload", {
      method: "POST", body: { plugin_codes: pluginCodes, mcp } });
    toast("系统面重载完成", true);
  } catch (e) {
    toast(e.message, false);
  } finally {
    systemState.reloading = false;
    await renderSystem();
  }
}

/* ============================== 启动 ============================== */

$("#api-key").value = state.key;
$("#api-key-save").onclick = () => {
  state.key = $("#api-key").value.trim();
  localStorage.setItem("nexus_console_key", state.key);
  toast(state.key ? "API key 已保存" : "API key 已清空", true);
  route();
};

window.addEventListener("hashchange", route);
route();
