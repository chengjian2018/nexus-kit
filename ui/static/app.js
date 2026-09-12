/* nexus-console P0 前端（无构建、无框架）
 * 路由：#/ 总览 | #/patterns 应用列表 | #/patterns/{code} 详情 | #/knowledge 知识库
 * 约定：所有数据经 /api/v1/console/*（响应包裹 {code,message,status,data}）；
 *       鉴权复用 NEXUS_API_KEY（X-API-Key 头，密钥存 localStorage）。
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

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function toast(message, ok) {
  const el = document.createElement("div");
  el.className = "toast " + (ok ? "ok" : "err");
  el.textContent = message;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), ok ? 2600 : 4200);
}

/* ============================== API 封装 ============================== */

const state = {
  key: localStorage.getItem("nexus_console_key") || "",
  scope: localStorage.getItem("nexus_console_scope") || "",
  scopes: [],
};

function setApiStatus(cls) {
  const dot = $("#api-status");
  dot.className = "status-dot" + (cls ? " " + cls : "");
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
        { "Content-Type": "application/json" },
        state.key ? { "X-API-Key": state.key } : {}),
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

/* ============================== 弹窗表单 ============================== */

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
    body: `<div class="form-row"><label class="req">${esc(text)}</label></div>`,
    submitText: "确认",
    onSubmit: async () => { await onOk(); },
  });
}

/* ============================== mermaid 渲染 ============================== */

let _mermaidPromise = null;
let _mermaidSeq = 0;

function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = new Promise((resolve) => {
      // 与 nexus/visualize.py 相同的多 CDN 回退序列
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

async function renderMermaid(container, code, fallbackPre) {
  const mermaid = await loadMermaid();
  if (!mermaid) { fallbackPre.hidden = false; return; }
  try {
    const { svg } = await mermaid.render("mmd" + (++_mermaidSeq), code);
    container.innerHTML = svg;
  } catch (e) {
    fallbackPre.hidden = false;
  }
}

/* ============================== 通用小组件 ============================== */

function typeBadge(type) {
  if (!type) return "";
  return `<span class="badge ${esc(type)}">${esc(type).toUpperCase()}</span>`;
}

function chips(list) {
  return (list || []).map((v) => `<span class="chip">${esc(v)}</span>`).join("")
    || '<span class="muted">-</span>';
}

function matchChips(match) {
  if (!match) return "";
  return Object.entries(match).map(
    ([field, words]) =>
      `<div class="match-expl">${esc(field)}: ` +
      words.map((w) => `<span class="chip hit">${esc(w)}</span>`).join(" ") +
      `</div>`).join("");
}

function downloadText(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板", true);
  } catch (e) {
    toast("复制失败，请手动选择复制", false);
  }
}

/* ============================== 路由 ============================== */

const views = {
  dashboard: renderDashboard,
  patterns: renderPatterns,
  patternDetail: renderPatternDetail,
  knowledge: renderKnowledge,
  rag: renderRag,
};

function route() {
  const hash = location.hash || "#/";
  const parts = hash.replace(/^#\//, "").split("/").filter(Boolean);
  let view = "dashboard", arg = null;
  if (parts[0] === "patterns" && parts[1]) { view = "patternDetail"; arg = decodeURIComponent(parts[1]); }
  else if (parts[0] === "patterns") view = "patterns";
  else if (parts[0] === "knowledge") view = "knowledge";
  else if (parts[0] === "rag") view = "rag";

  $$("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === view
      || (view === "patternDetail" && a.dataset.nav === "patterns")));
  $("#page-title").textContent = {
    dashboard: "总览", patterns: "应用（Pattern）",
    patternDetail: `应用详情 · ${arg}`, knowledge: "知识库",
    rag: "RAG 检索配置",
  }[view];

  const main = $("#main");
  main.innerHTML = '<div class="panel loading">加载中…</div>';
  views[view](main, arg).catch((err) => {
    main.innerHTML = `<div class="panel error">加载失败：${esc(err.message)}
      <div style="margin-top:10px"><button class="btn btn-sm" onclick="location.reload()">重试</button></div></div>`;
  });
}

/* ============================== 视图：总览 ============================== */

async function renderDashboard(main) {
  const [patternsData, scopesData] = await Promise.all([
    api("/api/v1/console/patterns"),
    api("/api/v1/console/knowledge/scopes").catch(() => ({ scopes: [] })),
  ]);
  const patterns = patternsData.patterns || [];
  const scopes = scopesData.scopes || [];
  const totalProducts = scopes.reduce((s, x) => s + x.product_count, 0);
  const totalCs = scopes.reduce((s, x) => s + x.cs_count, 0);

  main.innerHTML = `
    <div class="panel">
      <div class="row spread">
        <div><h2>应用（${patterns.length}）</h2>
        <p class="desc">点击卡片查看结构图 / 声明树 / YAML（P0 只读）</p></div>
        <a class="btn btn-sm" href="#/patterns">查看全部</a>
      </div>
      <div class="grid-cards">
        ${patterns.map((p) => `
          <div class="pattern-card" data-code="${esc(p.code)}">
            <div class="row spread">
              <span class="pc-name">${esc(p.name || p.code)}</span>
              ${typeBadge(p.pattern_type)}
            </div>
            <div class="pc-code">${esc(p.code)}</div>
            <div class="pc-desc">${esc(p.description || "")}</div>
            <div class="pc-meta">
              <span class="badge">节点 ${p.node_count}</span>
              <span class="badge">入口 ${esc(p.entry_node_code || "-")}</span>
              <span class="badge code-managed">事实源: code</span>
            </div>
          </div>`).join("")}
      </div>
    </div>
    <div class="panel">
      <h2>知识库</h2>
      <p class="desc">商品知识与客服知识按空间（scope = {channel}:{account_id}）隔离</p>
      <dl class="kv">
        <dt>空间数</dt><dd>${scopes.length}</dd>
        <dt>商品知识</dt><dd>${totalProducts} 条</dd>
        <dt>客服知识</dt><dd>${totalCs} 条</dd>
      </dl>
      <div style="margin-top:12px"><a class="btn btn-primary btn-sm" href="#/knowledge">进入知识库管理</a></div>
    </div>
    <div class="panel">
      <h2>说明</h2>
      <p class="desc" style="margin-bottom:6px">· 结构图配色：<span class="chip" style="background:var(--fsm-bg);border-color:var(--fsm-bd)">FSM 绿</span>
        <span class="chip" style="background:var(--agent-bg);border-color:var(--agent-bd)">AGENT 橙</span>（与 nexus/visualize.py 口径一致）</p>
      <p class="desc" style="margin:0">· 鉴权：服务设置 NEXUS_API_KEY 后，右上角填入密钥；未设置时留空即可。</p>
    </div>`;
  $$(".pattern-card", main).forEach((card) =>
    card.onclick = () => { location.hash = "#/patterns/" + card.dataset.code; });
}

/* ============================== 视图：应用列表 ============================== */

async function renderPatterns(main) {
  const data = await api("/api/v1/console/patterns");
  const patterns = data.patterns || [];
  main.innerHTML = `
    <div class="panel">
      <div class="row spread">
        <h2>已注册 pattern（${patterns.length}）</h2>
        <input type="text" id="pat-filter" placeholder="按 code / 名称过滤…" style="
          border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:240px">
      </div>
      <table class="tbl" style="margin-top:12px">
        <thead><tr><th>code</th><th>名称</th><th>类型</th><th>入口节点</th>
          <th>节点</th><th>流水线骨架</th><th>事实源</th><th></th></tr></thead>
        <tbody id="pat-tbody">
        ${patterns.map((p) => `
          <tr data-filter="${esc(p.code)} ${esc(p.name || "")}">
            <td class="num">${esc(p.code)}</td>
            <td>${esc(p.name || "-")}</td>
            <td>${typeBadge(p.pattern_type)}</td>
            <td class="num">${esc(p.entry_node_code || "-")}</td>
            <td class="num">${p.node_count}</td>
            <td>${(p.skeleton || []).map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</td>
            <td><span class="badge code-managed">code</span></td>
            <td><a class="btn btn-sm" href="#/patterns/${encodeURIComponent(p.code)}">查看</a></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  $("#pat-filter").oninput = (e) => {
    const kw = e.target.value.trim().toLowerCase();
    $$("#pat-tbody tr").forEach((tr) => {
      tr.style.display = !kw || tr.dataset.filter.toLowerCase().includes(kw) ? "" : "none";
    });
  };
}

/* ============================== 视图：应用详情 ============================== */

async function renderPatternDetail(main, code) {
  const data = await api(`/api/v1/console/patterns/${encodeURIComponent(code)}`);
  const { meta, yaml, mermaid, tree } = data;

  main.innerHTML = `
    <div class="panel">
      <div class="row spread">
        <div>
          <h2 class="row" style="gap:8px">${esc(meta.name || meta.code)} ${typeBadge(meta.pattern_type)}</h2>
          <div class="desc mono" style="margin:2px 0 8px">${esc(meta.code)}</div>
          <div>${esc(meta.description || "")}</div>
          <div class="row" style="margin-top:10px">
            <span class="badge">入口节点 <b class="mono">${esc(meta.entry_node_code)}</b></span>
            <span class="badge">节点 ${meta.node_count}</span>
            ${meta.max_steps != null ? `<span class="badge">max_steps ${meta.max_steps}</span>` : ""}
            ${(meta.plugins && Object.keys(meta.plugins).length) ? `<span class="badge">plugins ${Object.entries(meta.plugins).map(([s, c]) => `${esc(s)}=${esc(c)}`).join(", ")}</span>` : ""}
            ${(meta.allow_toolset && meta.allow_toolset.length) ? `<span class="badge">allow_toolset ${meta.allow_toolset.map(esc).join(", ")}</span>` : `<span class="badge">allow_toolset（无工具集）</span>`}
            <span class="badge code-managed">事实源: code（P0 只读）</span>
          </div>
        </div>
        <div class="row">
          <button class="btn btn-sm" id="btn-copy-yml">复制 YAML</button>
          <button class="btn btn-primary btn-sm" id="btn-dl-yml">导出 YAML</button>
        </div>
      </div>
    </div>
    <div class="tabs" id="detail-tabs">
      <button data-tab="diagram" class="active">结构图</button>
      <button data-tab="tree">结构树</button>
      <button data-tab="yaml">YAML</button>
    </div>
    <div id="detail-body"></div>`;

  $("#btn-copy-yml").onclick = () => copyText(yaml);
  $("#btn-dl-yml").onclick = () =>
    downloadText(`${meta.code}.yml`, yaml, "text/yaml;charset=utf-8");

  const body = $("#detail-body");
  const tabs = { diagram: renderDiagramTab, tree: renderTreeTab, yaml: renderYamlTab };
  function activate(name) {
    $$("#detail-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    tabs[name](body, { meta, yaml, mermaid, tree });
  }
  $$("#detail-tabs button").forEach((b) => b.onclick = () => activate(b.dataset.tab));
  activate("diagram");
}

function renderDiagramTab(body, { meta, mermaid, tree }) {
  const nodeCount = (tree.nodes || []).length;
  body.innerHTML = `
    <div class="panel">
      <div class="legend">
        <span class="chip" style="background:var(--fsm-bg);border-color:var(--fsm-bd)">FSM 状态机</span>
        <span class="chip" style="background:var(--agent-bg);border-color:var(--agent-bd)">AGENT 图运行时</span>
        <span class="chip">⏵ 开始 = 入口节点（entry_node_code）</span>
        <span class="chip">实线 → 节点边 sub_nodes（FSM 转移合法集 / AGENT 图邻接）</span>
        <span class="chip" style="background:var(--end-bg);border-color:var(--end-bd)">终态节点（is_end）</span>
      </div>
      <div class="diagram" id="mermaid-box">${meta.code} · ${nodeCount} 节点</div>
      <pre class="code" id="mermaid-fallback" hidden>${esc(mermaid)}</pre>
      <div class="row" style="margin-top:10px">
        <button class="btn btn-sm" id="btn-copy-mmd">复制 mermaid 源码</button>
        <span class="muted" style="font-size:12px">图渲染失败（如离线）时上方显示源码，可粘到 mermaid.live 查看</span>
      </div>
    </div>`;
  $("#btn-copy-mmd").onclick = () => copyText(mermaid);
  renderMermaid($("#mermaid-box"), mermaid, $("#mermaid-fallback"));
}

function renderTreeTab(body, { tree, meta }) {
  const nodes = tree.nodes || [];
  const entry = tree.entry_node_code;
  const plugins = Object.entries(tree.plugins || {})
    .map(([s, c]) => `<span class="chip">${esc(s)} → <b class="mono">${esc(c)}</b></span>`).join("");
  body.innerHTML = `
    <div class="panel">
      <div class="row spread">
        <h2 class="row" style="gap:8px">pattern 声明 ${typeBadge(tree.pattern_type)}</h2>
        <span class="muted" style="font-size:12px">${nodes.length} 节点</span>
      </div>
      ${tree.description ? `<p class="desc">${esc(tree.description)}</p>` : ""}
      <dl class="kv">
        <dt>入口节点</dt><dd><span class="mono">${esc(entry || "-")}</span></dd>
        <dt>plugins</dt><dd>${plugins || '<span class="muted">无（走默认执行器链）</span>'}</dd>
        <dt>allow_toolset</dt><dd>${(tree.allow_toolset || []).length
          ? chips(tree.allow_toolset) : '<span class="muted">无（不授权任何工具集）</span>'}</dd>
        ${meta && meta.max_steps != null ? `<dt>max_steps</dt><dd>${meta.max_steps}（AGENT 图步数预算）</dd>` : ""}
        ${(tree.stages || []).length ? `<dt>stages 骨架</dt><dd>${tree.stages.map(
          (s) => Object.keys(s).map((k) => `<span class="chip">${esc(k)} → <b class="mono">${esc(s[k])}</b></span>`).join(" ")
        ).join(" → ")}</dd>` : ""}
        ${(tree.config && Object.keys(tree.config).length) ? `<dt>config</dt><dd>${chips(Object.keys(tree.config))}</dd>` : ""}
      </dl>
    </div>
    <div class="panel">
      <h2>节点（${nodes.length}）</h2>
      ${nodes.length ? renderNodesTable(nodes, entry) : '<div class="empty">无节点</div>'}
    </div>`;
}

function renderNodesTable(nodes, entry) {
  return `
    <table class="tbl" style="margin-top:14px">
      <thead><tr><th>节点</th><th>名称</th><th>槽位</th><th>后继（sub_nodes）</th>
        <th>工具</th><th>终态</th><th>详情</th></tr></thead>
      <tbody>${nodes.map((n) => `
        <tr>
          <td class="num">${esc(n.code)}${n.code === entry ? ' <span class="badge" style="background:#fffbeb;border-color:#d97706;color:#92400e">⏵</span>' : ""}</td>
          <td>${esc(n.name || "-")}</td>
          <td>${chips(Object.keys(n.slots || {}))}</td>
          <td>${chips(n.sub_nodes)}</td>
          <td>${(n.use_tools || []).length ? chips(n.use_tools) : '<span class="muted">-</span>'}</td>
          <td>${n.is_end ? '<span class="badge end">终态</span>' : ""}</td>
          <td>${(n.answer_examples || []).length || n.description || n.task_description
              || (n.stages && Object.keys(n.stages).length)
              || (n.plugins && Object.keys(n.plugins).length)
              || (n.config && Object.keys(n.config).length) ? `
            <details class="sec"><summary>展开</summary>
              ${n.description ? `<p class="desc" style="margin:6px 0"><b>描述（入 NLG）：</b>${esc(n.description)}</p>` : ""}
              ${n.task_description ? `<p class="desc" style="margin:6px 0"><b>代办（入 NLU）：</b>${esc(n.task_description)}</p>` : ""}
              ${Object.keys(n.slots || {}).length ? `<p class="desc" style="margin:6px 0"><b>槽位定义：</b>${Object.entries(n.slots).map(([k, v]) => `<span class="chip">${esc(k)}: ${esc(v)}</span>`).join("")}</p>` : ""}
              ${(n.answer_examples || []).length ? `<p class="desc" style="margin:6px 0"><b>回答范式：</b></p>${n.answer_examples.map((x) => `<div class="desc" style="margin:2px 0">· ${esc(x)}</div>`).join("")}` : ""}
              ${(n.stages && Object.keys(n.stages).length) ? `<p class="desc" style="margin:6px 0"><b>stages 覆写：</b>${Object.entries(n.stages).map(([s, c]) => `<span class="chip">${esc(s)} → <b class="mono">${esc(c)}</b></span>`).join("")}</p>` : ""}
              ${(n.plugins && Object.keys(n.plugins).length) ? `<p class="desc" style="margin:6px 0"><b>plugins 覆写：</b>${Object.entries(n.plugins).map(([s, c]) => `<span class="chip">${esc(s)} → <b class="mono">${esc(c)}</b></span>`).join("")}</p>` : ""}
              ${(n.config && Object.keys(n.config).length) ? `<p class="desc" style="margin:6px 0"><b>config：</b>${chips(Object.keys(n.config))}</p>` : ""}
            </details>` : '<span class="muted">-</span>'}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderYamlTab(body, { yaml }) {
  body.innerHTML = `
    <div class="panel">
      <div class="row spread" style="margin-bottom:10px">
        <span class="muted">声明式 yml（与 CLI pattern-export 同格式，可被 pattern-load 消费）</span>
        <button class="btn btn-sm" id="btn-copy-yml2">复制</button>
      </div>
      <pre class="code">${esc(yaml)}</pre>
    </div>`;
  $("#btn-copy-yml2").onclick = () => copyText(yaml);
}

/* ============================== 视图：知识库 ============================== */

async function renderKnowledge(main) {
  main.innerHTML = `
    <div class="panel">
      <div class="scope-bar">
        <label class="muted" style="font-size:13px">空间（scope）</label>
        <select id="scope-sel"></select>
        <input type="text" id="scope-input" placeholder="或输入新空间 {channel}:{account_id}" style="width:230px">
        <button class="btn btn-sm" id="scope-apply">切换</button>
        <span class="muted" id="scope-counts" style="font-size:12.5px"></span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="scope-refresh">刷新</button>
        <button class="btn btn-sm btn-danger" id="scope-clear">清空空间</button>
      </div>
      <div class="notice">知识工具归属 <b class="mono">knowledge</b> 工具集——应用的 <b class="mono">pattern.allow_toolset</b> 引用该工具集、节点 <b class="mono">use_tools</b> 列出具体工具后才可调用（deny-by-default）。此处改动影响检索结果。检索为关键词匹配（jieba 分词），非语义检索，可在「试搜台」预演。</div>
      <div class="tabs" id="kb-tabs">
        <button data-tab="products" class="active">商品知识</button>
        <button data-tab="cs">客服知识</button>
        <button data-tab="search">试搜台</button>
      </div>
      <div id="kb-body"></div>
    </div>`;

  await refreshScopes();
  if (!state.scope && state.scopes.length) setScope(state.scopes[0].scope);

  $("#scope-apply").onclick = () => {
    const val = $("#scope-input").value.trim() || $("#scope-sel").value;
    if (!/^[^:\s]+:[^:\s]+$/.test(val)) return toast("scope 格式必须为 {channel}:{account_id}", false);
    setScope(val);
  };
  $("#scope-refresh").onclick = async () => { await refreshScopes(); activateKbTab(currentKbTab); };
  $("#scope-clear").onclick = () => {
    if (!state.scope) return;
    confirmDialog(
      `将删除空间 ${state.scope} 下的全部商品与客服知识，不可恢复。确认继续？`,
      async () => {
        const data = await api("/api/v1/console/knowledge/clear-scope",
          { method: "POST", body: { scope: state.scope } });
        toast(`已清空：商品 ${data.products_deleted} 条，客服知识 ${data.cs_deleted} 条`, true);
        await refreshScopes();
        activateKbTab(currentKbTab);
      });
  };

  let currentKbTab = "products";
  const kbTabs = { products: renderKbProducts, cs: renderKbCs, search: renderKbSearch };
  function activateKbTab(name) {
    currentKbTab = name;
    $$("#kb-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    kbTabs[name]($("#kb-body")).catch((err) => {
      $("#kb-body").innerHTML = `<div class="panel error">${esc(err.message)}</div>`;
    });
  }
  $$("#kb-tabs button").forEach((b) => b.onclick = () => activateKbTab(b.dataset.tab));
  activateKbTab("products");

  async function refreshScopes() {
    const data = await api("/api/v1/console/knowledge/scopes");
    state.scopes = data.scopes || [];
    const sel = $("#scope-sel");
    sel.innerHTML = state.scopes.map((s) =>
      `<option value="${esc(s.scope)}" ${s.scope === state.scope ? "selected" : ""}>${esc(s.scope)}</option>`).join("");
    renderScopeCounts();
  }
  function renderScopeCounts() {
    const s = state.scopes.find((x) => x.scope === state.scope);
    $("#scope-counts").textContent = s
      ? `商品 ${s.product_count} 条 · 客服 ${s.cs_count} 条 · 更新于 ${fmtTime(s.updated_at)}`
      : (state.scope ? "（新空间，暂无内容）" : "");
  }
  function setScope(scope) {
    state.scope = scope;
    localStorage.setItem("nexus_console_scope", scope);
    const sel = $("#scope-sel");
    if (![...sel.options].some((o) => o.value === scope)) {
      sel.insertAdjacentHTML("afterbegin",
        `<option value="${esc(scope)}" selected>${esc(scope)}（新）</option>`);
    } else sel.value = scope;
    $("#scope-input").value = "";
    renderScopeCounts();
    activateKbTab(currentKbTab);
  }
}

/* ---------- 知识库：商品 ---------- */

async function renderKbProducts(body) {
  if (!state.scope) { body.innerHTML = '<div class="empty">请先选择或输入一个空间（scope）</div>'; return; }
  const data = await api("/api/v1/console/knowledge/products",
    { params: { scope: state.scope, limit: 50 } });
  const rows = data.rows || [];
  body.innerHTML = `
    <div class="row spread" style="margin-bottom:10px">
      <input type="text" id="prod-query" placeholder="按关键词过滤本空间（走生产同款检索）" style="
        border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:300px">
      <button class="btn btn-primary btn-sm" id="prod-add">新增商品知识</button>
    </div>
    ${rows.length ? `
    <table class="tbl">
      <thead><tr><th>goods_id</th><th>名称</th><th>价格</th><th>已售</th><th>更新时间</th><th style="width:130px"></th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td class="num">${r.goods_id}</td>
          <td>${esc(r.goods_name)}</td>
          <td>${esc(r.price || "-")}</td>
          <td class="num">${r.sold_quantity == null ? "-" : r.sold_quantity}</td>
          <td class="muted">${fmtTime(r.updated_at)}</td>
          <td><button class="btn btn-sm" data-edit="${r.goods_id}">编辑</button>
              <button class="btn btn-sm btn-danger" data-del="${r.goods_id}">删除</button></td>
        </tr>`).join("")}
      </tbody>
    </table>
    <div class="muted" style="font-size:12px;margin-top:8px">默认展示最近 50 条；提取正文/参数等大字段在编辑弹窗内维护。</div>`
    : '<div class="empty">本空间暂无商品知识——点右上角「新增」录入，或经导入（后续版本）。</div>'}`;

  $("#prod-add").onclick = () => productForm(null);
  $$("[data-edit]", body).forEach((b) => b.onclick = async () => {
    const row = rows.find((r) => String(r.goods_id) === b.dataset.edit);
    productForm(row);
  });
  $$("[data-del]", body).forEach((b) => b.onclick = () =>
    confirmDialog(`删除商品知识 goods_id=${b.dataset.del}（${state.scope}）？`, async () => {
      await api("/api/v1/console/knowledge/products",
        { method: "DELETE", params: { scope: state.scope, goods_id: b.dataset.del } });
      toast("已删除", true);
      renderKbProducts(body);
    }));
  $("#prod-query").oninput = debounce(async (e) => {
    const q = e.target.value.trim();
    const d = await api("/api/v1/console/knowledge/products",
      { params: { scope: state.scope, query: q, limit: 50 } });
    // 简化：仅用结果数量提示，具体行渲染复用整表刷新
    if (!q) return renderKbProducts(body);
    const tb = $("table.tbl tbody", body);
    if (!tb) return renderKbProducts(body);
    tb.innerHTML = (d.rows || []).map((r) => `
      <tr><td class="num">${r.goods_id}</td><td>${esc(r.goods_name)}</td>
      <td>${esc(r.price || "-")}</td><td class="num">${r.sold_quantity == null ? "-" : r.sold_quantity}</td>
      <td class="muted">${fmtTime(r.updated_at)}</td>
      <td><button class="btn btn-sm" data-edit="${r.goods_id}">编辑</button>
          <button class="btn btn-sm btn-danger" data-del="${r.goods_id}">删除</button></td></tr>`).join("")
      || '<tr><td colspan="6" class="empty">无命中（关键词分词后按 AND 匹配名称/正文）</td></tr>';
    bindProductRowButtons(body);
  }, 350);

  function bindProductRowButtons(scopeRoot) {
    $$("[data-edit]", scopeRoot).forEach((b) => b.onclick = async () => {
      const d = await api("/api/v1/console/knowledge/products",
        { params: { scope: state.scope, goods_id: b.dataset.edit } });
      productForm((d.rows || [])[0]);
    });
    $$("[data-del]", scopeRoot).forEach((b) => b.onclick = () =>
      confirmDialog(`删除商品知识 goods_id=${b.dataset.del}（${state.scope}）？`, async () => {
        await api("/api/v1/console/knowledge/products",
          { method: "DELETE", params: { scope: state.scope, goods_id: b.dataset.del } });
        toast("已删除", true);
        renderKbProducts(body);
      }));
  }

  function productForm(row) {
    const isEdit = !!row;
    if (isEdit && !row) return toast("读取商品失败", false);
    const numOrNull = (v) => (v === "" || v == null ? null : Number(v));
    openModal({
      title: isEdit ? `编辑商品知识 · goods_id=${row.goods_id}` : "新增商品知识",
      body: `
        <div class="form-row"><label class="req">goods_id</label>
          <input type="number" name="goods_id" required value="${isEdit ? row.goods_id : ""}" ${isEdit ? "disabled" : ""}>
          <div class="form-hint">${isEdit ? "创建后不可改（scope + goods_id 是唯一键）" : "同 scope + goods_id 已存在则更新"}</div></div>
        <div class="form-row"><label class="req">商品名称（进入检索）</label>
          <input type="text" name="goods_name" required maxlength="512" value="${esc(isEdit ? row.goods_name : "")}"></div>
        <div class="row">
          <div class="form-row" style="flex:1"><label>价格</label>
            <input type="text" name="price" maxlength="64" value="${esc(isEdit ? row.price || "" : "")}"></div>
          <div class="form-row" style="flex:1"><label>已售数量</label>
            <input type="number" name="sold_quantity" value="${isEdit && row.sold_quantity != null ? row.sold_quantity : ""}"></div>
        </div>
        <div class="form-row"><label>参数规格（JSON 文本，可留空）</label>
          <textarea name="specifications" class="tall">${esc(isEdit ? row.specifications || "" : "")}</textarea></div>
        <div class="form-row"><label>提取正文（markdown，进入检索）</label>
          <textarea name="extracted_content" class="tall">${esc(isEdit ? row.extracted_content || "" : "")}</textarea>
          <div class="form-hint">留空提交 = 清空该字段（编辑时）；内容会经注入防护清洗后进入 LLM 上下文。</div></div>`,
      submitText: isEdit ? "保存修改" : "创建",
      onSubmit: async (v) => {
        if (v.specifications.trim()) {
          try { JSON.parse(v.specifications); }
          catch (e) { throw new Error("参数规格不是合法 JSON：" + e.message); }
        }
        const clean = (s) => (s === "" ? null : s);
        if (isEdit) {
          await api("/api/v1/console/knowledge/products", {
            method: "PUT",
            params: { scope: state.scope, goods_id: row.goods_id },
            body: {
              goods_name: v.goods_name,
              price: clean(v.price),
              sold_quantity: numOrNull(v.sold_quantity),
              specifications: clean(v.specifications),
              extracted_content: clean(v.extracted_content),
            },
          });
        } else {
          await api("/api/v1/console/knowledge/products", {
            method: "POST",
            body: {
              scope: state.scope,
              goods_id: Number(v.goods_id),
              goods_name: v.goods_name,
              price: clean(v.price),
              sold_quantity: numOrNull(v.sold_quantity),
              specifications: clean(v.specifications),
              extracted_content: clean(v.extracted_content),
            },
          });
        }
        toast(isEdit ? "已保存（新会话起生效）" : "已创建", true);
        renderKbProducts(body);
      },
    });
  }
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* ---------- 知识库：客服知识 ---------- */

async function renderKbCs(body) {
  if (!state.scope) { body.innerHTML = '<div class="empty">请先选择或输入一个空间（scope）</div>'; return; }
  const data = await api("/api/v1/console/knowledge/cs-entries",
    { params: { scope: state.scope } });
  const rows = data.rows || [];
  body.innerHTML = `
    <div class="row spread" style="margin-bottom:10px">
      <span class="muted" style="font-size:12.5px">停用条目不参与检索（管理口径仍可见）</span>
      <button class="btn btn-primary btn-sm" id="cs-add">新增客服知识</button>
    </div>
    ${rows.length ? `
    <table class="tbl">
      <thead><tr><th>标题</th><th>标签</th><th>启用</th><th>更新时间</th><th style="width:130px"></th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td><b>${esc(r.title)}</b><div class="muted" style="font-size:12px;max-width:420px;
            overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.content)}</div></td>
          <td>${chips((r.tags || "").split(/[,，]/).filter(Boolean))}</td>
          <td><label class="switch"><input type="checkbox" data-toggle="${r.id}" ${r.enabled ? "checked" : ""}><span class="slider"></span></label></td>
          <td class="muted">${fmtTime(r.updated_at)}</td>
          <td><button class="btn btn-sm" data-edit="${r.id}">编辑</button>
              <button class="btn btn-sm btn-danger" data-del="${r.id}">删除</button></td>
        </tr>`).join("")}
      </tbody>
    </table>` : '<div class="empty">本空间暂无客服知识</div>'}`;

  const reload = () => renderKbCs(body);

  $("#cs-add").onclick = () => csForm(null, reload);
  $$("[data-edit]", body).forEach((b) => b.onclick = () =>
    csForm(rows.find((r) => String(r.id) === b.dataset.edit), reload));
  $$("[data-del]", body).forEach((b) => b.onclick = () =>
    confirmDialog("删除该条客服知识？", async () => {
      await api(`/api/v1/console/knowledge/cs-entries/${b.dataset.del}`, { method: "DELETE" });
      toast("已删除", true);
      reload();
    }));
  $$("[data-toggle]", body).forEach((t) => t.onchange = async () => {
    try {
      await api(`/api/v1/console/knowledge/cs-entries/${t.dataset.toggle}`,
        { method: "PUT", body: { enabled: t.checked } });
      toast(t.checked ? "已启用（参与检索）" : "已停用（不再参与检索）", true);
    } catch (e) {
      toast(e.message, false);
      t.checked = !t.checked;
    }
  });

  function csForm(row, reloadFn) {
    const isEdit = !!row;
    openModal({
      title: isEdit ? "编辑客服知识" : "新增客服知识",
      body: `
        <div class="form-row"><label class="req">标题（进入检索）</label>
          <input type="text" name="title" required maxlength="256" value="${esc(isEdit ? row.title : "")}"></div>
        <div class="form-row"><label class="req">内容（进入检索）</label>
          <textarea name="content" required>${esc(isEdit ? row.content : "")}</textarea></div>
        <div class="form-row"><label>标签（逗号分隔，仅作管理分类）</label>
          <input type="text" name="tags" maxlength="256" value="${esc(isEdit ? row.tags || "" : "")}"></div>
        <div class="form-row"><label>启用</label>
          <label class="row" style="gap:6px"><input type="checkbox" name="enabled" ${!isEdit || row.enabled ? "checked" : ""}> 参与检索</label></div>`,
      submitText: isEdit ? "保存修改" : "创建",
      onSubmit: async (v) => {
        const clean = (s) => (s === "" ? null : s);
        if (isEdit) {
          await api(`/api/v1/console/knowledge/cs-entries/${row.id}`, {
            method: "PUT",
            body: { title: v.title, content: v.content, tags: clean(v.tags), enabled: v.enabled },
          });
        } else {
          await api("/api/v1/console/knowledge/cs-entries", {
            method: "POST",
            body: { scope: state.scope, title: v.title, content: v.content, tags: clean(v.tags), enabled: v.enabled },
          });
        }
        toast(isEdit ? "已保存" : "已新增", true);
        reloadFn();
      },
    });
  }
}

/* ---------- 知识库：试搜台 ---------- */

async function renderKbSearch(body) {
  if (!state.scope) { body.innerHTML = '<div class="empty">请先选择或输入一个空间（scope）</div>'; return; }
  body.innerHTML = `
    <div class="row" style="margin-bottom:6px">
      <input type="text" id="st-query" placeholder="输入买家口吻的查询，如：这手机电池怎么样" style="
        border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:340px">
      <input type="number" id="st-goods" placeholder="或按 goods_id 精确查" style="
        border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:170px">
      <select id="st-limit" style="border:1px solid var(--border);border-radius:8px;padding:7px;font-size:13px">
        <option>5</option><option selected>10</option><option>20</option><option>50</option>
      </select>
      <button class="btn btn-primary btn-sm" id="st-run">检索</button>
    </div>
    <div class="muted" style="font-size:12px;margin-bottom:12px">
      走生产同款检索（jieba 分词、关键词 LIKE）；多词之间 AND，名称/正文之间 OR。结果与运行时 agent 看到的一致。</div>
    <div id="st-result"></div>`;
  $("#st-run").onclick = runSearch;
  $("#st-query").onkeydown = (e) => { if (e.key === "Enter") runSearch(); };

  async function runSearch() {
    const query = $("#st-query").value.trim();
    const goodsId = $("#st-goods").value.trim();
    const box = $("#st-result");
    box.innerHTML = '<div class="empty">检索中…</div>';
    try {
      const data = await api("/api/v1/console/knowledge/search-test", {
        method: "POST",
        body: {
          scope: state.scope, query,
          goods_id: goodsId === "" ? null : Number(goodsId),
          limit: Number($("#st-limit").value),
        },
      });
      const products = data.products || [], cs = data.cs_entries || [];
      box.innerHTML = `
        <div class="row" style="margin:8px 0">
          <span class="muted" style="font-size:12.5px">分词：</span>
          ${(data.tokens || []).map((t) => `<span class="chip tok">${esc(t)}</span>`).join("")
            || '<span class="muted" style="font-size:12.5px">（无 / 精确模式）</span>'}
        </div>
        <h3 style="margin:14px 0 6px">商品知识（${products.length}）</h3>
        ${products.length ? `<table class="tbl">
          <thead><tr><th>goods_id</th><th>名称</th><th>命中解释</th></tr></thead>
          <tbody>${products.map((r) => `
            <tr><td class="num">${r.goods_id}</td><td>${esc(r.goods_name)}
              <div class="muted" style="font-size:12px">${esc((r.extracted_content || "").slice(0, 80))}${(r.extracted_content || "").length > 80 ? "…" : ""}</div></td>
            <td>${matchChips(r._match) || '<span class="muted">goods_id 精确命中</span>'}</td></tr>`).join("")}
          </tbody></table>` : '<div class="empty">无命中</div>'}
        <h3 style="margin:14px 0 6px">客服知识（${cs.length}）</h3>
        ${cs.length ? `<table class="tbl">
          <thead><tr><th>标题</th><th>内容</th><th>命中解释</th></tr></thead>
          <tbody>${cs.map((r) => `
            <tr><td><b>${esc(r.title)}</b></td>
            <td style="max-width:360px">${esc((r.content || "").slice(0, 120))}${(r.content || "").length > 120 ? "…" : ""}</td>
            <td>${matchChips(r._match)}</td></tr>`).join("")}
          </tbody></table>` : '<div class="empty">无命中</div>'}
        ${(!products.length && !cs.length) ? `
          <div class="notice">没有命中。检索是关键词匹配：请检查词条里是否包含查询中的词（分词后按 AND 叠加），或换更短的关键词。</div>` : ""}`;
    } catch (e) {
      box.innerHTML = `<div class="panel error">${esc(e.message)}</div>`;
    }
  }
}

/* ============================== 视图：RAG 检索配置 ============================== */

/* 配置对象 = clarify 召回管线的声明式装配（保存后重注册 stage，下一轮生效）。
 * 三段式：召回通路（接知识库）→ 过滤/融合/重排 → 门控规则（kb/mixed/fallback）。
 */
const RAG_PATH_TYPES = [
  { type: "kb_cs", label: "客服知识（title+content 检索，tags=业务关键词）" },
  { type: "kb_products", label: "商品知识（goods_name+extracted_content）" },
];
const MODE_BADGES = {
  kb: '<span class="badge fsm">kb · 高置信直答</span>',
  mixed: '<span class="badge" style="background:#fffbeb;border-color:#d97706;color:#92400e">mixed · 部分知识+拉回</span>',
  fallback: '<span class="badge">fallback · 无召回兜底</span>',
};

let ragPathsModel = [];

function renderRagPathRows() {
  const tbody = $("#rag-paths");
  tbody.innerHTML = ragPathsModel.map((p, i) => `
    <tr>
      <td><select data-pf="type" data-i="${i}">${RAG_PATH_TYPES.map((t) =>
        `<option value="${t.type}" ${p.type === t.type ? "selected" : ""}>${t.label}</option>`).join("")}</select></td>
      <td><input type="text" data-pf="name" data-i="${i}" value="${esc(p.name)}" style="width:110px"></td>
      <td><input type="text" data-pf="scope" data-i="${i}" value="${esc(p.scope)}" style="width:150px" placeholder="channel:account"></td>
      <td><input type="number" data-pf="weight" data-i="${i}" value="${p.weight}" step="0.1" min="0" max="10" style="width:80px"></td>
      <td><input type="number" data-pf="top_k" data-i="${i}" value="${p.top_k}" min="1" max="50" style="width:70px"></td>
      <td><button class="btn btn-sm btn-danger" data-pdel="${i}">删除</button></td>
    </tr>`).join("")
    || '<tr><td colspan="6" class="empty">无召回通路（= 内置默认行为：召回为空，澄清恒走 fallback）</td></tr>';
  $$("#rag-paths [data-pf]").forEach((el) => el.onchange = () => {
    const i = Number(el.dataset.i), f = el.dataset.pf;
    ragPathsModel[i][f] = f === "weight" ? Number(el.value) : f === "top_k" ? Number(el.value) : el.value;
  });
  $$("#rag-paths [data-pdel]").forEach((b) => b.onclick = () => {
    ragPathsModel.splice(Number(b.dataset.pdel), 1);
    renderRagPathRows();
  });
}

function collectRagConfig(root) {
  const filters = [];
  if ($("#rag-dedup-on", root).checked)
    filters.push({ type: "dedup", by: $("#rag-dedup-by", root).value });
  if ($("#rag-threshold-on", root).checked)
    filters.push({ type: "score_threshold", threshold: Number($("#rag-threshold", root).value) });
  if ($("#rag-max-on", root).checked)
    filters.push({ type: "max_results", max_results: Number($("#rag-max", root).value) });
  const fusion = { type: $("#rag-fusion", root).value };
  if (fusion.type === "rrf") fusion.k = Number($("#rag-rrf-k", root).value);
  const reranker = { type: $("#rag-reranker", root).value };
  if (reranker.type === "diversity") reranker.lambda_param = Number($("#rag-lambda", root).value);
  return {
    recall_paths: ragPathsModel,
    filters,
    fusion,
    reranker,
    rule: {
      t_high: Number($("#rag-t-high", root).value),
      t_low: Number($("#rag-t-low", root).value),
      keyword_bonus: Number($("#rag-bonus", root).value),
    },
  };
}

async function renderRag(main) {
  const data = await api("/api/v1/console/rag/config");
  const cfg = data.config;
  ragPathsModel = (cfg.recall_paths || []).map((p) => ({ ...p }));
  const findFilter = (t) => (cfg.filters || []).find((f) => f.type === t);

  main.innerHTML = `
    <div class="panel">
      <h2>说明</h2>
      <p class="desc" style="margin-bottom:4px">配置 <b>clarify 澄清召回管线</b>（MultiPathRecaller + 门控规则）：召回通路接知识库，命中打分后按阈值门控为
        <span class="badge fsm">kb 直答</span> / <span class="badge" style="background:#fffbeb;border-color:#d97706;color:#92400e">mixed</span> / <span class="badge">fallback</span> 三种模式。</p>
      <p class="desc" style="margin:0">生效范围：声明了 clarify 槽为 <span class="mono">rag_clarify</span> / <span class="mono">clarify_default</span> / <span class="mono">builtin:clarify</span> 的 pattern / 节点，
        保存后<b>下一对话轮生效</b>（无需重启）；install/repair 的 FAQ 澄清（自有装配）不受影响。
        当前来源：<b class="mono">${data.source === "file" ? "配置文件" : "内置默认"}</b>${data.file_error ? `，<span style="color:var(--danger)">文件解析失败：${esc(data.file_error)}</span>` : ""}（${esc(data.path)}）</p>
    </div>

    <div class="panel">
      <div class="row spread"><h2>召回通路</h2>
        <button class="btn btn-sm" id="rag-path-add">+ 添加通路</button></div>
      <table class="tbl" style="margin-top:8px">
        <thead><tr><th>类型</th><th>名称（融合权重索引）</th><th>scope</th><th>权重</th><th>top_k</th><th></th></tr></thead>
        <tbody id="rag-paths"></tbody>
      </table>
      <div class="muted" style="font-size:12px;margin-top:6px">打分 = (标题命中词数×2 + 正文命中词数) / (查询词数×3)，上限 1.0——全部词命中标题 ≈ 0.67，只命中正文 ≈ 0.33，与默认门控阈值对齐。</div>
    </div>

    <div class="panel">
      <h2>过滤 · 融合 · 重排</h2>
      <div class="row" style="gap:18px;margin:10px 0;flex-wrap:wrap">
        <label class="row" style="gap:6px"><input type="checkbox" id="rag-dedup-on" ${findFilter("dedup") ? "checked" : ""}> 去重（按
          <select id="rag-dedup-by" style="border:1px solid var(--border);border-radius:6px;padding:3px">
            <option ${(findFilter("dedup") || {}).by === "id" ? "selected" : ""}>id</option>
            <option ${(findFilter("dedup") || {}).by === "content" ? "selected" : ""}>content</option>
          </select>）</label>
        <label class="row" style="gap:6px"><input type="checkbox" id="rag-threshold-on" ${findFilter("score_threshold") ? "checked" : ""}> 分数阈值
          <input type="number" id="rag-threshold" value="${(findFilter("score_threshold") || {}).threshold ?? 0.1}" step="0.05" min="0" max="1" style="width:76px"></label>
        <label class="row" style="gap:6px"><input type="checkbox" id="rag-max-on" ${findFilter("max_results") ? "checked" : ""}> 最多保留
          <input type="number" id="rag-max" value="${(findFilter("max_results") || {}).max_results ?? 5}" min="1" max="100" style="width:70px"> 条</label>
      </div>
      <dl class="kv">
        <dt>融合策略</dt><dd><div class="row">
          <select id="rag-fusion" style="border:1px solid var(--border);border-radius:8px;padding:7px">
            <option value="weighted" ${cfg.fusion.type === "weighted" ? "selected" : ""}>加权分数（weighted）</option>
            <option value="rrf" ${cfg.fusion.type === "rrf" ? "selected" : ""}>倒数排名（RRF）</option>
            <option value="round_robin" ${cfg.fusion.type === "round_robin" ? "selected" : ""}>轮流取数（round_robin）</option>
          </select>
          <span id="rag-rrf-wrap" style="display:none" class="row">k <input type="number" id="rag-rrf-k" value="${cfg.fusion.k ?? 60}" min="1" style="width:80px"></span>
        </div></dd>
        <dt>重排策略</dt><dd><div class="row">
          <select id="rag-reranker" style="border:1px solid var(--border);border-radius:8px;padding:7px">
            <option value="score" ${cfg.reranker.type === "score" ? "selected" : ""}>按分数（score）</option>
            <option value="diversity" ${cfg.reranker.type === "diversity" ? "selected" : ""}>MMR 多样性（diversity）</option>
          </select>
          <span id="rag-lambda-wrap" style="display:none" class="row">λ <input type="number" id="rag-lambda" value="${cfg.reranker.lambda_param ?? 0.7}" step="0.1" min="0" max="1" style="width:70px"></span>
        </div></dd>
      </dl>
    </div>

    <div class="panel">
      <h2>门控规则（kb / mixed / fallback 分界）</h2>
      <div class="row" style="gap:18px;margin-top:8px">
        <label>t_high <input type="number" id="rag-t-high" value="${cfg.rule.t_high}" step="0.05" min="0" max="1" style="width:80px"></label>
        <label>t_low <input type="number" id="rag-t-low" value="${cfg.rule.t_low}" step="0.05" min="0" max="1" style="width:80px"></label>
        <label>keyword_bonus <input type="number" id="rag-bonus" value="${cfg.rule.keyword_bonus}" step="0.05" min="0" max="1" style="width:80px"></label>
        <span class="muted" style="font-size:12px">top 分 ≥ t_high → kb；≥ t_low → mixed；否则 fallback。topic/keywords 与条目业务关键词重叠时 top 分 +bonus。</span>
      </div>
    </div>

    <div class="panel row spread">
      <span class="muted" style="font-size:12.5px">保存 = 校验 + 落盘 ${esc(data.path)} + 重注册（立即生效）；重启后自动加载。</span>
      <div class="row">
        <button class="btn btn-danger" id="rag-reset">恢复默认</button>
        <button class="btn btn-primary" id="rag-save">保存并生效</button>
      </div>
    </div>

    <div class="panel">
      <h2>试跑台（离线 · 零 LLM）</h2>
      <p class="desc">按<b>当前表单</b>的配置试跑召回 + 门控（不落盘），验证参数效果后再保存。</p>
      <div class="row" style="margin-bottom:8px">
        <input type="text" id="ragt-query" placeholder="查询（模拟用户问题）" style="border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:320px">
        <input type="text" id="ragt-topic" placeholder="topic（可选，NLU 槽）" style="border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:160px">
        <input type="text" id="ragt-keywords" placeholder="keywords（逗号分隔，可选）" style="border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:220px">
        <button class="btn btn-primary btn-sm" id="ragt-run">试跑</button>
      </div>
      <div id="ragt-result"></div>
    </div>`;

  renderRagPathRows();
  $("#rag-path-add").onclick = () => {
    ragPathsModel.push({ type: "kb_cs", name: `path_${ragPathsModel.length + 1}`,
                         scope: state.scope || "", weight: 1.0, top_k: 5 });
    renderRagPathRows();
  };
  $("#rag-fusion").onchange = (e) =>
    $("#rag-rrf-wrap").style.display = e.target.value === "rrf" ? "" : "none";
  if (cfg.fusion.type === "rrf") $("#rag-rrf-wrap").style.display = "";
  $("#rag-reranker").onchange = (e) =>
    $("#rag-lambda-wrap").style.display = e.target.value === "diversity" ? "" : "none";
  if (cfg.reranker.type === "diversity") $("#rag-lambda-wrap").style.display = "";

  $("#rag-save").onclick = async () => {
    try {
      const data2 = await api("/api/v1/console/rag/config", {
        method: "PUT", body: { config: collectRagConfig(main) },
      });
      toast("已保存并生效（下一对话轮起）", true);
    } catch (e) { toast(e.message, false); }
  };
  $("#rag-reset").onclick = () =>
    confirmDialog("恢复内置默认装配（删除配置文件，召回通路清空）？", async () => {
      await api("/api/v1/console/rag/reset", { method: "POST" });
      toast("已恢复默认", true);
      renderRag(main);
    });
  $("#ragt-run").onclick = ragTestRun;
  $("#ragt-query").onkeydown = (e) => { if (e.key === "Enter") ragTestRun(); };

  async function ragTestRun() {
    const query = $("#ragt-query").value.trim();
    if (!query) return toast("请输入查询", false);
    const box = $("#ragt-result");
    box.innerHTML = '<div class="empty">试跑中…</div>';
    try {
      const out = await api("/api/v1/console/rag/test-run", {
        method: "POST",
        body: {
          config: collectRagConfig(main), query,
          topic: $("#ragt-topic").value.trim(),
          keywords: $("#ragt-keywords").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        },
      });
      box.innerHTML = `
        <div class="row" style="margin:8px 0;gap:14px">
          <span>门控结果：${MODE_BADGES[out.mode] || esc(out.mode)}</span>
          <span class="muted">top 分 <b>${out.top_score == null ? "-" : out.top_score}</b>
            （t_high=${out.rule.t_high}, t_low=${out.rule.t_low}, bonus=${out.rule.keyword_bonus}）</span>
          <span class="muted" style="font-size:12px">分词：${(out.tokens || []).map((t) => `<span class="chip tok">${esc(t)}</span>`).join("") || "-"}</span>
        </div>
        <div class="row" style="margin:6px 0">${(out.per_path || []).map((p) =>
          `<span class="chip">${esc(p.name)}: 召回 ${p.count} 条（w=${p.weight}, top_k=${p.top_k}）</span>`).join("")
          || '<span class="muted">无召回通路</span>'}</div>
        ${(out.results || []).length ? `<table class="tbl">
          <thead><tr><th>id</th><th>内容</th><th>分数</th><th>来源</th><th>业务关键词</th></tr></thead>
          <tbody>${out.results.map((r) => `
            <tr><td class="num">${esc(r.id)}</td>
            <td style="max-width:380px">${esc((r.content || "").slice(0, 120))}</td>
            <td class="num">${r.score}</td><td class="num">${esc(r.source)}</td>
            <td>${chips(r.keywords)}</td></tr>`).join("")}
          </tbody></table>`
          : '<div class="empty">无召回结果（门控将走 fallback）</div>'}
        <div class="muted" style="font-size:12px;margin-top:6px">${esc(out.scoring_note || "")}</div>`;
    } catch (e) {
      box.innerHTML = `<div class="panel error">${esc(e.message)}</div>`;
    }
  }
}



/* ============================== 启动 ============================== */

$("#api-key").value = state.key;
$("#api-key-save").onclick = () => {
  state.key = $("#api-key").value.trim();
  localStorage.setItem("nexus_console_key", state.key);
  toast(state.key ? "密钥已保存" : "已清除密钥", true);
  route();
};
$("#api-key").onkeydown = (e) => { if (e.key === "Enter") $("#api-key-save").click(); };

window.addEventListener("hashchange", route);
route();
