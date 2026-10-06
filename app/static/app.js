/* 事故复盘时间线 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  reviews: [],
  current: null,      // 服务器返回的复盘快照 {review, events, relations, attachments, computation}
  editEvents: [],     // 编辑中的事件 [{key, id?, name, earliest_start, latest_start, duration_minutes, note}]
  editRelations: [],  // 编辑中的关系 [{predecessor_key, successor_key, min_gap_minutes, max_gap_minutes}]
  seq: 0,
  merge: null,        // 并入流程的临时状态 {sourceId, version, detail, selected:Set}
  mergePreview: null, // 最近一次并入预览响应（确认时回传 digest）
};

function newKey() { return "k" + (++state.seq) + "_" + Date.now(); }

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || ("HTTP " + resp.status));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function showBanner(html) {
  const b = $("#banner");
  b.innerHTML = html;
  b.classList.remove("hidden");
}
function hideBanner() { $("#banner").classList.add("hidden"); }

/* ---------------------------------------------------------------- 加载 */

async function loadReviews(selectId) {
  const data = await api("GET", "/api/reviews");
  state.reviews = data.reviews;
  const sel = $("#review-select");
  sel.innerHTML = "";
  for (const r of state.reviews) {
    const opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = `#${r.id} ${r.title}（v${r.version}${r.status === "finalized" ? "·已定稿" : ""}）`;
    sel.appendChild(opt);
  }
  if (selectId) sel.value = selectId;
  $("#empty").classList.toggle("hidden", state.reviews.length > 0);
}

function openReview(id) {
  return api("GET", "/api/reviews/" + id).then(async (bundle) => {
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    try {
      await loadVersions();
    } catch (err) {
      showBanner("⚠ 版本列表加载失败：" + escapeHtml(err.message));
    }
    await loadMergeReviews();
  });
}

function resetEdits() {
  const b = state.current;
  state.editEvents = b.events.map((e) => ({
    key: "id_" + e.id,
    id: e.id,
    uid: e.uid || "",   // 事件持续身份：保存时回传，跨版本对照按它配对
    origin: e.origin || null,  // 并入事件的来源摘要（只读展示，不参与编辑/提交）
    name: e.name,
    timezone: e.timezone || "",
    earliest_start: e.earliest_start || "",
    latest_start: e.latest_start || "",
    duration_minutes: e.duration_minutes === null ? "" : e.duration_minutes,
    note: e.note || "",
  }));
  const idToKey = {};
  for (const e of state.editEvents) if (e.id) idToKey[e.id] = e.key;
  state.editRelations = b.relations.map((r) => ({
    predecessor_key: idToKey[r.predecessor_id],
    successor_key: idToKey[r.successor_id],
    min_gap_minutes: r.min_gap_minutes === null || r.min_gap_minutes === undefined ? 0 : r.min_gap_minutes,
    max_gap_minutes: r.max_gap_minutes === null || r.max_gap_minutes === undefined ? "" : r.max_gap_minutes,
  })).filter((r) => r.predecessor_key && r.successor_key);
}

/* ------------------------------------------------------------ 附件（现场证据） */

function attachmentsByEvent() {
  const map = {};
  for (const a of (state.current.attachments || [])) {
    (map[a.event_uid] = map[a.event_uid] || []).push(a);
  }
  return map;
}

function fmtSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(2) + " MB";
}

function shortSha(sha) {
  return sha ? sha.slice(0, 8) + "…" + sha.slice(-6) : "";
}

/* 客户端预检（服务端仍会强校验）：类型与 20MB 上限，避免明显无效的上传往返 */
const ALLOWED_ATT_EXT = /\.(txt|log|md|csv|json|png|jpg|jpeg|pdf)$/i;
function checkAttachmentFile(file) {
  if (!file) return "请选择要上传的文件";
  if (!ALLOWED_ATT_EXT.test(file.name)) {
    return "仅支持文本（.txt/.log/.md/.csv/.json）、PNG、JPEG 或 PDF";
  }
  if (file.size === 0) return "不允许上传空文件";
  if (file.size > 20 * 1024 * 1024) return "单文件不得超过 20MB（当前 " + fmtSize(file.size) + "）";
  return null;
}

function attachmentListHtml(atts) {
  if (!atts || !atts.length) return `<div class="hint att-empty">无附件</div>`;
  return atts.map((a) =>
    `<div class="att-item" data-att="${a.id}">` +
      `<a class="att-name" href="${a.download_url}" title="下载 ${escapeHtml(a.filename)}" download="${escapeHtml(a.filename)}">${fileIcon(a)} ${escapeHtml(a.filename)}</a>` +
      `<div class="att-meta hint" title="SHA-256：${a.blob_sha256}">${fmtSize(a.size)} · <code>${shortSha(a.blob_sha256)}</code>` +
      (a.uploaded_at ? ` · ${escapeHtml(String(a.uploaded_at).replace("T", " "))}` : "") + `</div>` +
      (a.description ? `<div class="att-desc">${escapeHtml(a.description)}</div>` : "") +
      `<button class="att-del danger" title="移除当前附件（旧版本仍保留可下载）">移除</button>` +
    `</div>`).join("");
}

function fileIcon(a) {
  const ct = a.content_type || "";
  if (ct.startsWith("image/")) return "🖼️";
  if (ct === "application/pdf") return "📄";
  return "📝";
}

function renderAttachmentCell(td, e) {
  const grouped = attachmentsByEvent();
  td.innerHTML = `<div class="att-box"></div>`;
  const box = td.querySelector(".att-box");
  const atts = grouped[e.uid] || [];
  const list = document.createElement("div");
  list.className = "att-list";
  list.innerHTML = attachmentListHtml(atts);
  box.appendChild(list);

  const bar = document.createElement("div");
  bar.className = "att-upload";
  if (!e.uid) {
    // 尚未保存的事件没有服务端持续身份：不能上传附件
    bar.innerHTML = `<span class="hint">保存事件后可上传附件</span>`;
  } else {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".txt,.log,.md,.csv,.json,.png,.jpg,.jpeg,.pdf";
    const desc = document.createElement("input");
    desc.type = "text";
    desc.placeholder = "附件说明（可选）";
    desc.className = "att-desc-input";
    const btn = document.createElement("button");
    btn.className = "att-upload-btn";
    btn.textContent = "⬆ 上传";
    btn.addEventListener("click", () => uploadAttachment(e, input.files[0], desc.value, btn));
    bar.appendChild(input);
    bar.appendChild(desc);
    bar.appendChild(btn);
  }
  box.appendChild(bar);

  td.querySelectorAll(".att-del").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = btn.closest(".att-item");
      const id = parseInt(item.dataset.att, 10);
      const att = (state.current.attachments || []).find((x) => x.id === id);
      if (!att) return;
      if (!confirm(`移除附件「${att.filename}」吗？\n只会影响新保存的版本；历史版本中该附件仍保留、可下载。`)) return;
      removeAttachment(id, btn);
    });
  });
}

/* 当前表单（editEvents/editRelations）相对已保存状态是否有未保存改动 */
function hasUnsavedEdits() {
  const cur = state.current;
  if (!cur) return false;
  const idToKey = {};
  for (const ev of cur.events) idToKey[ev.id] = "id_" + ev.id;
  const savedRels = (cur.relations || []).map((r) => ({
    predecessor_key: idToKey[r.predecessor_id],
    successor_key: idToKey[r.successor_id],
    min_gap_minutes: r.min_gap_minutes,
    max_gap_minutes: r.max_gap_minutes === null ? "" : r.max_gap_minutes,
  }));
  if (state.editEvents.length !== cur.events.length) return true;
  for (const e of state.editEvents) {
    const s = cur.events.find((x) => ("id_" + x.id) === e.key);
    if (!s) return true;
    if ((e.name || "") !== (s.name || "")) return true;
    if ((e.timezone || "") !== (s.timezone || "")) return true;
    if ((e.earliest_start || "") !== (s.earliest_start || "")) return true;
    if ((e.latest_start || "") !== (s.latest_start || "")) return true;
    const ed = e.duration_minutes === "" ? null : e.duration_minutes;
    if (ed !== s.duration_minutes) return true;
    if ((e.note || "") !== (s.note || "")) return true;
  }
  if (state.editRelations.length !== savedRels.length) return true;
  for (let i = 0; i < state.editRelations.length; i++) {
    const a = state.editRelations[i], b = savedRels[i];
    if (a.predecessor_key !== b.predecessor_key || a.successor_key !== b.successor_key) return true;
    if ((a.min_gap_minutes === "" ? 0 : a.min_gap_minutes) !== b.min_gap_minutes) return true;
    if ((a.max_gap_minutes === "" ? "" : a.max_gap_minutes) !== b.max_gap_minutes) return true;
  }
  return false;
}

/* 附件操作成功后刷新：表单无未保存改动时仅同步版本/附件数据并重绘（输入保留）；
   有改动时重置为服务器新快照并提示旧表单改动需重新录入（附件已另存新版本）。 */
function refreshAfterAttachmentChange(bundle) {
  const keepForm = !hasUnsavedEdits();
  state.current = bundle;
  if (keepForm) {
    render();  // renderEventsTable 重建输入，但值取自 editEvents（未变）
  } else {
    resetEdits();
    showBanner("附件已另存为新版本；表单里尚未保存的事件/关系修改未包含在该版本中，请按需重新编辑保存。");
    render();
  }
  loadVersions();
  loadReviews(bundle.review.id);
}

/* 上传/移除都是独立的保存动作：以当前 base_version 原子另存新版本。
   成功后只刷新服务器状态并重绘附件/徽标，不清空正在编辑的事件表单输入。 */
async function uploadAttachment(e, file, description, btn) {
  const problem = checkAttachmentFile(file);
  if (problem) { showBanner("⚠ " + escapeHtml(problem)); return; }
  // 附件操作基于已保存版本另存新版本；先收起表单中对已保存事件的改名/时间修改
  // 到编辑态（未保存的新事件行本来就不能传附件，需要先点“保存并重新推算”）。
  syncFromDOM();
  const rid = state.current.review.id;
  const fd = new FormData();
  fd.append("base_version", String(state.current.review.version));
  if (description) fd.append("description", description.trim().slice(0, 1000));
  fd.append("file", file, file.name);
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/reviews/${rid}/events/${e.uid}/attachments`,
                             { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(data.error || ("HTTP " + resp.status));
      err.status = resp.status; err.data = data;
      throw err;
    }
    hideBanner();
    refreshAfterAttachmentChange(data);
  } catch (err) {
    handleSaveError(err);
  } finally {
    btn.disabled = false;
  }
}

async function removeAttachment(id, btn) {
  btn.disabled = true;
  try {
    const data = await api("DELETE",
      `/api/reviews/${state.current.review.id}/attachments/${id}`,
      { base_version: state.current.review.version });
    hideBanner();
    refreshAfterAttachmentChange(data);
  } catch (err) {
    handleSaveError(err);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 渲染 */

function eventName(key) {
  const e = state.editEvents.find((x) => x.key === key);
  return e ? e.name : "?";
}

function render() {
  const b = state.current;
  if (!b) return;
  $("#main").classList.remove("hidden");
  $("#empty").classList.add("hidden");
  $("#review-title").value = b.review.title;

  const comp = b.computation;
  const vb = $("#version-badge");
  vb.textContent = "版本 v" + b.review.version;
  const sb = $("#status-badge");
  if (b.review.status === "finalized") {
    sb.textContent = "已定稿"; sb.className = "badge ok";
  } else if (comp && comp.status === "conflict") {
    sb.textContent = "存在冲突·不得定稿"; sb.className = "badge err";
  } else if (comp && comp.result.drafts.length) {
    sb.textContent = "草稿·有暂存事件"; sb.className = "badge warn";
  } else {
    sb.textContent = "草稿"; sb.className = "badge";
  }
  $("#btn-finalize").disabled = b.review.status === "finalized";

  renderEventsTable();
  renderRelations();
  renderResults();
}

/* 时间输入：带时区用文本框（可含 UTC 偏移，如 2026-11-01T01:30-05:00）；
   未带时区用浏览器 datetime-local（朴素本地时间，旧语义）。 */
function timeInputHtml(field, tz) {
  if (tz) {
    return `<input data-f="${field}" class="tztime" type="text" inputmode="numeric"` +
      ` placeholder="YYYY-MM-DDTHH:MM±HH:MM">`;
  }
  return `<input data-f="${field}" type="datetime-local">`;
}

/* 并入事件的来源摘要：只读徽标（来源复盘/版本被删除后仍显示保存的摘要）。
   非并入事件（无 origin）返回空串，页面上不出现任何来源信息。 */
function originBadgeHtml(origin) {
  if (!origin) return "";
  const title =
    `来源复盘：#${origin.source_review_id} ${origin.source_review_title || ""}（v${origin.source_version}）\n` +
    `来源事件：「${origin.source_event_name || ""}」\n来源事件身份：${origin.source_event_uid}`;
  return `<div class="origin-tag" title="${escapeHtml(title)}">` +
    `⇲ 并入自 #${escapeHtml(String(origin.source_review_id))} ` +
    `${escapeHtml(origin.source_review_title || "（复盘已删除）")} ` +
    `v${escapeHtml(String(origin.source_version))} · 「${escapeHtml(origin.source_event_name || "（事件已删除）")}」</div>`;
}

function renderEventsTable() {
  const tbody = $("#events-table tbody");
  tbody.innerHTML = "";
  for (const e of state.editEvents) {
    const tr = document.createElement("tr");
    tr.dataset.key = e.key;
    tr.innerHTML =
      `<td><input data-f="name" type="text" value="">${originBadgeHtml(e.origin)}</td>` +
      `<td><input data-f="timezone" class="tz" type="text" list="tz-list"` +
      ` placeholder="留空=不指定"></td>` +
      `<td>${timeInputHtml("earliest_start", e.timezone)}</td>` +
      `<td>${timeInputHtml("latest_start", e.timezone)}</td>` +
      `<td><input data-f="duration_minutes" type="number" min="0" style="width:80px"></td>` +
      `<td><input data-f="note" type="text"></td>` +
      `<td class="att-cell"></td>` +
      `<td><button class="row-del" title="删除事件">✕</button></td>`;
    tr.querySelector('[data-f="name"]').value = e.name;
    tr.querySelector('[data-f="timezone"]').value = e.timezone;
    tr.querySelector('[data-f="earliest_start"]').value = e.earliest_start;
    tr.querySelector('[data-f="latest_start"]').value = e.latest_start;
    tr.querySelector('[data-f="duration_minutes"]').value = e.duration_minutes;
    tr.querySelector('[data-f="note"]').value = e.note;
    renderAttachmentCell(tr.querySelector(".att-cell"), e);
    // 时区改变会改变时间输入的形态（朴素 vs 带偏移）：先同步 DOM 再整体重绘
    tr.querySelector('[data-f="timezone"]').addEventListener("change", () => {
      syncFromDOM();
      renderEventsTable();
    });
    tr.querySelector(".row-del").addEventListener("click", () => {
      syncFromDOM();
      state.editEvents = state.editEvents.filter((x) => x.key !== e.key);
      state.editRelations = state.editRelations.filter(
        (r) => r.predecessor_key !== e.key && r.successor_key !== e.key);
      renderEventsTable();
      renderRelations();
    });
    tbody.appendChild(tr);
  }
}

/* 间隔展示文案：未设置（最短 0 / 最长不限）时不附加说明，与旧语义一致 */
function gapText(r) {
  const min = r.min_gap_minutes === null || r.min_gap_minutes === undefined ? 0 : Number(r.min_gap_minutes);
  const max = r.max_gap_minutes === null || r.max_gap_minutes === undefined || r.max_gap_minutes === ""
    ? null : Number(r.max_gap_minutes);
  const parts = [];
  if (min > 0) parts.push(`最短等待 ${min} 分钟`);
  if (max !== null) parts.push(`最长等待 ${max} 分钟`);
  return parts.length ? `（${parts.join("，")}）` : "";
}

/* 解析间隔输入：空串 → 默认值；非法 → {ok:false} */
function parseGapInput(raw, label, emptyValue) {
  const s = String(raw).trim();
  if (s === "") return { ok: true, value: emptyValue };
  if (!/^\d+$/.test(s)) return { ok: false, message: `“先于”关系的${label}必须是非负整数分钟` };
  return { ok: true, value: parseInt(s, 10) };
}

/* 带时区事件：已填写时间必须能被 Date 解析且带 UTC 偏移（末段 Z 或 ±HH:MM）；
   未带时区事件：时间不得携带偏移。偏移是否符合该时区当日规则由服务端裁定，
   这里只做形态预检，真正的夏令时不存在时刻/错误偏移由服务端整次拒绝。 */
function checkEventTimeInput(e) {
  const hasOffset = (v) => /[zZ]$|[+-]\d{2}:?\d{2}$/.test(String(v).trim());
  for (const f of ["earliest_start", "latest_start"]) {
    const v = (e[f] || "").trim();
    if (!v) continue;
    if (e.timezone) {
      if (!hasOffset(v)) {
        return `事件「${e.name}」指定了时区 ${e.timezone}，${f === "earliest_start" ? "最早" : "最晚"}` +
          `开始必须携带 UTC 偏移（例如 ${v}+08:00）`;
      }
      if (Number.isNaN(new Date(v).getTime())) {
        return `事件「${e.name}」的${f === "earliest_start" ? "最早" : "最晚"}开始不是合法的带偏移日期时间`;
      }
    } else if (hasOffset(v)) {
      return `事件「${e.name}」未指定时区，${f === "earliest_start" ? "最早" : "最晚"}开始不得携带 UTC 偏移`;
    }
  }
  return null;
}

function renderRelations() {
  const opts = state.editEvents
    .map((e) => `<option value="${e.key}">${escapeHtml(e.name)}</option>`).join("");
  $("#rel-pre").innerHTML = opts;
  $("#rel-suc").innerHTML = opts;
  const ul = $("#relations-list");
  ul.innerHTML = "";
  state.editRelations.forEach((r, idx) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `「${eventName(r.predecessor_key)}」先于「${eventName(r.successor_key)}」`;
    li.appendChild(label);

    const gaps = document.createElement("span");
    gaps.className = "rel-gaps";
    gaps.innerHTML =
      `<label>最短等待 <input type="number" min="0" step="1" data-gap="min" value="${r.min_gap_minutes}"> 分钟</label>` +
      `<label>最长等待 <input type="number" min="0" step="1" data-gap="max" placeholder="不限" value="${r.max_gap_minutes}"> 分钟</label>`;
    gaps.querySelector('[data-gap="min"]').addEventListener("input", (ev) => {
      r.min_gap_minutes = ev.target.value === "" ? "" : parseInt(ev.target.value, 10);
      if (Number.isNaN(r.min_gap_minutes)) r.min_gap_minutes = "";
    });
    gaps.querySelector('[data-gap="max"]').addEventListener("input", (ev) => {
      r.max_gap_minutes = ev.target.value === "" ? "" : parseInt(ev.target.value, 10);
      if (Number.isNaN(r.max_gap_minutes)) r.max_gap_minutes = "";
    });
    li.appendChild(gaps);

    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.addEventListener("click", () => {
      state.editRelations.splice(idx, 1);
      renderRelations();
    });
    li.appendChild(btn);
    ul.appendChild(li);
  });
}

function chainHtml(chain) {
  return `<ol class="chain">` + chain.map((s) => `<li>${escapeHtml(s.text)}</li>`).join("") + `</ol>`;
}

function fmtTime(s) { return s ? escapeHtml(String(s).replace("T", " ")) : "—（暂存）"; }

function buildResultHtml(res, evByKey, analysisVersion) {
  // 生成推算结果各分区 HTML。evByKey: {窗口中使用的事件 id/key -> 事件输入}
  // analysisVersion 为该结果对应的可回看版本号时，冲突分区提供“最小冲突约束
  // 组分析”入口（当前版本与历史回看均可进入；并入预览不传，不提供入口）。
  const nConf = res.conflicts.length;
  const nDraft = res.drafts.length;
  let summary;
  if (nConf > 0) {
    summary = `<div class="summary err">推算发现 ${nConf} 处约束冲突 —— 原始输入已保留，该版本不得定稿。</div>`;
  } else if (nDraft > 0) {
    summary = `<div class="summary warn">推算通过，但有 ${nDraft} 个事件处于暂存状态，补齐时间范围前不得定稿。</div>`;
  } else {
    summary = `<div class="summary ok">推算通过：所有事件均存在可行时间窗。</div>`;
  }

  let conflicts = "";
  if (nConf > 0) {
    let entry = "";
    if (analysisVersion) {
      entry = `<div class="diagnose-entry"><button class="btn-diagnose" data-version="${analysisVersion}">` +
        `🔎 分析该版本的最小冲突约束组（只读）</button>` +
        `<span class="hint">按该版本不可变快照定位“去掉任一条即可解”的最小冲突输入</span></div>`;
    }
    conflicts = `<div class="section-title">冲突明细（输入已保留）</div>` + entry + res.conflicts.map((c) => {
      let html = `<div class="card conflict"><div class="msg">⚠ ${escapeHtml(c.message)}</div>`;
      if (c.earliest_chain) {
        html += `<details open><summary>最早可行开始的依据链</summary>${chainHtml(c.earliest_chain)}</details>`;
        html += `<details open><summary>最晚可行开始的依据链</summary>${chainHtml(c.latest_chain)}</details>`;
      }
      return html + `</div>`;
    }).join("");
  }

  let drafts = "";
  if (nDraft > 0) {
    drafts = `<div class="section-title">暂存事件（不参与推算）</div>` + res.drafts.map((d) => {
      const src = evByKey[d.id] || evByKey[String(d.id)];
      const originLine = src && src.origin ? originBadgeHtml(src.origin) : "";
      return `<div class="card draft"><h3>「${escapeHtml(d.name)}」</h3>` +
        (originLine ? `<div class="origin-line">${originLine}</div>` : "") +
        `<div class="reason">${escapeHtml(d.reason)}</div></div>`;
    }).join("");
  }

  let skipped = "";
  if (res.skipped_relations.length > 0) {
    skipped = `<div class="section-title">暂不参与推算的关系</div>` + res.skipped_relations.map((s) =>
      `<div class="card draft"><div class="reason">${escapeHtml(s.reason)}</div></div>`).join("");
  }

  let windows = "";
  const winIds = Object.keys(res.windows);
  if (winIds.length > 0) {
    windows = `<div class="section-title">各事件可行时间窗（推算结果，与原始输入分开保存）</div>` +
      winIds.map((id) => {
        const w = res.windows[id];
        const ev = evByKey[id];
        const name = ev ? ev.name : id;
        const dur = ev ? ev.duration_minutes : "";
        const tz = w.timezone || (ev ? ev.timezone : "") || "";
        const tzTag = tz ? ` <span class="tz-tag">${escapeHtml(tz)}</span>` : "";
        const originLine = ev && ev.origin
          ? `<div class="origin-line">${originBadgeHtml(ev.origin)}</div>` : "";
        return `<div class="card">` +
          `<h3>「${escapeHtml(name)}」${tzTag} <span class="window">${w.earliest_start.replace("T", " ")} ~ ${w.latest_start.replace("T", " ")}</span></h3>` +
          originLine +
          `<div class="hint">可行结束窗口：${w.earliest_end.replace("T", " ")} ~ ${w.latest_end.replace("T", " ")}` +
          (dur === null || dur === undefined || dur === "" ? "" : `（持续 ${dur} 分钟）`) + `</div>` +
          `<details><summary>最早可行开始的依据链</summary>${chainHtml(w.earliest_chain)}</details>` +
          `<details><summary>最晚可行开始的依据链</summary>${chainHtml(w.latest_chain)}</details>` +
          `</div>`;
      }).join("");
  }

  return { summary, conflicts, drafts, skipped, windows };
}

function renderResults() {
  const comp = state.current.computation;
  const slots = [
    ["#result-summary", "summary"], ["#conflicts", "conflicts"],
    ["#drafts", "drafts"], ["#skipped", "skipped"], ["#windows", "windows"],
  ];
  if (!comp) {
    for (const [sel] of slots) $(sel).innerHTML = "";
    $("#result-summary").innerHTML = `<div class="summary warn">尚无推算结果，请保存后查看。</div>`;
    return;
  }
  const byKey = {};
  for (const e of state.current.events) byKey[String(e.id)] = e;
  const parts = buildResultHtml(comp.result, byKey, state.current.review.version);
  for (const [sel, key] of slots) $(sel).innerHTML = parts[key];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- 保存 */

function syncFromDOM() {
  document.querySelectorAll("#events-table tbody tr").forEach((tr) => {
    const e = state.editEvents.find((x) => x.key === tr.dataset.key);
    if (!e) return;
    e.name = tr.querySelector('[data-f="name"]').value.trim();
    e.timezone = tr.querySelector('[data-f="timezone"]').value.trim();
    e.earliest_start = tr.querySelector('[data-f="earliest_start"]').value;
    e.latest_start = tr.querySelector('[data-f="latest_start"]').value;
    const d = tr.querySelector('[data-f="duration_minutes"]').value;
    e.duration_minutes = d === "" ? "" : parseInt(d, 10);
    e.note = tr.querySelector('[data-f="note"]').value;
  });
}

async function save() {
  if (!state.current) return;
  syncFromDOM();
  for (const e of state.editEvents) {
    const problem = checkEventTimeInput(e);
    if (problem) {
      showBanner("⚠ " + escapeHtml(problem));
      return;
    }
  }
  const relations = [];
  for (const r of state.editRelations) {
    const min = parseGapInput(r.min_gap_minutes, "最短等待", 0);
    const max = parseGapInput(r.max_gap_minutes, "最长等待", null);
    if (!min.ok || !max.ok) {
      showBanner("⚠ " + escapeHtml(min.message || max.message));
      return;
    }
    if (max.value !== null && max.value < min.value) {
      showBanner(`⚠ 关系「${escapeHtml(eventName(r.predecessor_key))}」先于` +
        `「${escapeHtml(eventName(r.successor_key))}」的最长等待不得短于最短等待`);
      return;
    }
    relations.push({
      predecessor_key: r.predecessor_key,
      successor_key: r.successor_key,
      min_gap_minutes: min.value,
      max_gap_minutes: max.value,
    });
  }
  const payload = {
    base_version: state.current.review.version,
    title: $("#review-title").value.trim(),
    events: state.editEvents.map((e) => ({
      key: e.key,
      uid: e.uid || "",
      name: e.name,
      timezone: e.timezone || null,
      earliest_start: e.earliest_start || null,
      latest_start: e.latest_start || null,
      duration_minutes: e.duration_minutes === "" ? null : e.duration_minutes,
      note: e.note,
    })),
    relations,
  };
  try {
    const bundle = await api("PUT", "/api/reviews/" + state.current.review.id, payload);
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    loadVersions();
    loadReviews(bundle.review.id).then(() => loadMergeReviews());
  } catch (err) {
    handleSaveError(err);
  }
}

function handleSaveError(err) {
  if (err.status === 409) {
    closeMergeModal();
    showBanner(
      `⚠ ${escapeHtml(err.data.error)}（服务器当前版本：v${err.data.current_version}，` +
      `你基于 v${state.current.review.version} 编辑）` +
      `<button id="btn-reload-latest">加载最新版本（放弃未保存修改）</button>`);
    $("#btn-reload-latest").addEventListener("click", () => {
      if (confirm("确定放弃当前未保存的修改并加载最新版本吗？")) {
        openReview(state.current.review.id);
      }
    });
  } else {
    showBanner("⚠ " + escapeHtml(err.message || "保存失败"));
  }
}

async function finalize() {
  if (!state.current) return;
  try {
    const bundle = await api("POST", "/api/reviews/" + state.current.review.id + "/finalize",
      { base_version: state.current.review.version });
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    loadVersions();
    loadReviews(bundle.review.id).then(() => loadMergeReviews());
  } catch (err) {
    handleSaveError(err);
  }
}

/* ------------------------------------------------------------ 版本回看/恢复 */

async function loadVersions() {
  if (!state.current) return;
  const data = await api("GET", "/api/reviews/" + state.current.review.id + "/versions");
  const tbody = $("#versions-table tbody");
  tbody.innerHTML = "";
  for (const v of data.versions) {
    const tr = document.createElement("tr");
    const cur = v.version === data.current_version;
    let html = `<td>v${v.version}${cur ? ' <span class="kind-tag">当前</span>' : ""}</td>`;
    html += `<td>${kindTag(v)}</td>`;
    html += `<td>${statusText(v)}</td>`;
    html += `<td>${v.attachment_count ? `<span class="att-count" title="该版本留存 ${v.attachment_count} 个现场附件">📎 ${v.attachment_count}</span>` : '<span class="hint">—</span>'}</td>`;
    html += `<td class="hint">${(v.computed_at || "").replace("T", " ")}</td>`;
    html += `<td class="version-actions"></td>`;
    tr.innerHTML = html;
    const td = tr.querySelector(".version-actions");
    if (v.reviewable) {
      const btn = document.createElement("button");
      btn.textContent = "回看";
      btn.addEventListener("click", () => openVersion(v.version));
      td.appendChild(btn);
    } else {
      const span = document.createElement("span");
      span.className = "legacy-note";
      span.textContent = "不可回看、不可恢复（" + (v.reason || "缺少输入快照") + "）";
      td.appendChild(span);
    }
    tbody.appendChild(tr);
  }
  fillCompareSelects(data.versions, data.current_version);
}

/* 对照下拉框：只有具备输入快照的版本可参与对照；旧库缺快照版本禁选并标注 */
function fillCompareSelects(versions, currentVersion) {
  const fromSel = $("#compare-from"), toSel = $("#compare-to");
  const asc = [...versions].sort((a, b) => a.version - b.version);
  const opts = asc.map((v) => {
    const label = `v${v.version}` + (v.reviewable ? "" : "（缺快照不可对照）");
    return `<option value="${v.version}"${v.reviewable ? "" : " disabled"}>${label}</option>`;
  }).join("");
  fromSel.innerHTML = opts;
  toSel.innerHTML = opts;
  const reviewable = asc.filter((v) => v.reviewable).map((v) => v.version);
  const toDefault = reviewable.includes(currentVersion)
    ? currentVersion : reviewable[reviewable.length - 1];
  const earlier = reviewable.filter((v) => v < toDefault);
  fromSel.value = earlier.length ? earlier[earlier.length - 1] : toDefault;
  toSel.value = toDefault;
  $("#btn-compare").disabled = reviewable.length < 2;
}

function kindTag(v) {
  if (v.kind === "finalize") return `<span class="kind-tag finalize">已定稿</span>`;
  if (v.kind === "merge") return `<span class="kind-tag restore">并入产生</span>`;
  if (v.kind === "restore") return `<span class="kind-tag restore">恢复产生</span>`;
  if (v.kind === "attachment") return `<span class="kind-tag restore">附件变更</span>`;
  if (v.kind === "save") return `<span class="kind-tag">保存</span>`;
  return `<span class="kind-tag">历史记录</span>`;
}

function statusText(v) {
  if (v.state === "finalized") return `<span class="badge ok">已定稿</span>`;
  if (v.status === "conflict") return `<span class="badge err">冲突·不得定稿</span>`;
  if (v.has_drafts) return `<span class="badge warn">草稿·有暂存</span>`;
  return `<span class="badge">草稿</span>`;
}

async function openVersion(version) {
  const rid = state.current.review.id;
  let detail;
  try {
    detail = await api("GET", `/api/reviews/${rid}/versions/${version}`);
  } catch (err) {
    showBanner("⚠ " + escapeHtml(err.message || "无法回看该版本"));
    return;
  }
  $("#vm-title").textContent = `v${detail.version} ${detail.title}`;
  $("#vm-badges").innerHTML = kindTag(detail) + " " + statusText(detail);
  let meta = `推算状态：${detail.status === "ok" ? "通过" : "冲突"}`;
  if (detail.kind === "restore" && detail.source_version) {
    meta += `　|　由 v${detail.source_version} 恢复而来`;
  }
  if (detail.kind === "merge" && detail.source_review_id) {
    meta += `　|　由复盘 #${detail.source_review_id} v${detail.source_version} 并入而来`;
  }
  meta += `　|　推算时间：${(detail.computed_at || "").replace("T", " ")}`;
  $("#vm-meta").textContent = meta;
  $("#vm-body").innerHTML = renderVersionDetail(detail);
  const btn = $("#btn-vm-restore");
  btn.disabled = false;
  btn.onclick = () => restoreVersion(detail);
  $("#version-modal").classList.remove("hidden");
}

function closeVersionModal() {
  $("#version-modal").classList.add("hidden");
}

function renderVersionDetail(detail) {
  const events = detail.input.events || [];
  const byKey = {};
  for (const e of events) byKey[e.key] = e;
  const nameByUid = {};
  for (const e of events) nameByUid[e.uid] = e.name;

  const rows = events.map((e) =>
    `<tr><td>${escapeHtml(e.name)}${originBadgeHtml(e.origin)}</td>` +
    `<td>${escapeHtml(e.timezone || "")}</td>` +
    `<td>${fmtTime(e.earliest_start)}</td>` +
    `<td>${fmtTime(e.latest_start)}</td>` +
    `<td>${e.duration_minutes === null || e.duration_minutes === undefined ? "—" : e.duration_minutes}</td>` +
    `<td>${escapeHtml(e.note || "")}</td></tr>`).join("");

  const rels = (detail.input.relations || []).map((r) => {
    const a = byKey[r.predecessor_key], b = byKey[r.successor_key];
    const an = a ? a.name : "?";
    const bn = b ? b.name : "?";
    return `<div>「${escapeHtml(an)}」先于「${escapeHtml(bn)}」${escapeHtml(gapText(r))}</div>`;
  }).join("") || `<div class="hint">无</div>`;

  // 该版本留存的现场附件（不可变快照）：审阅者看到并下载当时的原件
  const atts = detail.input.attachments || [];
  let attsHtml;
  if (!atts.length) {
    attsHtml = `<div class="hint">无附件</div>`;
  } else {
    attsHtml = `<div class="vm-att-list">` + atts.map((a) =>
      `<div class="att-item">` +
        `<div>事件：「${escapeHtml(nameByUid[a.event_uid] || "（已删除事件）")}」</div>` +
        `<a class="att-name" href="${a.download_url}" download="${escapeHtml(a.filename)}">${fileIcon(a)} ${escapeHtml(a.filename)}</a>` +
        `<div class="att-meta hint" title="SHA-256：${a.blob_sha256}">${fmtSize(a.size)} · <code>${shortSha(a.blob_sha256)}</code>` +
        (a.uploaded_at ? ` · ${escapeHtml(String(a.uploaded_at).replace("T", " "))}` : "") + `</div>` +
        (a.description ? `<div class="att-desc">${escapeHtml(a.description)}</div>` : "") +
      `</div>`).join("") + `</div>`;
  }

  // 快照事件以 id_<旧行id> 为 key，推算结果窗口仍以旧行 id 为键
  const winIndex = {};
  for (const e of events) winIndex[String(e.key).replace(/^id_/, "")] = e;
  const parts = buildResultHtml(detail.result, winIndex, detail.version);
  return `<div class="section-title">当时输入（不可变快照）</div>` +
    `<table><thead><tr><th>事件名称</th><th>时区</th><th>最早开始</th><th>最晚开始</th>` +
    `<th>持续(分钟)</th><th>备注</th></tr></thead><tbody>${rows}</tbody></table>` +
    `<div class="section-title">“先于”关系</div><div class="vm-relations">${rels}</div>` +
    `<div class="section-title">现场附件（${atts.length} 个，随该版本留存，可下载原件）</div>` +
    `<div class="vm-attachments">${attsHtml}</div>` +
    `<div class="section-title">当时推算依据与结果</div>` +
    parts.summary + parts.conflicts + parts.drafts + parts.skipped + parts.windows;
}

async function restoreVersion(detail) {
  if (!state.current) return;
  if (!confirm(`确定把 v${detail.version} 的输入恢复为当前复盘的新草稿版本吗？\n` +
               `将重新推算，且不会覆盖任何旧版本（需基于当前 v${state.current.review.version}）。`)) {
    return;
  }
  try {
    const bundle = await api(
      "POST",
      `/api/reviews/${state.current.review.id}/versions/${detail.version}/restore`,
      { base_version: state.current.review.version });
    closeVersionModal();
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    await loadVersions();
    await loadReviews(bundle.review.id);
    await loadMergeReviews();
  } catch (err) {
    handleSaveError(err);
  }
}

/* ------------------------------------------------------------ 版本对照 */

async function openCompare() {
  if (!state.current) return;
  const from = parseInt($("#compare-from").value, 10);
  const to = parseInt($("#compare-to").value, 10);
  try {
    const data = await api(
      "GET", `/api/reviews/${state.current.review.id}/compare?from=${from}&to=${to}`);
    hideBanner();
    renderCompareModal(data);
  } catch (err) {
    // 顺序无效(400)、版本不存在(404)、旧库缺快照(422)等明确报错
    showBanner("⚠ 版本对照失败：" + escapeHtml(err.message || "未知错误"));
  }
}

function closeCompareModal() { $("#compare-modal").classList.add("hidden"); }

function fmtVal(v) { return v === null || v === undefined || v === "" ? "—" : escapeHtml(String(v)); }
function fmtWin(w) {
  if (!w) return "—（暂存，无推算窗口）";
  return `${w.earliest_start.replace("T", " ")} ~ ${w.latest_start.replace("T", " ")}`;
}
function fmtGap(g) {
  const parts = [];
  if (g.min_gap_minutes) parts.push(`最短等待 ${g.min_gap_minutes} 分钟`);
  if (g.max_gap_minutes !== null && g.max_gap_minutes !== undefined) {
    parts.push(`最长等待 ${g.max_gap_minutes} 分钟`);
  }
  return parts.length ? `（${parts.join("，")}）` : "（未设间隔）";
}

const INPUT_FIELD_LABELS = {
  earliest_start: "最早开始", latest_start: "最晚开始",
  duration_minutes: "持续分钟数", timezone: "IANA 时区", note: "备注",
};
const WINDOW_FIELD_LABELS = {
  earliest_start: "最早可行开始", latest_start: "最晚可行开始",
  earliest_end: "最早可行结束", latest_end: "最晚可行结束",
};

function changeRows(changes, labels, isTime) {
  return Object.keys(changes).map((f) => {
    const c = changes[f];
    const fmt = isTime ? (v) => fmtVal(v).replace("T", " ") : fmtVal;
    return `<tr><td>${labels[f] || f}</td><td>${fmt(c.from)}</td><td>${fmt(c.to)}</td></tr>`;
  }).join("");
}

function renderCompareModal(data) {
  $("#cm-title").textContent = `版本对照：v${data.from.version} → v${data.to.version}`;
  const meta = [
    `标题：「${data.from.title}」→「${data.to.title}」${data.title_changed ? "（已变更）" : "（未变）"}`,
    `推算状态：${data.from.status} → ${data.to.status}`,
    `定稿状态：${data.from.state === "finalized" ? "已定稿" : "草稿"} → ` +
      `${data.to.state === "finalized" ? "已定稿" : "草稿"}`,
  ];
  $("#cm-meta").textContent = meta.join("　|　");
  $("#cm-body").innerHTML = data.pairable ? compareDiffHtml(data) : compareRawHtml(data);
  $("#compare-modal").classList.remove("hidden");
}

/* 缺可靠身份的旧快照：只展示各版本原貌，不展示逐事件差值 */
function compareRawHtml(data) {
  const side = (label, v) => {
    const detail = { input: v.input, result: v.result };
    return `<div class="cmp-col"><div class="section-title">${label}（v${v.version}）原貌</div>` +
      renderVersionDetail(detail) + `</div>`;
  };
  return `<div class="summary warn">⚠ ${escapeHtml(data.reason)}</div>` +
    `<div class="cmp-grid">${side("较早版本", data.from)}${side("较晚版本", data.to)}</div>`;
}

function eventBriefHtml(e, tag) {
  return `<div class="card ${tag}"><h3>「${escapeHtml(e.name)}」` +
    (e.timezone ? ` <span class="tz-tag">${escapeHtml(e.timezone)}</span>` : "") +
    (e.draft ? ` <span class="badge warn">暂存</span>` : "") + `</h3>` +
    (e.origin ? `<div class="origin-line">${originBadgeHtml(e.origin)}</div>` : "") +
    `<div class="hint">输入范围：${fmtVal(e.earliest_start).replace("T", " ")} ~ ` +
    `${fmtVal(e.latest_start).replace("T", " ")}` +
    (e.duration_minutes === null || e.duration_minutes === undefined
      ? "" : `（持续 ${e.duration_minutes} 分钟）`) + `</div>` +
    `<div class="hint">推算窗口：${fmtWin(e.window)}</div></div>`;
}

function compareDiffHtml(data) {
  const s = data.summary;
  const ev = data.events;
  let html = `<div class="summary ok">事件：新增 ${s.events_added}、删除 ${s.events_removed}、` +
    `改名 ${s.events_renamed}、变化 ${s.events_changed}、未变 ${s.events_unchanged}　|　` +
    `关系：新增 ${s.relations_added}、删除 ${s.relations_removed}、间隔变化 ${s.relations_changed}　|　` +
    `冲突 ${s.conflicts_from} → ${s.conflicts_to}　|　暂存 ${s.drafts_from} → ${s.drafts_to}</div>`;

  if (ev.added.length) {
    html += `<div class="section-title">新增事件</div>` +
      ev.added.map((e) => eventBriefHtml(e, "diff-add")).join("");
  }
  if (ev.removed.length) {
    html += `<div class="section-title">删除事件</div>` +
      ev.removed.map((e) => eventBriefHtml(e, "diff-del")).join("");
  }
  if (ev.renamed.length) {
    html += `<div class="section-title">改名事件（同一事件，按持续身份配对）</div>` +
      ev.renamed.map((r) =>
        `<div class="card diff-rename">「${escapeHtml(r.from)}」→「${escapeHtml(r.to)}」` +
        (r.origin ? `<div class="origin-line">${originBadgeHtml(r.origin)}</div>` : "") +
        `</div>`).join("");
  }
  if (ev.changed.length) {
    html += `<div class="section-title">输入与推算窗口变化</div>` + ev.changed.map((c) => {
      let card = `<div class="card"><h3>「${escapeHtml(c.name.from)}」` +
        (c.name.from !== c.name.to ? ` → 「${escapeHtml(c.name.to)}」` : "") +
        (c.draft.from !== c.draft.to
          ? ` <span class="badge warn">${c.draft.to ? "变为暂存" : "已补齐"}</span>` : "") +
        `</h3>` +
        (c.origin ? `<div class="origin-line">${originBadgeHtml(c.origin)}</div>` : "");
      const inputRows = changeRows(c.input_changes, INPUT_FIELD_LABELS, true);
      if (inputRows) {
        card += `<table class="cmp"><thead><tr><th>输入</th><th>v${data.from.version}</th>` +
          `<th>v${data.to.version}</th></tr></thead><tbody>${inputRows}</tbody></table>`;
      }
      const winRows = changeRows(c.window_changes, WINDOW_FIELD_LABELS, true);
      if (winRows) {
        card += `<table class="cmp"><thead><tr><th>推算窗口</th><th>v${data.from.version}</th>` +
          `<th>v${data.to.version}</th></tr></thead><tbody>${winRows}</tbody></table>`;
      } else if (c.window.from === null || c.window.to === null) {
        card += `<div class="hint">推算窗口：${fmtWin(c.window.from)} → ${fmtWin(c.window.to)}</div>`;
      }
      return card + `</div>`;
    }).join("");
  }
  if (ev.unchanged.length) {
    const originHits = ev.unchanged.filter((e) => e.origin);
    html += `<details><summary>未变化事件（${ev.unchanged.length}）</summary><div class="hint">` +
      ev.unchanged.map((e) => `「${escapeHtml(e.name)}」`).join("、") + `</div>` +
      (originHits.length
        ? `<div class="section-title">其中并入事件的来源</div>` +
          originHits.map((e) =>
            `<div class="card"><h3>「${escapeHtml(e.name)}」</h3>` +
            `<div class="origin-line">${originBadgeHtml(e.origin)}</div></div>`).join("")
        : "") + `</details>`;
  }

  const rel = data.relations;
  if (rel.added.length || rel.removed.length || rel.changed.length) {
    html += `<div class="section-title">“先于”关系变化</div>`;
    const relName = (r) =>
      `「${escapeHtml(r.predecessor.name || "?")}」先于「${escapeHtml(r.successor.name || "?")}」`;
    html += rel.added.map((r) =>
      `<div class="card diff-add">＋ ${relName(r)}${escapeHtml(fmtGap(r.to))}</div>`).join("");
    html += rel.removed.map((r) =>
      `<div class="card diff-del">－ ${relName(r)}${escapeHtml(fmtGap(r.from))}</div>`).join("");
    html += rel.changed.map((r) =>
      `<div class="card diff-rename">△ ${relName(r)}：` +
      `间隔 ${escapeHtml(fmtGap(r.from))} → ${escapeHtml(fmtGap(r.to))}</div>`).join("");
  }

  const d = data.drafts;
  if (d.resolved.length || d.became.length || d.remaining.length) {
    html += `<div class="section-title">暂存状态变化（${d.from_count} → ${d.to_count}）</div>`;
    const names = (list) => list.map((e) => `「${escapeHtml(e.name)}」`).join("、");
    if (d.resolved.length) html += `<div class="card diff-add">已补齐：${names(d.resolved)}</div>`;
    if (d.became.length) html += `<div class="card diff-del">转为暂存：${names(d.became)}</div>`;
    if (d.remaining.length) html += `<div class="card">仍为暂存：${names(d.remaining)}</div>`;
  }

  html += `<div class="section-title">冲突变化（${data.conflicts.from.length} → ` +
    `${data.conflicts.to.length}）</div>`;
  const conflictList = (list, label) =>
    `<div class="cmp-col"><div class="hint">${label}</div>` +
    (list.length
      ? list.map((c) => `<div class="card conflict"><div class="msg">⚠ [${escapeHtml(c.type)}] ` +
          `${escapeHtml(c.message)}</div>` +
          (c.events.length
            ? `<div class="hint">涉及：${c.events.map((e) => `「${escapeHtml(e.name)}」`).join("、")}</div>`
            : "") + `</div>`).join("")
      : `<div class="hint">无冲突</div>`) + `</div>`;
  html += `<div class="cmp-grid">` +
    conflictList(data.conflicts.from, `v${data.from.version}`) +
    conflictList(data.conflicts.to, `v${data.to.version}`) + `</div>`;
  return html;
}

/* ------------------------------------------------------------ 并入其他复盘 */

/* 来源复盘下拉：除当前复盘外的全部复盘；切换复盘时重新加载其可回看版本。 */
async function loadMergeReviews(preselectId) {
  const sel = $("#merge-review");
  sel.innerHTML = "";
  const curId = state.current ? state.current.review.id : null;
  const others = state.reviews.filter((r) => r.id !== curId);
  if (!others.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（暂无其他复盘）";
    sel.appendChild(opt);
    sel.disabled = true;
    $("#btn-merge-load").disabled = true;
    $("#merge-version").innerHTML = "";
  } else {
    sel.disabled = false;
    $("#btn-merge-load").disabled = false;
    for (const r of others) {
      const opt = document.createElement("option");
      opt.value = r.id;
      opt.textContent = `#${r.id} ${r.title}（v${r.version}）`;
      sel.appendChild(opt);
    }
    if (preselectId) sel.value = String(preselectId);
    await loadMergeVersions();
  }
}

async function loadMergeVersions() {
  const vsel = $("#merge-version");
  vsel.innerHTML = "";
  const sid = parseInt($("#merge-review").value, 10);
  if (!sid) return;
  const data = await api("GET", `/api/reviews/${sid}/versions`);
  for (const v of data.versions) {
    const opt = document.createElement("option");
    opt.value = v.version;
    const isCurrent = v.version === data.current_version;
    opt.textContent = `v${v.version}` +
      (isCurrent ? "（当前）" : "") +
      (v.reviewable ? "" : "（缺快照不可并入）");
    if (!v.reviewable) opt.disabled = true;
    vsel.appendChild(opt);
  }
  // 默认选中来源复盘的当前版本（避免直接读旧快照、误并入过时事件）；
  // 当前版本缺少可回看快照时，回退到最新的可回看版本（列表按版本号倒序）。
  const reviewableVersions = data.versions.filter((v) => v.reviewable)
    .map((v) => v.version);
  const fallback = reviewableVersions[0];
  const defaultVersion = reviewableVersions.includes(data.current_version)
    ? data.current_version : fallback;
  if (defaultVersion) vsel.value = String(defaultVersion);
  // 没有任何可并入版本时，加载按钮禁用（缺快照版本始终不可选）
  $("#btn-merge-load").disabled = !defaultVersion;
}

/* 加载来源版本的不可变输入，渲染事件勾选表；关系仅用于提示“两端均选”会带哪些。 */
async function loadMergeSource() {
  if (!state.current) return;
  const sid = parseInt($("#merge-review").value, 10);
  const sver = parseInt($("#merge-version").value, 10);
  if (!sid || !sver) return;
  let detail;
  try {
    detail = await api("GET", `/api/reviews/${sid}/versions/${sver}`);
  } catch (err) {
    showBanner("⚠ 来源版本无法加载：" + escapeHtml(err.message || "未知错误"));
    return;
  }
  state.merge = {
    sourceId: sid, sourceVersion: sver, detail,
    selected: new Set((detail.input.events || []).map((e) => e.uid).filter(Boolean)),
  };
  renderMergePicker();
}

function mergeSelectedCount() {
  return state.merge ? state.merge.selected.size : 0;
}

function renderMergePicker() {
  const box = $("#merge-event-picker");
  if (!state.merge) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const { detail, selected } = state.merge;
  const events = detail.input.events || [];
  const uidByKey = {};
  for (const e of events) uidByKey[e.key] = e.uid;
  const nameByUid = {};
  for (const e of events) nameByUid[e.uid] = e.name;
  const attsByUid = {};
  for (const a of (detail.input.attachments || [])) {
    (attsByUid[a.event_uid] = attsByUid[a.event_uid] || []).push(a);
  }

  const rows = events.map((e) => {
    const checked = selected.has(e.uid) ? " checked" : "";
    const draft = (!e.earliest_start || !e.latest_start || e.duration_minutes === null
      || e.duration_minutes === undefined);
    const nAtt = (attsByUid[e.uid] || []).length;
    return `<tr data-uid="${escapeHtml(e.uid || "")}">` +
      `<td><input type="checkbox" class="mp-check"${checked}${e.uid ? "" : " disabled"}></td>` +
      `<td>${escapeHtml(e.name)}${nAtt ? ` <span class="att-count" title="该事件在来源版本留存 ${nAtt} 个附件，入选后将复制附件关联">📎 ${nAtt}</span>` : ""}</td>` +
      `<td>${escapeHtml(e.timezone || "")}</td>` +
      `<td>${fmtTime(e.earliest_start)}</td>` +
      `<td>${fmtTime(e.latest_start)}</td>` +
      `<td>${e.duration_minutes === null || e.duration_minutes === undefined ? "—" : e.duration_minutes}</td>` +
      `<td>${draft ? '<span class="badge warn">暂存</span>' : ""}</td>` +
      `</tr>`;
  }).join("");

  const relLines = (detail.input.relations || []).map((r) => {
    const ua = uidByKey[r.predecessor_key], ub = uidByKey[r.successor_key];
    const both = selected.has(ua) && selected.has(ub);
    return `<div class="${both ? "mp-rel-on" : "mp-rel-off"}">` +
      (both ? "✔ 将复制：" : "· ") +
      `「${escapeHtml(nameByUid[ua] || "?")}」先于「${escapeHtml(nameByUid[ub] || "?")}」` +
      `${escapeHtml(gapText(r))}${both ? "" : "（至少一端未入选，不会复制）"}</div>`;
  }).join("");

  box.innerHTML =
    `<div class="mp-actions">` +
      `<button id="btn-mp-all">全选</button><button id="btn-mp-none">全不选</button>` +
      `<span class="hint">已选 <strong id="mp-count">${selected.size}</strong> / ${events.length} 个事件</span>` +
      `<button id="btn-merge-preview" class="primary">预览并入</button>` +
    `</div>` +
    `<table class="merge-events"><thead><tr><th></th><th>事件名称</th><th>时区</th>` +
      `<th>最早开始</th><th>最晚开始</th><th>持续(分钟)</th><th></th></tr></thead>` +
      `<tbody>${rows}</tbody></table>` +
    `<div class="section-title">来源中的“先于”关系（仅两端均入选才复制，保留最短/最长等待）</div>` +
    `<div class="mp-rel">${relLines || '<span class="hint">无</span>'}</div>`;

  box.querySelectorAll("tr[data-uid]").forEach((tr) => {
    const cb = tr.querySelector(".mp-check");
    if (!cb) return;
    cb.addEventListener("change", () => {
      const uid = tr.dataset.uid;
      if (cb.checked) selected.add(uid); else selected.delete(uid);
      renderMergePicker();
    });
  });
  $("#btn-mp-all").addEventListener("click", () => {
    for (const e of events) if (e.uid) selected.add(e.uid);
    renderMergePicker();
  });
  $("#btn-mp-none").addEventListener("click", () => {
    selected.clear();
    renderMergePicker();
  });
  $("#btn-merge-preview").addEventListener("click", requestMergePreview);
}

async function requestMergePreview() {
  if (!state.current || !state.merge) return;
  const uids = [...state.merge.selected];
  if (!uids.length) {
    showBanner("⚠ 请至少选择一个要并入的来源事件");
    return;
  }
  const payload = {
    source_review_id: state.merge.sourceId,
    source_version: state.merge.sourceVersion,
    event_uids: uids,
  };
  try {
    const data = await api(
      "POST", `/api/reviews/${state.current.review.id}/merge/preview`, payload);
    hideBanner();
    state.mergePreview = data;
    renderMergeModal(data);
  } catch (err) {
    showBanner("⚠ 并入预览失败：" + escapeHtml(err.message || "未知错误"));
  }
}

/* 预览结果中推算窗口的键：t_<目标行id> / s_<来源uid>，映射回展示事件。 */
function mergeResultEvByKey(data) {
  const map = {};
  for (const e of (state.current.events || [])) map["t_" + e.id] = e;
  for (const e of data.copied_events) {
    map["s_" + e.uid] = {
      name: e.name, timezone: e.timezone,
      duration_minutes: e.duration_minutes,
      origin: e.origin || null,
    };
  }
  return map;
}

function renderMergeModal(data) {
  $("#mm-title").textContent =
    `并入预览：复盘 #${data.source.review_id}「${data.source.title}」v${data.source.version}` +
    ` → 「${data.target.title}」v${data.target.base_version}`;
  $("#mm-meta").textContent =
    `预览只读，不改任何版本；确认时将基于目标 v${data.target.base_version} 重新核对，` +
    `目标未变化则结论与本预览一致，并一次性另存为目标的新草稿版本。` +
    `来源历史与目标原有事件均不改变；复制事件在目标取得全新持续身份。`;

  const evRows = data.copied_events.map((e) =>
    `<tr><td>${escapeHtml(e.name)}${(e.attachments || []).length
        ? ` <span class="att-count" title="将复制 ${e.attachments.length} 个附件关联（文件共享，来源删除后仍可下载）">📎 ${e.attachments.length}</span>`
        : ""}</td><td>${escapeHtml(e.timezone || "")}</td>` +
    `<td>${fmtTime(e.earliest_start)}</td><td>${fmtTime(e.latest_start)}</td>` +
    `<td>${e.duration_minutes === null || e.duration_minutes === undefined ? "—" : e.duration_minutes}</td>` +
    `<td>${escapeHtml(e.note || "")}</td></tr>`).join("");
  const rels = data.copied_relations.map((r) =>
    `<div>「${escapeHtml(r.predecessor.name || "?")}」先于「${escapeHtml(r.successor.name || "?")}」` +
    `${escapeHtml(gapText(r))}</div>`).join("") || `<div class="hint">无（没有两端均入选的关系）</div>`;

  // 将随入选事件复制的附件关联（只复制关联、文件按内容共享，来源删除后目标仍可下载）
  const nameByUid = {};
  for (const e of data.copied_events) nameByUid[e.uid] = e.name;
  const cpAtts = data.copied_attachments || [];
  let attsSection;
  if (!cpAtts.length) {
    attsSection = "";
  } else {
    attsSection = `<div class="section-title">将复制的现场附件关联（${cpAtts.length} 个，` +
      `只复制关联到新事件身份；文件共享，来源删除后目标仍可下载）</div><div class="mp-rel">` +
      cpAtts.map((a) => `<div>📎 「${escapeHtml(nameByUid[a.event_uid] || "?")}」· ${escapeHtml(a.filename)}` +
        ` <span class="hint">${fmtSize(a.size)} · <code>${shortSha(a.blob_sha256)}</code></span>` +
        (a.description ? ` <span class="hint">${escapeHtml(a.description)}</span>` : "") + `</div>`).join("") +
      `</div>`;
  }

  const byKey = mergeResultEvByKey(data);
  const parts = buildResultHtml(data.result, byKey);

  const body =
    `<div class="section-title">将复制的事件（${data.copied_events.length} 个，保留时间范围/持续/备注/时区与 UTC 偏移）</div>` +
    `<table class="merge-events"><thead><tr><th>事件名称</th><th>时区</th><th>最早开始</th>` +
    `<th>最晚开始</th><th>持续(分钟)</th><th>备注</th></tr></thead><tbody>${evRows}</tbody></table>` +
    `<div class="section-title">将复制的“先于”关系（${data.copied_relations.length} 条，保留等待上下界）</div>` +
    `<div class="mp-rel">${rels}</div>` +
    attsSection +
    `<div class="section-title">合并后的推算结论（预览）</div>` +
    parts.summary + parts.conflicts + parts.drafts + parts.skipped + parts.windows;
  $("#mm-body").innerHTML = body;
  $("#mm-note").textContent = data.result.status === "ok" && !data.result.drafts.length
    ? "合并结果无冲突、无暂存事件。"
    : "冲突或暂存结果仍可保存为草稿，但不得定稿。";
  $("#btn-mm-confirm").disabled = false;
  $("#merge-modal").classList.remove("hidden");
}

function closeMergeModal() { $("#merge-modal").classList.add("hidden"); }

async function confirmMerge() {
  if (!state.current || !state.merge || !state.mergePreview) return;
  const data = state.mergePreview;
  const uids = data.copied_events.map((e) => e.uid);
  if (!confirm(`确认把复盘 #${data.source.review_id} v${data.source.version} 的 ${uids.length} 个事件` +
               `并入「${data.target.title}」吗？\n将一次性另存为新草稿版本；来源历史不变，` +
               `目标原有事件保留，复制事件取得全新持续身份。`)) {
    return;
  }
  const payload = {
    source_review_id: data.source.review_id,
    source_version: data.source.version,
    event_uids: uids,
    base_version: state.current.review.version,
    digest: data.digest,
  };
  try {
    const bundle = await api(
      "POST", `/api/reviews/${state.current.review.id}/merge/confirm`, payload);
    closeMergeModal();
    $("#merge-event-picker").classList.add("hidden");
    state.merge = null;
    state.mergePreview = null;
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    await loadVersions();
    await loadReviews(bundle.review.id);
    await loadMergeReviews();
  } catch (err) {
    handleSaveError(err);
  }
}

/* ------------------------------------------------------------ 冲突最小约束分析 */

async function openDiagnosis(version) {
  if (!state.current) return;
  const rid = state.current.review.id;
  let data;
  try {
    data = await api("GET", `/api/reviews/${rid}/versions/${version}/diagnosis`);
  } catch (err) {
    // 版本不存在(404)、旧库缺快照(422)等：明确提示，不做任何修改
    showBanner("⚠ 冲突分析失败：" + escapeHtml(err.message || "未知错误"));
    return;
  }
  renderDiagnosisModal(data);
}

function closeDiagnosisModal() { $("#diagnosis-modal").classList.add("hidden"); }

function dgTime(c) {
  if (!c.local_time) return "";
  const tz = c.event && c.event.timezone ? `（${c.event.timezone}）` : "";
  return `<span class="dg-time">${escapeHtml(String(c.local_time).replace("T", " "))}${escapeHtml(tz)}</span>`;
}

function renderDiagnosisModal(data) {
  const a = data.analysis;
  $("#dg-title").textContent = `冲突最小约束组分析：v${data.version} ${data.title}`;
  $("#dg-meta").textContent =
    `只读分析，基于该版本不可变快照，不更改输入、版本号与定稿状态。推算状态：` +
    `${a.status === "conflict" ? "冲突" : "通过"}　|　同一版本重复查询结果一致。` +
    `　|　分析时间所依据的推算：${(data.computed_at || "").replace("T", " ")}`;

  let body = "";
  if (!a.groups.length) {
    body += `<div class="summary ok">该版本不存在冲突约束：最小冲突约束组为空。</div>`;
  } else {
    body += `<div class="summary err">共定位到 ${a.groups.length} 组不可再缩减的冲突约束。` +
      `每组整组不可满足；移去其中任意一条后，该组即可满足。</div>`;
    body += a.groups.map((g) => {
      const kindLabel = { invalid_range: "输入范围矛盾", cycle: "“先于”关系成环", window: "时间窗/多路径矛盾" }[g.kind] || g.kind;
      let html = `<div class="card dg-group">` +
        `<h3>第 ${g.index} 组 · <span class="dg-kind">${escapeHtml(kindLabel)}</span>` +
        ` <span class="hint">环上矛盾量 ${g.contradiction_minutes} 分钟</span></h3>` +
        `<div class="msg">⚠ ${escapeHtml(g.message)}</div>` +
        `<div class="hint">关联事件：${g.events.map((e) =>
          `「${escapeHtml(e.name || "?")}」` +
          (e.timezone ? ` <span class="tz-tag">${escapeHtml(e.timezone)}</span>` : "")).join("、")}</div>` +
        `<ol class="dg-constraints">` +
        g.constraints.map((c) => {
          const gapExtra = c.type === "earliest_bound" || c.type === "latest_bound"
            ? dgTime(c)
            : `<span class="hint">约束增量 ${c.delta_minutes} 分钟（前项持续 ${c.predecessor_duration_minutes}` +
              `${c.type === "min_wait" && c.gap_minutes ? ` + 最短等待 ${c.gap_minutes}` : ""}` +
              `${c.type === "max_wait" ? ` + 最长等待 ${c.gap_minutes}` : ""}）</span>`;
          return `<li><span class="dg-type">${escapeHtml(c.type_label)}</span>` +
            `<div>${escapeHtml(c.text)} ${gapExtra}</div></li>`;
        }).join("") + `</ol>` +
        `<div class="hint">极小性：整组不可满足；删除以上任意一条后，该组约束均可满足。</div>` +
        `</div>`;
      return html;
    }).join("");
  }

  if (a.drafts && a.drafts.length) {
    body += `<div class="section-title">不参与分析的暂存事件（${a.drafts.length}）</div>` +
      a.drafts.map((d) => `<div class="card draft"><h3>「${escapeHtml(d.name || "?")}」</h3>` +
        `<div class="reason">${escapeHtml(d.reason)}</div></div>`).join("");
  }
  if (a.excluded_relations && a.excluded_relations.length) {
    body += `<div class="section-title">不参与分析的“先于”关系（${a.excluded_relations.length}）</div>` +
      a.excluded_relations.map((r) => `<div class="card draft"><div class="reason">` +
        `「${escapeHtml(r.predecessor.name || "?")}」先于「${escapeHtml(r.successor.name || "?")}」` +
        `：${escapeHtml(r.reason)}</div></div>`).join("");
  }

  $("#dg-body").innerHTML = body;
  $("#diagnosis-modal").classList.remove("hidden");
}

/* ---------------------------------------------------------------- 事件绑定 */

$("#btn-new").addEventListener("click", async () => {
  const title = prompt("复盘标题：", "未命名复盘");
  if (title === null) return;
  const bundle = await api("POST", "/api/reviews", { title: title.trim() || "未命名复盘" });
  await loadReviews(bundle.review.id);
  await openReview(bundle.review.id);
});

$("#btn-delete").addEventListener("click", async () => {
  if (!state.current) return;
  if (!confirm(`确定删除复盘「${state.current.review.title}」吗？该操作不可恢复。`)) return;
  await api("DELETE", "/api/reviews/" + state.current.review.id);
  state.current = null;
  $("#main").classList.add("hidden");
  await loadReviews();
  if (state.reviews.length) await openReview(state.reviews[0].id);
});

$("#btn-refresh").addEventListener("click", () => {
  if (state.current) openReview(state.current.review.id);
});

$("#btn-reload-versions").addEventListener("click", () => {
  if (state.current) loadVersions();
});
$("#btn-compare").addEventListener("click", openCompare);
$("#merge-review").addEventListener("change", () => {
  $("#merge-event-picker").classList.add("hidden");
  state.merge = null;
  loadMergeVersions();
});
$("#btn-merge-load").addEventListener("click", loadMergeSource);
$("#btn-mm-close").addEventListener("click", closeMergeModal);
$("#merge-modal").addEventListener("click", (e) => {
  if (e.target.id === "merge-modal") closeMergeModal();
});
$("#btn-mm-confirm").addEventListener("click", confirmMerge);
$("#btn-cm-close").addEventListener("click", closeCompareModal);
$("#compare-modal").addEventListener("click", (e) => {
  if (e.target.id === "compare-modal") closeCompareModal();
});
$("#btn-vm-close").addEventListener("click", closeVersionModal);
$("#version-modal").addEventListener("click", (e) => {
  if (e.target.id === "version-modal") closeVersionModal();
});
$("#btn-dg-close").addEventListener("click", closeDiagnosisModal);
$("#diagnosis-modal").addEventListener("click", (e) => {
  if (e.target.id === "diagnosis-modal") closeDiagnosisModal();
});
/* 冲突结果中的“最小冲突约束组分析”入口（当前结果区与版本回看弹窗内共用，
   按钮用 data-version 指向要分析的可回看版本） */
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-diagnose");
  if (btn) openDiagnosis(parseInt(btn.dataset.version, 10));
});

$("#review-select").addEventListener("change", (e) => openReview(e.target.value));
$("#btn-save").addEventListener("click", save);
$("#btn-finalize").addEventListener("click", finalize);

$("#btn-add-event").addEventListener("click", () => {
  syncFromDOM();
  state.editEvents.push({
    key: newKey(), name: "新事件", timezone: "", earliest_start: "", latest_start: "",
    duration_minutes: "", note: "",
  });
  renderEventsTable();
  renderRelations();
});

$("#btn-add-relation").addEventListener("click", () => {
  const a = $("#rel-pre").value, b = $("#rel-suc").value;
  if (!a || !b) return;
  const min = parseGapInput($("#rel-min-gap").value, "最短等待", 0);
  const max = parseGapInput($("#rel-max-gap").value, "最长等待", null);
  if (!min.ok || !max.ok) {
    showBanner("⚠ " + escapeHtml(min.message || max.message));
    return;
  }
  if (max.value !== null && max.value < min.value) {
    showBanner("⚠ “先于”关系的最长等待不得短于最短等待");
    return;
  }
  hideBanner();
  state.editRelations.push({
    predecessor_key: a, successor_key: b,
    min_gap_minutes: min.value, max_gap_minutes: max.value === null ? "" : max.value,
  });
  renderRelations();
});

/* ---------------------------------------------------------------- 启动 */

(async function init() {
  await loadReviews();
  if (state.reviews.length) await openReview(state.reviews[0].id);
})();
