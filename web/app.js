/* 星空AI视频翻译 — 前端逻辑（原生 JS，无框架） */
const $ = (id) => document.getElementById(id);

const STAGE_NAMES = {
  download: "下载/准备",
  translate: "AI 翻译",
  dub: "中文配音",
  burn: "字幕烧录",
  engine: "引擎安装",
  ytdlp: "下载器更新",
  done: "完成",
  cancelled: "已取消",
  error: "出错",
};

let POLL = null;
let LAST_LOG_LEN = 0;
let DEFAULT_OUT = "";   // 兜底输出目录（软件目录下的 output\）：链接下载、视频旁边写不进去时用它

// ---------- 初始化 ----------
let LINKS = {};

async function boot() {
  const data = await pywebview.api.boot();
  const s = data.settings;
  LINKS = data.links || {};

  // 语言与音色
  const opts = data.langs.map(([v, n]) => `<option value="${v}">${n}</option>`).join("");
  const vopts = data.voices.map((v) => `<option value="${v}">${v}</option>`).join("");
  $("sel-lang").innerHTML = opts;
  $("sel-voice").innerHTML = vopts;
  $("sel-lang-b").innerHTML = opts;
  $("sel-voice-b").innerHTML = vopts;
  $("sel-lang").value = $("sel-lang-b").value = s.source_lang || "en";
  $("sel-voice").value = $("sel-voice-b").value = s.voice || "冰糖";
  // 配音/烧录的勾选也记住上次的选择（设置里没有这两项时按默认「都开」）
  $("chk-dub").checked = $("chk-dub-b").checked = s.do_dub !== false;
  $("chk-burn").checked = $("chk-burn-b").checked = s.burn_subs !== false;

  // key 状态提示交给「打开设置时自动检测」填（不再显示"已配置"）

  // key 明文回填（直接显示已保存的，方便核对）
  const kp = s.keys_plain || {};
  $("set-key-mimo").value = kp.mimo || "";
  $("set-key-deepseek").value = kp.deepseek || "";
  $("set-key-gemini").value = kp.gemini || "";

  // 设置回填
  $("set-exe").value = s.finesub_exe || "finesub";
  $("set-home").value = s.finesub_home || "";
  $("set-proxy").value = s.proxy || "";
  $("set-github-mirror").value = s.github_mirror || "auto";
  $("set-out").value = s.output_dir || "";
  // 留空时成品到底存哪儿：本地视频存在视频旁边的 output\，链接下载没有本地来源、
  // 还是软件目录。光写死一条路径会误导，所以跟着当前输入走（见 refreshOutHint）
  DEFAULT_OUT = s.default_output_dir || "";
  refreshOutHint();
  $("set-vctx").checked = !!s.video_context;
  $("set-wsearch").checked = !!s.web_search;
  $("set-ytsubs").value = s.yt_subs || "manual";
  $("set-ytauto").checked = s.yt_auto_extra !== false;
  $("set-tts-style").value = s.tts_style || "";
  $("set-tts-clone").checked = !!s.tts_clone;
  $("set-auto-dl").checked = s.auto_update_downloader !== false;
  // 背景亮度（设置 → 输出与界面）：跟着设置走，默认 55%
  $("set-bgopacity").value = Math.round((s.bg_opacity ?? 0.55) * 100);
  applyBgOpacity();
  const ov = Math.round((s.orig_vol ?? 0.0) * 100);
  $("rng-orig").value = ov;
  $("orig-val").textContent = ov + "%";
  fillSlider("rng-orig", ov);
  setMode(MODE);           // 把「翻译/批量」的导航高亮和右下角按钮摆到位

  render(data.status);
  renderTools(data.tools);
  refreshEngine();
  refreshEnv();            // 环境快检（不联网）：顶栏「环境自检」入口显示状态
  // 标题栏 + 顶栏都带版本号：客户截图报障时一眼看出装的是哪一版
  const APP_VER = data.app_version || "?";
  document.title = "星空AI视频翻译 v" + APP_VER;
  $("brand-ver").textContent = "v" + APP_VER;
  $("ver-text").textContent = "当前版本 v" + APP_VER;
  refreshDownloader();
  checkUpdateState();
  setTimeout(checkUpdateState, 6000);   // 启动时的自动检查是后台跑的，稍后再看一次
}

// ---------- 左侧导航 + 页面路由 ----------
// 版式参考 Dream Unreal Manager：导航是一条图标栏；翻译/批量是同一个工作页的两种模式，
// 环境自检和设置从原来的弹窗改成了独立页面（大标题在页头，操作在页头右侧）。
let MODE = "single";
let PAGE = "work";

function markRail() {
  const key = PAGE === "work" ? MODE : PAGE;
  document.querySelectorAll(".rail-btn").forEach((b) => {
    b.classList.toggle("active", (b.dataset.mode || b.dataset.page) === key);
  });
}

const PAGE_TITLE = { work: () => (MODE === "batch" ? "批量翻译" : "视频翻译"), env: () => "环境自检", settings: () => "设置" };

function setPage(p) {
  PAGE = p;
  ["work", "env", "settings"].forEach((x) => $("page-" + x).classList.toggle("hidden", x !== p));
  $("page-sub-work").classList.toggle("hidden", p !== "work");
  $("page-sub-env").classList.toggle("hidden", p !== "env");
  $("page-sub-settings").classList.toggle("hidden", p !== "settings");
  // 页头右侧的操作按钮跟着页面走
  ["btn-env-recheck", "btn-env-diag", "btn-env-close"].forEach((id) =>
    $(id).classList.toggle("hidden", p !== "env")
  );
  $("btn-close").classList.toggle("hidden", p !== "settings");
  $("page-title").textContent = (PAGE_TITLE[p] || PAGE_TITLE.work)();
  markRail();
  if (p !== "settings") closeSettings();     // 离开设置页 = 失焦 + 把没落盘的改动写掉
  else { autoTestKeys(); refreshDownloader(); refreshOutHint(); }
}

function setMode(m) {
  MODE = m;
  $("panel-single").classList.toggle("hidden", m !== "single");
  $("panel-batch").classList.toggle("hidden", m !== "batch");
  // 右下角浮动主操作：单个/批量各一套（取消键由 setRunning 管）
  $("btn-start").classList.toggle("hidden", m !== "single");
  $("btn-start-batch").classList.toggle("hidden", m !== "batch");
  $("btn-scan").classList.toggle("hidden", m !== "batch");
  if (PAGE !== "work") setPage("work");
  else { $("page-title").textContent = PAGE_TITLE.work(); markRail(); }
}
document.querySelectorAll(".rail-btn[data-mode]").forEach((b) =>
  b.addEventListener("click", () => setMode(b.dataset.mode))
);
$("page-title").textContent = PAGE_TITLE.work();

// ---------- 批量：来源切换 ----------
function batchKind() { return $("seg-batch").dataset.active === "playlist" ? "playlist" : "folder"; }
document.querySelectorAll("#seg-batch .seg-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    switchSeg($("seg-batch"), btn);
    const v = btn.dataset.v;
    $("row-bfolder").classList.toggle("hidden", v !== "folder");
    $("row-burl").classList.toggle("hidden", v !== "playlist");
  })
);

$("btn-pick-folder").addEventListener("click", async () => {
  const d = await pywebview.api.pick_dir();
  if (d) {
    $("in-bfolder").value = d;
    refreshOutHint();
    scanBatch();
  }
});

function batchParams() {
  return {
    source: batchKind() === "folder" ? $("in-bfolder").value.trim() : $("in-burl").value.trim(),
    source_lang: $("sel-lang-b").value,
    voice: $("sel-voice-b").value,
    extra_info: $("in-extra-b").value.trim(),
    do_dub: $("chk-dub-b").checked,
    burn_subs: $("chk-burn-b").checked,
    orig_vol: parseInt($("rng-orig").value, 10) / 100,
  };
}

// 合集：扫出来的标题交给后端认软件，认得出就自动填「背景信息」
let AUTO_EXTRA_B = "";
function fillBatchExtra(titles) {
  const box = $("in-extra-b");
  if (!box || !titles || !titles.length) return;
  const cur = box.value.trim();
  if (cur && cur !== AUTO_EXTRA_B) return;          // 用户自己写过，不碰
  pywebview.api.probe_titles(titles).then((r) => {
    if (!r || !r.ok || !r.extra) return;
    const now = box.value.trim();
    if (now && now !== AUTO_EXTRA_B) return;
    box.value = r.extra;
    AUTO_EXTRA_B = r.extra;
    const h = $("extra-hint-b");
    if (h) {
      h.textContent = r.apps && r.apps.length
        ? `按合集标题认出：${r.apps.join("、")} · 这段可以自己改`
        : "没认出具体软件 · 可以自己补一句";
    }
  }).catch(() => {});
}

function scheduleBatchExtraProbe(src) {
  if (EXTRA_TIMER) clearTimeout(EXTRA_TIMER);
  EXTRA_TIMER = setTimeout(() => maybeFillExtra(src), 600);
}
$("in-burl").addEventListener("change", () => scheduleBatchExtraProbe($("in-burl").value.trim()));
$("in-extra-b").addEventListener("input", () => { AUTO_EXTRA_B = "";
  const h = $("extra-hint-b"); if (h) h.textContent = ""; });

async function scanBatch() {
  const btn = $("btn-scan");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  try {
    const r = await pywebview.api.scan_source({ ...batchParams(), batch_kind: batchKind() });
    if (!r.ok) { alert(r.error); return; }
    renderQueue(r.items.map((it) => ({ title: it.title, status: "pending", detail: "" })));
    $("stage-text").textContent = `扫描到 ${r.items.length} 个视频`;
    fillBatchExtra(r.items.map((it) => it.title));
  } finally {
    btn.disabled = false;
    btn.textContent = "扫描列表";
  }
}
$("btn-scan").addEventListener("click", scanBatch);

$("btn-start-batch").addEventListener("click", async () => {
  const r = await pywebview.api.start_batch({ ...batchParams(), batch_kind: batchKind() });
  if (!r.ok) { alert(r.error); return; }
  LAST_LOG_LEN = 0;
  $("log").textContent = "";
  setRunning(true);
  startPolling();
});

$("btn-cancel-b").addEventListener("click", async () => {
  await pywebview.api.cancel();
});

function renderQueue(queue) {
  const el = $("queue");
  if (!queue || !queue.length) { el.innerHTML = ""; return; }
  const icons = { pending: "○", running: "◐", done: "✓", failed: "✕" };
  el.innerHTML = queue
    .map((it) => {
      const st = it.status || "pending";
      return `<div class="q-item ${st}">
        <span class="q-icon">${icons[st] || "○"}</span>
        <span class="q-title">${esc(it.title)}</span>
        <span class="q-detail">${esc(it.detail || "")}</span>
      </div>`;
    })
    .join("");
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// key 连通性测试（手动点「测试」= 测当前输入框里的值）
const KEY_FIELDS = { mimo: ["set-key-mimo", "hint-mimo"], deepseek: ["set-key-deepseek", "hint-deepseek"], gemini: ["set-key-gemini", "hint-gemini"] };
document.querySelectorAll("[data-test]").forEach((btn) =>
  btn.addEventListener("click", () => testOneKey(btn.dataset.test))
);

// 申请链接：用系统浏览器打开
document.querySelectorAll(".apply").forEach((a) =>
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const url = LINKS[a.dataset.k];
    if (url) pywebview.api.open_url(url);
  })
);

// ---------- 检查更新 ----------
async function checkUpdateState() {
  try {
    const st = await pywebview.api.update_state();
    if (st && st.has_update && !SESSION_IGNORED_UPDATE) {
      $("update-ver").textContent = "v" + st.version;
      $("update-notes").textContent = st.notes || "";
      $("update-bar").classList.remove("hidden");
      $("btn-do-update").onclick = async () => {
        // sha256 一并带上：下载完校验，避免半个安装包把更新装坏
        const r = await pywebview.api.do_update(st.pkg_url, st.sha256 || "");
        if (!r.ok) { alert(r.error); return; }
        LAST_LOG_LEN = 0;
        $("log").textContent = "";
        setRunning(true);
        startPolling();
      };
      $("btn-ignore-update").onclick = () => {
        SESSION_IGNORED_UPDATE = true;
        $("update-bar").classList.add("hidden");
      };
    }
  } catch (_) {}
}
let SESSION_IGNORED_UPDATE = false;

$("btn-check-update").addEventListener("click", async () => {
  const btn = $("btn-check-update");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "检查中…";
  try {
    const r = await pywebview.api.check_update();
    if (!r.ok) { alert(r.error || "检查失败"); return; }
    alert(r.has_update ? `发现新版本 v${r.version}！\n\n${r.notes || ""}\n\n点右下角「确定」后，界面顶部会出现更新提示条。`
                       : `当前已是最新版本（v${r.current}）`);
    await checkUpdateState();
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

// ---------------- 下载器（yt-dlp）状态 ----------------
// 下载 YouTube 用的是随包的 yt-dlp，软件每次启动会自动把它更新到最新
// （YouTube 的反爬规则变得比软件的版本节奏快）。这里只是把它现在装的是哪一版
// 告诉用户，"下不了 YouTube 视频"时也有个手动入口。
async function refreshDownloader() {
  const el = $("dl-text");
  if (!el) return;
  try {
    const r = await pywebview.api.downloader_state();
    if (!r.ok) { el.textContent = "读取失败"; return; }
    const ch = r.channel === "nightly" ? "每日构建版" : "稳定版";
    el.textContent = `yt-dlp ${r.version}（${ch}）`;
  } catch (e) {
    el.textContent = "读取失败";
  }
}

$("btn-update-dl").addEventListener("click", async () => {
  const r = await pywebview.api.update_downloader();
  if (!r.ok) { alert(r.error); return; }
  LAST_LOG_LEN = 0;
  $("log").textContent = "";
  setRunning(true);
  startPolling();
});

// ---------------- 翻译引擎状态 ----------------
async function refreshEngine() {
  const e = await pywebview.api.engine_status();
  const dot = $("engine-dot"), txt = $("engine-text"), btn = $("btn-install-engine");
  if (e.ready) {
    dot.className = "engine-dot ok";
    txt.textContent = "翻译引擎已就绪";
    btn.classList.add("hidden");
  } else {
    dot.className = "engine-dot warn";
    txt.textContent = "未检测到翻译引擎（finesub）—— 需先安装才能翻译";
    btn.classList.remove("hidden");
  }
}

$("btn-install-engine").addEventListener("click", async () => {
  const r = await pywebview.api.engine_install();
  if (!r.ok) { alert(r.error); return; }
  LAST_LOG_LEN = 0;
  $("log").textContent = "";
  setRunning(true);
  startPolling();
});

// ---------------- 随包组件自检（ffmpeg / ffprobe / yt-dlp） ----------------
const TOOL_HINTS = {
  "ffmpeg": "视频压制、配音合成",
  "ffprobe": "音视频时长探测",
  "yt-dlp": "YouTube 链接下载",
};
let LAST_TOOLS = null;

function renderTools(ck) {
  const bar = $("tools-bar");
  if (!bar) return;
  const miss = (ck && ck.missing) || [];
  const sig = miss.join(",");
  if (sig === LAST_TOOLS) return;          // 没变化就别动 DOM（状态每 0.5s 轮询一次）
  LAST_TOOLS = sig;
  if (!miss.length) { bar.classList.add("hidden"); return; }
  const names = miss.map((n) => `${n}（${TOOL_HINTS[n] || ""}）`).join("、");
  $("tools-text").innerHTML =
    `⚠️ 缺少运行组件：<b>${esc(names)}</b>，相关功能会失败。` +
    `这些程序随安装包自带，可能被<b>杀毒软件隔离</b>了——请检查杀软隔离区并恢复，或重新安装本软件。`;
  bar.classList.remove("hidden");
}

$("btn-check-tools").addEventListener("click", async () => {
  const btn = $("btn-check-tools");
  btn.disabled = true;
  btn.textContent = "检测中…";
  try {
    renderTools(await pywebview.api.env_check());
  } finally {
    btn.disabled = false;
    btn.textContent = "重新检测";
  }
});

// ---------- 输入切换（自绘分段控件） ----------
function srcType() { return $("seg-src").dataset.active === "url" ? "url" : "file"; }

/** 通用分段控件切换：更新 data-active / 滑动块位置 / 选中态 */
function switchSeg(segEl, btn) {
  const btns = [...segEl.querySelectorAll(".seg-btn")];
  segEl.dataset.active = btn.dataset.v;
  segEl.classList.toggle("pos2", btns.indexOf(btn) === 1);   // 第二项 → 滑块右移
  btns.forEach((b) => b.classList.toggle("active", b === btn));
}

document.querySelectorAll("#seg-src .seg-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    switchSeg($("seg-src"), btn);
    const isFile = srcType() === "file";
    $("row-file").classList.toggle("hidden", !isFile);
    $("row-url").classList.toggle("hidden", isFile);
  })
);

// 「输出目录」留空时成品到底存哪儿 —— 跟着输入走：本地视频 / 文件夹是它旁边的
// output\，链接下载没有本地来源、还是软件目录。规则以后端 pipeline.resolve_output_base
// 为准，这里只是把它显示出来，免得用户以为还是老地方。
function refreshOutHint() {
  const el = $("set-out");
  if (!el || el.value.trim()) return;      // 自己填了目录就不用提示了
  const file = ($("in-file").value || "").trim();
  const dir = ($("in-bfolder").value || "").trim();
  if (file) {
    const parent = file.replace(/[\\/][^\\/]*$/, "");
    el.placeholder = parent ? parent + "\\output" : DEFAULT_OUT;
  } else if (dir) {
    el.placeholder = dir.replace(/[\\/]+$/, "") + "\\output";
  } else {
    el.placeholder = DEFAULT_OUT;
  }
}

$("btn-pick").addEventListener("click", async () => {
  const p = await pywebview.api.pick_video();
  if (p) { $("in-file").value = p; refreshOutHint(); maybeFillExtra(p); }
});

// ---------- 按标题自动填「背景信息」 ----------
// 链接走 yt-dlp 取一次标题（不下载视频）；本地文件直接用文件名。
// 认出的软件还会让知识库里的术语条目被翻译模型挑中（专有名词的正确拼写跟着进去）。
let AUTO_EXTRA = "";        // 上一次自动填进去的内容——不是它就别覆盖，用户自己写过字要留住
let EXTRA_TIMER = null;

function maybeFillExtra(src) {
  const box = $("in-extra");
  if (!box || !src) return;
  const cur = box.value.trim();
  if (cur && cur !== AUTO_EXTRA) return;            // 用户自己写的，不碰
  const hint = $("extra-hint");
  if (hint) hint.textContent = "正在按标题识别…";
  pywebview.api.probe(src).then((r) => {
    if (!r || !r.ok || !r.extra) {
      if (hint) hint.textContent = r && r.error ? "" : "";
      return;
    }
    const now = box.value.trim();
    if (now && now !== AUTO_EXTRA) return;          // 等结果的工夫用户自己填了，让给他
    box.value = r.extra;
    AUTO_EXTRA = r.extra;
    if (hint) {
      hint.textContent = r.apps && r.apps.length
        ? `已认出：${r.apps.join("、")} · 这段可以自己改`
        : "没认出具体软件 · 可以自己补一句";
    }
  }).catch(() => { if (hint) hint.textContent = ""; });
}

function scheduleExtraProbe(src) {
  if (EXTRA_TIMER) clearTimeout(EXTRA_TIMER);
  EXTRA_TIMER = setTimeout(() => maybeFillExtra(src), 600);   // 别每敲一个字符就打一次网络
}

$("in-url").addEventListener("change", () => scheduleExtraProbe($("in-url").value.trim()));
$("in-url").addEventListener("blur", () => scheduleExtraProbe($("in-url").value.trim()));
$("in-extra").addEventListener("input", () => { AUTO_EXTRA = "";  // 用户接手了，提示收掉
  const h = $("extra-hint"); if (h) h.textContent = ""; });

$("btn-pick-out").addEventListener("click", async () => {
  const d = await pywebview.api.pick_dir();
  if (d) { $("set-out").value = d; queueSave(); }   // 程序填的值不触发 change，手动存
});
$("set-out").addEventListener("input", refreshOutHint);   // 清空时把「默认存哪儿」提示回来
$("btn-pick-home").addEventListener("click", async () => {
  const d = await pywebview.api.pick_dir();
  if (d) { $("set-home").value = d; queueSave(); }
});

// 音色试听
$("btn-preview").addEventListener("click", async () => {
  const btn = $("btn-preview");
  const voice = $("sel-voice").value;
  btn.disabled = true;
  btn.textContent = "合成中…";
  try {
    const r = await pywebview.api.preview_voice(voice);
    if (!r.ok) { alert(r.error || "试听失败"); return; }
    new Audio(r.file).play();
  } catch (e) {
    alert("试听失败：" + e);
  } finally {
    btn.disabled = false;
    btn.textContent = "试听";
  }
});

// 滑杆：除了数值，还要把「已填充比例」写给 CSS（--p），轨道左边才是强调色
function fillSlider(id, pct) {
  $(id).style.setProperty("--p", pct + "%");
}

// 原声音量滑杆
$("rng-orig").addEventListener("input", () => {
  $("orig-val").textContent = $("rng-orig").value + "%";
  fillSlider("rng-orig", $("rng-orig").value);
});
$("rng-orig").addEventListener("change", () => {
  pywebview.api.save_settings({ orig_vol: parseInt($("rng-orig").value, 10) / 100 });
});

// 背景亮度：星空那层的强度（0% = 纯深色底）
function applyBgOpacity() {
  const v = parseInt($("set-bgopacity").value, 10) || 0;
  $("bg-layer").style.opacity = (v / 100).toFixed(2);
  $("bgopacity-val").textContent = v + "%";
  fillSlider("set-bgopacity", v);
}
$("set-bgopacity").addEventListener("input", applyBgOpacity);   // 拖着就能实时预览
// （存盘走设置页统一的那套「变即存」，见下面的 queueSave）

// 主界面的选择也存进设置，下次打开还是这些。
// 客户的实际用法是「原声 + 外挂字幕」（配音和烧录都不需要），不记住的话
// 每开一次软件都要重新选源语言 + 关掉两个勾，太反人类。
// 只存**改动的那一项**，免得动一个控件把另一个面板的选择覆盖掉。
const MAIN_SAVE = {
  "sel-lang": "source_lang", "sel-lang-b": "source_lang",
  "sel-voice": "voice", "sel-voice-b": "voice",
  "chk-dub": "do_dub", "chk-dub-b": "do_dub",
  "chk-burn": "burn_subs", "chk-burn-b": "burn_subs",
};
Object.entries(MAIN_SAVE).forEach(([id, key]) => {
  $(id).addEventListener("change", (e) => {
    const el = e.currentTarget;
    pywebview.api.save_settings({ [key]: el.type === "checkbox" ? el.checked : el.value });
  });
});

// ---------- 任务控制 ----------
$("btn-start").addEventListener("click", async () => {
  const params = {
    source: srcType() === "file" ? $("in-file").value.trim() : $("in-url").value.trim(),
    source_lang: $("sel-lang").value,
    voice: $("sel-voice").value,
    extra_info: $("in-extra").value.trim(),
    orig_vol: parseInt($("rng-orig").value, 10) / 100,
    do_dub: $("chk-dub").checked,
    burn_subs: $("chk-burn").checked,
  };
  const r = await pywebview.api.start(params);
  if (!r.ok) { alert(r.error); return; }
  LAST_LOG_LEN = 0;
  $("log").textContent = "";
  $("result-actions").style.display = "none";
  setRunning(true);
  startPolling();
});

$("btn-cancel").addEventListener("click", async () => {
  await pywebview.api.cancel();
});

function setRunning(running) {
  $("btn-start").disabled = running;
  $("btn-cancel").classList.toggle("hidden", !running);
  $("btn-start-batch").disabled = running;
  $("btn-cancel-b").classList.toggle("hidden", !running);
}

function startPolling() {
  if (POLL) clearInterval(POLL);
  POLL = setInterval(async () => {
    const st = await pywebview.api.get_status();
    render(st);
    if (!st.running) {
      clearInterval(POLL); POLL = null; setRunning(false);
      refreshEngine();   // 任务/安装结束后刷新引擎状态
    }
  }, 500);
}

// ---------- 渲染 ----------
// 日志分级：错误/警告/成功/进度/普通
function classify(line) {
  // 「失败 0」不算错误：批量收尾那行是「🎉 批量完成：成功 5，失败 0」
  const s = String(line).replace(/失败\s*[:：]?\s*0(?!\d)/g, "");
  if (/❌|失败|出错|错误|error|exception|traceback/i.test(s)) return "l-err";
  if (/⚠|warning|警告|跳过/i.test(s)) return "l-warn";
  if (/✅|🎉|完成|就绪|已保存|已生成/.test(s)) return "l-ok";
  if (/\d+(\.\d+)?%/.test(s)) return "l-prog";
  return "";
}

function render(st) {
  if (!st) return;
  const pct = st.percent || 0;
  $("bar").style.width = pct + "%";
  // 配音阶段后端会带「已合成 X/Y · 预计剩余」；本机克隆要跑很久，别只给百分比
  $("stage-text").textContent = (st.stage === "dub" && st.message)
    ? st.message
    : (STAGE_NAMES[st.stage] || st.stage || "等待开始");
  $("pct-text").textContent = pct + "%";

  // 状态横幅：一眼区分 在跑 / 出错 / 完成
  const banner = $("state-banner"), stext = $("state-text");
  if (st.error) {
    banner.className = "state-banner err";
    stext.textContent = "出错：" + st.error;
  } else if (st.running) {
    banner.className = "state-banner run";
    stext.textContent = (STAGE_NAMES[st.stage] || "处理") + " 进行中…";
  } else if (st.stage === "done") {
    banner.className = "state-banner ok";
    stext.textContent = "已完成";
  } else if (st.stage === "cancelled") {
    banner.className = "state-banner warn";
    stext.textContent = "已取消";
  } else {
    banner.className = "state-banner idle";
    stext.textContent = "就绪";
  }

  if (st.log && st.log.length) {
    const el = $("log");
    // ★ 只追加新增的行，**绝不整体重建**。
    //   以前这里是 `el.innerHTML = st.log.map(...)` —— 每秒轮询重建一次，
    //   用户刚框选中的文字立刻被新节点顶掉，等于"日志根本选不住、复制不了"。
    //   追加还有个好处：自动跟随只在贴着底部时才生效，往上翻着看不会被拽回去。
    // ★ 后端每次只回**最后 300 行**，另给 log_index（这一截第一行的绝对序号）。
    //   只按数组长度记 LAST_LOG_LEN 的话，窗口填满后长度恒为 300，新行永远推不出来
    //   （2026-10-21 界面日志停更的根因）。
    const base = st.log_index | 0;
    const total = base + st.log.length;
    if (total < LAST_LOG_LEN) {     // 换任务 / 后端截断过 → 整体重建
      el.textContent = "";
      LAST_LOG_LEN = base;
    }
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    const frag = document.createDocumentFragment();
    for (let i = Math.max(0, LAST_LOG_LEN - base); i < st.log.length; i++) {
      const d = document.createElement("div");
      d.className = classify(st.log[i]);
      d.textContent = st.log[i];  // 用 textContent，日志内容不会被当 HTML 解析
      frag.appendChild(d);
    }
    if (frag.childNodes.length) {
      el.appendChild(frag);
      if (stick) el.scrollTop = el.scrollHeight;
    }
    LAST_LOG_LEN = total;
  }

  const res = st.result;
  if (res) {
    $("result-actions").style.display = "flex";
    const final = res.final || res.dubbed || res.srt || res.out_dir;
    $("btn-open-final").onclick = () => pywebview.api.open_path(final);
    $("btn-open-dir").onclick = () => pywebview.api.open_path(res.out_dir);
    $("btn-open-final").textContent = res.batch
      ? `打开输出目录（成功 ${res.ok} / 失败 ${res.failed}）`
      : res.final ? "打开成片" : res.dubbed ? "打开配音版" : "打开字幕";
  }
  if (st.error) $("stage-text").textContent = "出错：" + st.error;
  renderTools(st.tools);
  renderQueue(st.queue);
}

// ---------- 设置 ----------
// ---------- 设置：打开即自动检测 key 连通性 ----------
async function testOneKey(kind) {
  const [inId, hintId] = KEY_FIELDS[kind];
  const key = $(inId).value.trim();
  if (!key) {
    $(hintId).textContent = "未配置";
    $(hintId).style.color = "#ff9aa2";
    return;
  }
  $(hintId).textContent = "检测中…";
  $(hintId).style.color = "";
  try {
    const r = await pywebview.api.test_key(kind, key);
    $(hintId).textContent = r.ok ? "✅ " + (r.detail || "连接正常") : "❌ " + (r.error || "测试失败");
    $(hintId).style.color = r.ok ? "" : "#ff9aa2";
  } catch (e) {
    $(hintId).textContent = "❌ " + e;
    $(hintId).style.color = "#ff9aa2";
  }
}

async function autoTestKeys() {
  // 只自动测两个必填的；Gemini 在「高级选项」里，进设置时测一次要白等一次超时
  await Promise.all(["mimo", "deepseek"].map(testOneKey));
}

function closeSettings() {
  // 光标还在某个框里就先让它失焦：不然刚敲进去的值不算「改过」，
  // 直接切到别的页面就会把它丢掉（change 只在失焦时才响）。
  const el = document.activeElement;
  if (el && el.blur && $("page-settings").contains(el)) el.blur();
  flushSettings();          // 输一半就切走也不丢：把还没落盘的改动立刻写掉
}

/** 浮层通用出口：点窗外空白处 / Esc（内容长的浮层尤其需要） */
function bindModalClose(maskId, closeFn) {
  const mask = $(maskId);
  mask.addEventListener("click", (e) => {
    if (e.target === mask) closeFn();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !mask.classList.contains("hidden")) closeFn();
  });
}

$("btn-settings").addEventListener("click", () => setPage("settings"));
$("btn-close").addEventListener("click", () => setPage("work"));
bindModalClose("qr-mask", () => $("qr-mask").classList.add("hidden"));

// Esc：在设置/自检页就退回工作页（赞赏码浮层开着时归它自己管）
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("qr-mask").classList.contains("hidden")) return;
  if (PAGE !== "work") setPage("work");
});

// ---------------- 设置：改了就存，不用点保存 ----------------
// 文本类只在「输完离开这个框」时存（change），勾选类一改就存——
// 每敲一个字符就写盘的话，会把写了一半的 API Key / 路径也存进去，
// 其中「引擎数据目录」存错一次就得重下好几个 GB。
// 攒 400ms 合并成一次写盘；关弹窗时再兜一次。
let SAVE_TIMER = null;
let SAVE_DIRTY = false;

function settingsPatch() {
  const patch = {
    finesub_exe: $("set-exe").value.trim() || "finesub",
    finesub_home: $("set-home").value.trim(),
    proxy: $("set-proxy").value.trim(),
    github_mirror: $("set-github-mirror").value.trim() || "auto",
    output_dir: $("set-out").value.trim(),
    video_context: $("set-vctx").checked,
    web_search: $("set-wsearch").checked,
    yt_subs: $("set-ytsubs").value,
    yt_auto_extra: $("set-ytauto").checked,
    tts_style: $("set-tts-style").value.trim(),
    tts_clone: $("set-tts-clone").checked,
    auto_update_downloader: $("set-auto-dl").checked,
    bg_opacity: (parseInt($("set-bgopacity").value, 10) || 0) / 100,
  };
  // 三类 key：填了才更新（留空 = 不改）
  for (const [name, elId] of [["mimo_key", "set-key-mimo"], ["deepseek_key", "set-key-deepseek"], ["gemini_key", "set-key-gemini"]]) {
    const v = $(elId).value.trim();
    if (v) patch[name] = v;
  }
  return patch;
}

function setSaveState(text, cls) {
  const el = $("save-state");
  el.textContent = text;
  el.className = "save-state" + (cls ? " " + cls : "");
}

async function flushSettings() {
  if (SAVE_TIMER) { clearTimeout(SAVE_TIMER); SAVE_TIMER = null; }
  if (!SAVE_DIRTY) return;
  SAVE_DIRTY = false;
  const patch = settingsPatch();
  try {
    const r = await pywebview.api.save_settings(patch);
    if (r && r.ok === false) {
      setSaveState("保存失败：" + (r.error || "未知原因"), "bad");
      SAVE_DIRTY = true;                    // 没写进去，下次改动/关窗时再试
      return;
    }
    setSaveState("已自动保存 ✓", "ok");
    if ("finesub_exe" in patch) refreshEngine();   // 换了引擎路径，状态点跟着刷新
  } catch (e) {
    setSaveState("保存失败：" + e, "bad");
    SAVE_DIRTY = true;
  }
}

function queueSave() {
  SAVE_DIRTY = true;
  if (SAVE_TIMER) clearTimeout(SAVE_TIMER);
  SAVE_TIMER = setTimeout(flushSettings, 400);
}

// 设置页里每一项：文本「输完离开」、勾选「一改」都会触发 change
document.querySelectorAll("#page-settings input, #page-settings select").forEach((el) => {
  el.addEventListener("change", queueSave);
  // 还在打字、没输完的：只记一笔「有未保存的改动」，**不**写盘
  // （每敲一个字符就存会把半截的 API Key / 路径也存进去）；关窗时由 flushSettings 兜住
  if (el.type !== "checkbox") el.addEventListener("input", () => { SAVE_DIRTY = true; });
});

// ---------------- 环境自检 ----------------
// 用户不该靠翻日志排错：把「缺什么、能不能跑」一次查清楚。
// 能自动修的（引擎数据目录 os error 448）后端在自检时就顺手修了，这里只负责讲结果。
let ENV_REPORT = null;

function envParams() {
  // 结合"他现在想干什么"：要用链接才提醒代理，要配音才要求 MiMo Key
  const single = MODE === "single";
  const raw = single
    ? (srcType() === "url" ? $("in-url").value : $("in-file").value)
    : (batchKind() === "folder" ? $("in-bfolder").value : $("in-burl").value);
  return {
    source: (raw || "").trim(),
    do_dub: single ? $("chk-dub").checked : $("chk-dub-b").checked,
  };
}

const ENV_ICONS = { ok: "✅", warn: "⚠️", bad: "❌" };

function renderEnvList(rep) {
  const sum = $("env-summary");
  sum.className = "env-summary " + (rep.state || "");
  sum.textContent = (ENV_ICONS[rep.state] || "") + " " + (rep.summary || "");
  $("env-list").innerHTML = (rep.items || [])
    .map((it) => `
      <div class="env-item ${it.state}">
        <span class="env-icon">${ENV_ICONS[it.state] || "•"}</span>
        <div class="env-body">
          <b>${esc(it.title)}</b>
          <div class="env-detail">${esc(it.detail)}</div>
        </div>
        ${it.fix ? `<button class="ghost env-fix" data-fix="${esc(it.fix)}">${esc(it.fix_label || "修复")}</button>` : ""}
      </div>`)
    .join("");
}

async function refreshEnv(deep = false) {
  let rep;
  try {
    rep = await pywebview.api.env_report(deep, envParams());
  } catch (e) {
    rep = { state: "bad", summary: "自检没能跑完（" + e + "）", items: [] };
  }
  ENV_REPORT = rep;
  $("env-dot").className = "env-dot " + (rep.state || "");
  // 导航上只有一个圆点，具体文字挂在 tooltip 上（悬停就能看到）
  $("btn-env").title = "环境自检 · " + (rep.state === "ok" ? "环境正常"
    : rep.state === "warn" ? "有提示" : "待处理");
  if (rep.tools) renderTools(rep.tools);   // 缺组件时顶上那条横幅照旧
  return rep;
}

function closeEnv() { setPage("work"); }

$("btn-env").addEventListener("click", async () => {
  setPage("env");
  $("env-summary").textContent = "检查中…";
  $("env-list").innerHTML = "";
  renderEnvList(await refreshEnv(true));   // 主动打开时带联网检查
});
$("btn-env-close").addEventListener("click", closeEnv);

$("btn-env-recheck").addEventListener("click", async () => {
  const btn = $("btn-env-recheck");
  btn.disabled = true;
  btn.textContent = "检测中…";
  try {
    renderEnvList(await refreshEnv(true));
  } finally {
    btn.disabled = false;
    btn.textContent = "重新检测";
  }
});

$("btn-env-diag").addEventListener("click", async () => {
  const r = await pywebview.api.export_diag();
  if (!r.ok) alert("导出失败：" + (r.error || "未知错误"));
});

$("env-list").addEventListener("click", (e) => {
  const btn = e.target.closest(".env-fix");
  if (!btn) return;
  const fix = btn.dataset.fix;
  if (fix === "engine") { closeEnv(); $("btn-install-engine").click(); }  // 复用已有的一键装引擎
  else if (fix === "settings") { closeEnv(); $("btn-settings").click(); }
  else if (fix === "downloader") { closeEnv(); $("btn-update-dl").click(); }
  else if (fix === "recheck") { $("btn-env-recheck").click(); }
});

// 赞赏码：点击放大
$("tip-qr").addEventListener("click", () => $("qr-mask").classList.remove("hidden"));
$("qr-mask").addEventListener("click", () => $("qr-mask").classList.add("hidden"));

// 联系作者：复制微信号
$("btn-copy-wx").addEventListener("click", async () => {
  const btn = $("btn-copy-wx");
  const r = await pywebview.api.copy_text($("wx-id").textContent.trim());
  if (r.ok) {
    btn.textContent = "已复制 ✓";
    setTimeout(() => (btn.textContent = "复制"), 1600);
  } else {
    alert("复制失败，请手动记下：" + $("wx-id").textContent.trim());
  }
});

// 诊断：复制日志 / 打开日志 / 导出报告 / 文档
// 复制的是**屏幕上这一份**（含界面里所有滚动出来的行），直接粘贴就能发给售后。
$("btn-copy-log").addEventListener("click", async () => {
  const btn = $("btn-copy-log");
  const old = btn.textContent;
  const text = ($("log").innerText || "").trim();
  if (!text) {
    btn.textContent = "还没有日志";
    setTimeout(() => (btn.textContent = old), 1600);
    return;
  }
  try {
    const r = await pywebview.api.copy_text(text);
    btn.textContent = r && r.ok ? "已复制 ✓" : "复制失败";
  } catch (e) {
    btn.textContent = "复制失败";
  }
  setTimeout(() => (btn.textContent = old), 1600);
});
$("btn-open-logs").addEventListener("click", () => pywebview.api.open_logs());
async function openDoc(which) {
  try {
    const r = await pywebview.api.open_doc(which);
    if (!r.ok) alert(r.error || "打开失败");
  } catch (e) {
    alert("打开失败：" + e);
  }
}
$("btn-open-terms").addEventListener("click", () => openDoc("terms"));
$("btn-open-licenses").addEventListener("click", () => openDoc("licenses"));
$("btn-export-diag").addEventListener("click", async () => {
  const r = await pywebview.api.export_diag();
  if (!r.ok) alert("导出失败：" + (r.error || "未知错误"));
});

// 前端报错也写进日志（方便排查界面问题）
window.addEventListener("error", (e) => {
  try { pywebview.api.log_js_error(`${e.message} @ ${e.filename}:${e.lineno}:${e.colno}`); } catch (_) {}
});
window.addEventListener("unhandledrejection", (e) => {
  try { pywebview.api.log_js_error(`Promise 未处理: ${e.reason}`); } catch (_) {}
});

// 启动
window.addEventListener("pywebviewready", boot);
