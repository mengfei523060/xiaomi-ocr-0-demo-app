"use strict";
/* Xiaomi-OCR-0 App — standalone UI. The built-in model (app/model) is detected
 * and started by the local server; the UI only shows its status. No external
 * model configuration is exposed. */

const $ = (s) => document.querySelector(s);
const LS_KEY = "xiaomi_ocr_app_settings";

const state = {
  task: "document",
  mode: "page",
  view: "pretty",
  image: null,          // {name, dataUrl}
  pdf: null,            // {name, dataUrl}
  busy: false,
  jobId: null,
  pollTimer: null,
  startedAt: 0,
  tickTimer: null,
  result: null,         // {content, meta}
};

let settings = {
  max_tokens: 4096, temperature: 0, concurrency: 4, dpi: 150, max_pages: null,
};
let modelConfig = null;   // 本机模型接入配置（来自 /api/model 或 serve）
let builtin = null;

/* ── settings persistence ──────────────────────────────────────────── */
function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    for (const k of ["max_tokens", "temperature", "concurrency", "dpi", "max_pages"]) {
      if (k in saved) settings[k] = saved[k];
    }
  } catch (_) {}
  $("#cfgMaxTokens").value = settings.max_tokens;
  $("#cfgTemp").value = settings.temperature;
  $("#cfgConcurrency").value = settings.concurrency;
  $("#cfgDpi").value = settings.dpi;
  $("#cfgMaxPages").value = settings.max_pages ?? "";
}
function saveSettings() {
  settings.max_tokens = clampInt($("#cfgMaxTokens").value, 1, 16384, 4096);
  settings.temperature = Math.min(2, Math.max(0, parseFloat($("#cfgTemp").value) || 0));
  settings.concurrency = clampInt($("#cfgConcurrency").value, 1, 16, 4);
  settings.dpi = clampInt($("#cfgDpi").value, 36, 300, 150);
  settings.max_pages = $("#cfgMaxPages").value ? clampInt($("#cfgMaxPages").value, 1, 10000, null) : null;
  localStorage.setItem(LS_KEY, JSON.stringify(settings));
}
function clampInt(v, lo, hi, fallback) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n >= lo && n <= hi ? n : fallback;
}
function currentConfig() {
  if (!modelConfig) return null;
  return { ...modelConfig, temperature: settings.temperature };
}

/* ── local model runtime (需自备 llama.cpp / Ollama) ───────────────── */
function setConn(kind, text) {
  $("#connDot").className = "dot " + kind;
  $("#connText").textContent = text;
}
function modelLine() { return $("#modelStatusText"); }

async function loadBuiltin() {
  try {
    builtin = await (await fetch("/api/model")).json();
  } catch (_) {
    modelLine().textContent = "无法读取模型状态";
    return;
  }
  if (builtin.preferred) {
    modelConfig = builtin.preferred;
    const r = builtin.runtimes;
    const where = builtin.preferred_source === "llama_cpp"
      ? `llama-server 运行中 · :${r.llama_cpp.port}`
      : `Ollama · ${r.ollama.matched}`;
    modelLine().textContent = `${builtin.model} · ${where}`;
    setConn("on", "本机模型就绪");
  } else {
    modelConfig = null;
    const ready = builtin.runtimes.llama_cpp.binary || (builtin.runtimes.ollama.binary && builtin.runtimes.ollama.reachable);
    modelLine().textContent = builtin.hint || "需要安装 llama.cpp 或 Ollama 并配置 xiaomi-ocr-0";
    setConn(ready ? "wait" : "off", ready ? "等待模型…" : "模型未就绪");
  }
}

async function startBuiltin() {
  // Mini does not download or import models: it reuses a running llama-server,
  // or starts one automatically when APP_LLAMA_MODEL points at local GGUF files.
  if (!builtin) await loadBuiltin();
  if (modelConfig) return modelConfig;
  try {
    const res = await (await fetch("/api/model/serve", { method: "POST" })).json();
    if (res.ok) {
      modelConfig = res.config;
      await loadBuiltin();
      return modelConfig;
    }
    await loadBuiltin();
    if (!modelConfig) {
      setConn("off", "模型未就绪");
      modelLine().textContent = builtin && builtin.hint ? builtin.hint : (res.error || "启动失败");
    }
    return modelConfig;
  } catch (e) {
    setConn("off", "模型状态读取失败");
    modelLine().textContent = `读取失败：${e.message}`;
    return null;
  }
}

async function ensureConfig() {
  if (modelConfig) return true;
  const cfg = await startBuiltin();
  if (!cfg) toast("模型不可用：请先安装 llama.cpp 或 Ollama 并配置 xiaomi-ocr-0（见 README）");
  return !!cfg;
}

/* ── toast ─────────────────────────────────────────────────────────── */
let toastTimer = null;
function toast(msg) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.append(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3800);
}

/* ── task & mode tabs ──────────────────────────────────────────────── */
function setTask(task) {
  state.task = task;
  document.querySelectorAll("#taskTabs button").forEach((b) => {
    const on = b.dataset.task === task;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on);
  });
  $("#kieFieldsWrap").hidden = task !== "kie";
  $("#vqaQuestionWrap").hidden = task !== "vqa";
  $("#docPromptWrap").hidden = task !== "document";
  $("#pdfNote").hidden = task !== "pdf";
  $("#btnRun").textContent = task === "pdf" ? "开始解析 PDF ↗" : "开始解析 ↗";
}

/* ── file selection ────────────────────────────────────────────────── */
function pickFile(file) {
  if (!file) return;
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  const reader = new FileReader();
  reader.onload = () => {
    if (isPdf) {
      state.pdf = { name: file.name, dataUrl: reader.result };
      state.image = null;
      if (state.task !== "pdf") setTask("pdf");
      $("#previewImg").hidden = true;
      $("#dropHint").innerHTML = `已选择 <b>${file.name}</b>（${(file.size / 1048576).toFixed(1)} MB）<br />点击「开始解析 PDF」`;
      $("#srcBadge").textContent = "PDF";
      $("#pageStrip").hidden = true;
    } else {
      state.image = { name: file.name, dataUrl: reader.result };
      state.pdf = null;
      if (state.task === "pdf") setTask("document");
      $("#previewImg").src = reader.result;
      $("#previewImg").hidden = false;
      $("#dropHint").hidden = true;
      $("#srcBadge").textContent = "IMAGE";
    }
  };
  reader.readAsDataURL(file);
}

/* ── samples ───────────────────────────────────────────────────────── */
async function loadSamples() {
  try {
    const data = await (await fetch("/api/samples")).json();
    const strip = $("#thumbs");
    strip.innerHTML = "";
    (data.items || []).forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "thumb";
      btn.innerHTML = `<img src="${item.image}" alt="" loading="lazy" /><span>${item.title}</span><small>${item.desc}</small>`;
      btn.onclick = async () => {
        const blob = await (await fetch(item.image)).blob();
        const reader = new FileReader();
        reader.onload = () => {
          state.image = { name: item.title + ".png", dataUrl: reader.result };
          setTask(item.task === "pdf" ? "document" : item.task);
          $("#previewImg").src = reader.result;
          $("#previewImg").hidden = false;
          $("#dropHint").hidden = true;
          $("#srcBadge").textContent = "样例";
          if (item.task === "kie" && !$("#kieFields").value) $("#kieFields").value = "";
          if (item.task === "vqa" && !$("#vqaQuestion").value) $("#vqaQuestion").value = "图片里写了什么？";
        };
        reader.readAsDataURL(blob);
      };
      strip.append(btn);
    });
  } catch (_) {}
}

/* ── runtime bar ───────────────────────────────────────────────────── */
function runtime(show, text, done, total) {
  // 状态行常驻显示，不再隐藏
  if (text !== undefined) $("#stageText").textContent = text;
  if (done !== undefined) {
    $("#progressBar").max = Math.max(1, total || 1);
    $("#progressBar").value = done;
    $("#countText").textContent = total ? `${done} / ${total}` : String(done);
  }
}
function startTick() {
  state.startedAt = performance.now();
  clearInterval(state.tickTimer);
  state.tickTimer = setInterval(() => {
    $("#elapsedText").textContent = ((performance.now() - state.startedAt) / 1000).toFixed(1) + "s";
  }, 200);
}
function stopTick(extra) {
  clearInterval(state.tickTimer);
  if (extra) $("#elapsedText").textContent = (extra / 1000).toFixed(1) + "s";
}

/* ── run: image tasks via SSE ──────────────────────────────────────── */
async function runImageTask() {
  if (!await ensureConfig()) return;
  if (!state.image) { toast("请先选择一张图片（或使用下方样例）"); return; }
  if (state.task === "vqa" && !$("#vqaQuestion").value.trim()) { toast("请输入问题"); return; }
  saveSettings();
  setBusy(true, "解析中…");
  startTick();
  const body = {
    config: currentConfig(),
    task: state.task,
    image_base64: state.image.dataUrl,
    max_tokens: settings.max_tokens,
    concurrency: settings.concurrency,
  };
  if (state.task === "document") {
    const p = $("#docPrompt").value.trim();
    if (p) body.prompt = p;
  } else if (state.task === "kie") {
    body.fields = $("#kieFields").value;
  } else {
    body.question = $("#vqaQuestion").value;
  }
  try {
    const resp = await fetch("/api/run/stream", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
      const err = await resp.json().catch(() => ({ error: "HTTP " + resp.status }));
      throw new Error(err.error || "HTTP " + resp.status);
    }
    await readSSE(resp.body, (event, data) => {
      if (event === "done") {
        finishSuccess(data);
      } else if (event === "error") {
        finishError(data.error);
      }
    });
  } catch (e) {
    finishError(e.message);
  }
}
async function readSSE(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message", data = null;
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7).trim();
        else if (line.startsWith("data: ")) { try { data = JSON.parse(line.slice(6)); } catch (_) {} }
      }
      if (data) onEvent(event, data);
    }
  }
}

/* ── run: PDF job ──────────────────────────────────────────────────── */
async function runPdfJob() {
  if (!await ensureConfig()) return;
  if (!state.pdf) { toast("请先上传 PDF 文件"); return; }
  saveSettings();
  setBusy(true, "上传并渲染页面…");
  startTick();
  seenPages.clear();
  $("#btnCancel").hidden = false;
  $("#pageStrip").innerHTML = "";
  $("#pageStrip").hidden = false;
  const body = {
    config: currentConfig(),
    pdf_base64: state.pdf.dataUrl,
    pdf_name: state.pdf.name,
    max_tokens: settings.max_tokens,
    concurrency: Math.min(settings.concurrency, 8),
    dpi: settings.dpi,
  };
  if (settings.max_pages) body.max_pages = settings.max_pages;
  const p = $("#docPrompt").value.trim();
  if (p) body.prompt = p;
  try {
    const resp = await fetch("/api/book", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!data.id) throw new Error(data.error || "启动失败");
    state.jobId = data.id;
    pollBookJob();
  } catch (e) {
    finishError(e.message);
  }
}
function pollBookJob() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    if (!state.jobId) return;
    let job;
    try {
      job = await (await fetch(`/api/book/${state.jobId}`)).json();
    } catch (_) { pollBookJob(); return; }
    const phase = job.phase || (job.status === "queued" ? "排队中" : job.status === "running" ? "解析中…" : job.status);
    runtime(true, phase, job.done || 0, job.total || 1);
    $("#srcBadge").textContent = `PDF · ${job.done || 0}/${job.total || 0} 页`;
    (job.page_log || []).forEach((row) => addThumb(row));
    if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
      if (job.status === "done" || job.markdown_ready) {
        const raw = await (await fetch(`/api/book/${state.jobId}/raw`)).json();
        finishSuccess({ content: raw.markdown || "", usage: job.usage || {},
                        latency_ms: job.elapsed_ms || (performance.now() - state.startedAt),
                        mode: job.mode, warning: job.error || "", pages: job.done });
      } else if (job.status === "cancelled") {
        finishError("已取消");
      } else {
        finishError(job.error || "解析失败");
      }
    } else {
      pollBookJob();
    }
  }, 900);
}
const seenPages = new Set();
function addThumb(row) {
  const key = row.page;
  if (seenPages.has(key)) return;
  seenPages.add(key);
  const img = document.createElement("img");
  img.src = row.img;
  img.alt = "第 " + row.page + " 页";
  img.title = `第 ${row.page} 页 · ${row.ms}ms${row.ok ? "" : " · 失败"}`;
  img.className = row.ok ? "" : "bad";
  img.onclick = () => window.open(row.img, "_blank");
  $("#pageStrip").append(img);
}

/* ── busy / finish ─────────────────────────────────────────────────── */
function setBusy(busy, text) {
  state.busy = busy;
  $("#btnRun").disabled = busy;
  if (text !== undefined) runtime(busy, text);
  if (!busy) {
    $("#btnCancel").hidden = true;
    clearTimeout(state.pollTimer);
    state.jobId = null;
  }
}
function finishSuccess(data) {
  stopTick(data.latency_ms);
  setBusy(false);
  runtime(true, "完成", undefined, undefined);
  $("#countText").textContent = "";
  $("#progressBar").value = $("#progressBar").max;
  $("#pillLat").textContent = "耗时 " + ((data.latency_ms || 0) / 1000).toFixed(1) + "s";
  const u = data.usage || {};
  $("#pillTok").textContent = "Token " + (u.total_tokens ?? "-") + (u.completion_tokens ? `（出 ${u.completion_tokens}）` : "");
  if (data.warning) toast("提示：" + data.warning);
  state.result = {
    content: data.content,
    kind: state.task === "kie" ? "json" : state.task === "vqa" ? "text" : "markdown",
    meta: { task: state.task, model: modelConfig ? modelConfig.model : "", backend: modelConfig ? modelConfig.backend : "", mode: data.mode,
            pages: data.pages, usage: data.usage, latency_ms: data.latency_ms,
            warning: data.warning || undefined },
  };
  $("#resBadge").textContent = "已完成";
  $("#btnCopy").disabled = false;
  $("#btnDownload").disabled = false;
  renderResult();
}
function finishError(message) {
  stopTick();
  setBusy(false);
  runtime(false);
  $("#resBadge").textContent = "失败";
  toast("失败：" + message);
}

/* ── result rendering ──────────────────────────────────────────────── */
function renderResult() {
  const el = $("#resText");
  $("#resEmpty").hidden = !!state.result;
  el.hidden = !state.result;
  if (!state.result) return;
  if (state.view === "raw") {
    el.innerHTML = `<pre class="raw">${escapeHtml(state.result.content)}</pre>`;
  } else if (state.view === "json") {
    el.innerHTML = `<pre class="raw">${escapeHtml(JSON.stringify(state.result.kind === "json" ? tryJson(state.result.content) : state.result.meta, null, 2))}</pre>`;
  } else if (state.result.kind === "json") {
    const obj = tryJson(state.result.content);
    el.innerHTML = `<div class="kv">${Object.entries(obj || {}).map(([k, v]) =>
      `<div class="kv-row"><span>${escapeHtml(k)}</span><b>${escapeHtml(typeof v === "object" ? JSON.stringify(v) : String(v))}</b></div>`).join("")}</div>`;
  } else if (state.result.kind === "text") {
    el.innerHTML = `<div class="answer">${escapeHtml(state.result.content)}</div>`;
  } else {
    let html;
    try {
      html = markdownToHtml(state.result.content);
    } catch (e) {
      html = `<pre class="raw">${escapeHtml(state.result.content)}</pre>`;
    }
    el.innerHTML = `<article class="md">${html}</article>`;
  }
}
function tryJson(s) { try { return JSON.parse(s); } catch (_) { return { raw: s }; } }

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* Markdown → HTML（服务器已把 OTSL 表格转成 HTML、公式转成 $…$） */
function markdownToHtml(src) {
  const store = [];
  const keep = (html) => { store.push(html); return "\u0000" + (store.length - 1) + "\u0000"; };

  let text = String(src || "");
  text = text.replace(/```([\s\S]*?)```/g, (_, c) => keep(`<pre class="code">${escapeHtml(c.replace(/^\n/, ""))}</pre>`));
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => keep(texHtml(t, true)));
  text = text.replace(/(^|[^\\$])\$([^$\n]+?)\$/g, (_, pre, t) => pre + keep(texHtml(t, false)));
  text = text.replace(/<table[\s\S]*?<\/table>/gi, (t) => keep(sanitizeTable(t)));

  text = escapeHtml(text);
  text = text.replace(/^(#{1,6})\s+(.*)$/gm, (_, h, t) => `<h${h.length + 1}>${inline(t)}</h${h.length + 1}>`);
  text = text.replace(/^\s*([-*•])\s+(.*)$/gm, "<li>$2</li>");
  text = text.replace(/^\s*\d+[.、)]\s+(.*)$/gm, "<li>$1</li>");
  text = text.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  text = text.replace(/^\s*(---|—{2,})\s*$/gm, "<hr />");
  text = text.split(/\n{2,}/).map((block) => {
    const t = block.trim();
    if (!t) return "";
    if (/^<(h\d|ul|ol|li|hr|pre|table|div|article)/i.test(t) || /^\u0000\d+\u0000$/.test(t)) return t;
    return `<p>${inline(t).replace(/\n/g, "<br />")}</p>`;
  }).join("\n");

  return text.replace(/\u0000(\d+)\u0000/g, (_, i) => store[+i] || "");
}
function inline(t) {
  return String(t)
    .replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}
function texHtml(tex, display) {
  if (typeof katex === "undefined") return `<span class="math">${escapeHtml(tex)}</span>`;
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "html" });
  } catch (_) {
    return `<span class="math">${escapeHtml(tex)}</span>`;
  }
}
function sanitizeTable(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  const allowed = new Set(["TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD", "CAPTION", "BR",
                           "B", "STRONG", "EM", "I", "SUB", "SUP", "SPAN", "DIV", "P"]);
  (function clean(node) {
    const kids = [...node.childNodes];
    for (const kid of kids) {
      if (kid.nodeType === 3) continue;
      if (kid.nodeType !== 1 || !allowed.has(kid.tagName)) { kid.replaceWith(document.createTextNode(kid.textContent || "")); continue; }
      [...kid.attributes].forEach((a) => kid.removeAttribute(a.name));
      clean(kid);
    }
  })(template.content);
  return template.innerHTML;
}

/* ── actions ───────────────────────────────────────────────────────── */
function bindEvents() {
  ["cfgMaxTokens", "cfgTemp", "cfgConcurrency", "cfgDpi", "cfgMaxPages"].forEach((id) => {
    $("#" + id).addEventListener("change", saveSettings);
  });
  $("#btnCfg").addEventListener("click", () => {
    const panel = $("#settingsPanel");
    panel.hidden = !panel.hidden;
    $("#btnCfg").setAttribute("aria-expanded", String(!panel.hidden));
    $("#btnCfg").classList.toggle("open", !panel.hidden);
  });

  document.querySelectorAll("#taskTabs button").forEach((b) => b.addEventListener("click", () => setTask(b.dataset.task)));
  document.querySelectorAll("#viewSeg button").forEach((b) => b.addEventListener("click", () => {
    state.view = b.dataset.view;
    document.querySelectorAll("#viewSeg button").forEach((x) => {
      const on = x === b;
      x.classList.toggle("active", on);
      x.setAttribute("aria-pressed", on);
    });
    renderResult();
  }));

  $("#btnRun").addEventListener("click", () => (state.task === "pdf" ? runPdfJob() : runImageTask()));
  $("#btnCancel").addEventListener("click", async () => {
    if (state.jobId) { try { await fetch(`/api/book/${state.jobId}/cancel`, { method: "POST" }); } catch (_) {} }
    finishError("已取消");
  });
  $("#btnUpload").addEventListener("click", () => $("#fileInput").click());
  $("#dropZone").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (e) => pickFile(e.target.files[0]));
  const dz = $("#dropZone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("hover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("hover"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("hover"); pickFile(e.dataTransfer.files[0]); });

  $("#btnCopy").addEventListener("click", async () => {
    if (!state.result) return;
    try { await navigator.clipboard.writeText(state.result.content); toast("已复制到剪贴板"); }
    catch (_) { toast("复制失败，请在「原文」视图手动复制"); }
  });
  $("#btnDownload").addEventListener("click", () => {
    if (!state.result) return;
    const ext = state.result.kind === "json" ? "json" : "md";
    let stem = (state.pdf && state.pdf.name) || (state.image && state.image.name) || "";
    stem = stem.replace(/\.[A-Za-z0-9]+$/, "").replace(/[\\/:*?"<>|]/g, "_").slice(0, 60);
    const name = (stem || "xiaomi-ocr-result") + "." + ext;
    try {
      // 首选浏览器本地下载（不经过服务器，兼容性最好）
      const blob = new Blob([state.result.content], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.append(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 5000);
      toast("已下载 " + name);
    } catch (_) {
      // 兜底：服务器附件下载（隐藏 iframe 承载，避免页面跳转）
      let fr = $("#exportFrame");
      if (!fr) {
        fr = document.createElement("iframe");
        fr.id = "exportFrame";
        fr.name = "exportFrame";
        fr.hidden = true;
        document.body.append(fr);
      }
      const form = document.createElement("form");
      form.method = "POST";
      form.action = "/api/export";
      form.target = "exportFrame";
      const mk = (n, v) => { const i = document.createElement("input"); i.type = "hidden"; i.name = n; i.value = v; form.append(i); };
      mk("content", state.result.content);
      mk("format", ext);
      document.body.append(form);
      form.submit();
      form.remove();
      toast("正在下载 " + name + " …");
    }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !state.busy) (state.task === "pdf" ? runPdfJob() : runImageTask());
  });
}

/* ── boot ──────────────────────────────────────────────────────────── */
async function main() {
  loadSettings();
  bindEvents();
  setTask("document");
  loadSamples();
  await loadBuiltin();
  if (!modelConfig) {
    startBuiltin();
    // 用户可能正在后台安装/拉取模型：每 5 秒复查一次，就绪即停
    const retry = setInterval(async () => {
      await loadBuiltin();
      if (modelConfig) clearInterval(retry);
    }, 5000);
  }
  if (location.search.includes("settings=1")) $("#btnCfg").click();
}
main();
