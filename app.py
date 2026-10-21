#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""星译 — 应用入口（pywebview：Python 后端 + 本地 HTML 界面）

注意：pywebview 会递归遍历 js_api 对象的所有「非下划线开头」属性来暴露方法，
所以 Api 的内部状态必须全部用下划线命名（否则会走进 Window/.NET 对象导致卡死）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

import settings as cfg
import diag
import engine
import subtitle_preview
import topics
import tools
import wallpaper_poster
from job import Job, Cancelled, guard as guard_processes, no_window_kwargs
from pipeline import run_pipeline
from pipeline import probe_info as pipeline_probe_info

APP_VERSION = "6.16"


def _base_dir() -> Path:
    """资源目录：打包后（PyInstaller）在解包目录，开发时在脚本目录"""
    if tools.is_packaged():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


BASE_DIR = _base_dir()
WEB_DIR = BASE_DIR / "web"
ASSETS_DIR = BASE_DIR / "assets"
APP_ICON_ICO = ASSETS_DIR / "app.ico"
APP_NAME = "星译"
# 窗口标题带上版本号：客户反馈问题时截图就能看出装的是哪一版
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
_DIALOG_OPEN = getattr(webview, "FileDialog", None)
_DIALOG_OPEN = _DIALOG_OPEN.OPEN if _DIALOG_OPEN else webview.OPEN_DIALOG
_DIALOG_DIR = getattr(webview, "FileDialog", None)
_DIALOG_DIR = _DIALOG_DIR.FOLDER if _DIALOG_DIR else webview.FOLDER_DIALOG

VIDEO_TYPES = ("视频文件 (*.mp4;*.mov;*.mkv;*.webm;*.avi)", "所有文件 (*.*)")

#: 从流水线日志里认出「输出目录」和语音识别产出的原始字幕。
#: 引擎会在第 4 阶段结束时打一行「完成：<路径>-raw.srt」，这时纠错翻译还没开始，
#: 正好是用户检查识别文本的窗口。见 Api._note_subtitle_paths。
_RE_OUT_DIR = re.compile(r"输出目录:\s*(.+?)\s*$")
_RE_RAW_SRT = re.compile(r"([A-Za-z]:\\[^\r\n]*?-raw\.srt)", re.I)


class Api:
    """暴露给前端的接口（JS: pywebview.api.<方法>）

    公开方法 = JS 可调用；内部属性一律 _ 开头（pywebview 会跳过下划线属性）。
    """

    def __init__(self):
        self._window: webview.Window | None = None
        self._job: Job | None = None
        self._thread: threading.Thread | None = None
        self._installing = False
        self._lock = threading.Lock()
        self._status = {"running": False, "stage": "", "percent": 0, "log": [], "result": None,
                        "error": "", "out_dir": "", "subtitle_raw": "", "subtitle_final": ""}
        self._last_result: dict | None = None     # 最近一次成功任务的结果（重新压制要用）
        self._tools = tools.self_check()   # 随包组件自检（ffmpeg / ffprobe / yt-dlp）
        self._update_info: dict | None = None

    def attach_window(self, window):
        self._window = window

    def _on_closing(self):
        """窗口关闭：先把正在跑的引擎进程树收干净，再放行。

        关窗口是最常见的"中断"方式，而引擎是**子孙成群**的（finesub.exe → uv.exe）。
        只让主进程退出的话，孙子会活下来继续下载、继续握着引擎数据目录里的文件锁，
        下一次开工干等 300 秒锁超时然后失败（2026-10-08 客户一上午栽两次）。

        `job.guard()` 从系统层面已经能兜住（本进程一消失就清掉整棵树），
        这里再显式做一遍是为了**确定性**：正常关闭时立刻清理，而不是等进程真正结束。
        """
        try:
            if self._job:
                self._job.cancel_now()
        except Exception:  # noqa: BLE001 - 关窗口绝不能被清理逻辑卡住
            pass

    # ---------------- 前端调用 ----------------

    def boot(self) -> dict:
        """前端启动时拉初始状态"""
        c = cfg.load()
        # 老版本的 dub_cache / wheels / subtitle_preview 散在 %APPDATA% 下，
        # 启动时顺手并进「数据目录」（同盘是改名，瞬时；搬不动就留在原处）。
        try:
            import paths
            moved = paths.migrate_legacy()
            if moved:
                diag.write("[data] 已归并旧缓存：" + "；".join(moved), "app")
        except Exception as e:  # noqa: BLE001 - 归并失败不影响启动
            diag.write(f"[data] 归并旧缓存失败: {type(e).__name__}: {e}", "app")

        def keyinfo(name: str) -> dict:
            v = c.get(name) or ""
            return {"ready": bool(v), "tail": cfg.mask_key(v)}

        pub = {k: v for k, v in c.items() if k not in ("mimo_key", "deepseek_key", "gemini_key")}
        pub["keys"] = {"mimo": keyinfo("mimo_key"),
                       "deepseek": keyinfo("deepseek_key"),
                       "gemini": keyinfo("gemini_key")}
        # 明文 key：本机界面展示/核对用（页面是本地文件，不出网）
        pub["keys_plain"] = {"mimo": c.get("mimo_key", ""),
                             "deepseek": c.get("deepseek_key", ""),
                             "gemini": c.get("gemini_key", "")}
        # 实际数据目录（给设置页显示「现在装在哪 / 默认装在哪」）
        try:
            import paths
            pub["data_root"] = str(paths.root())
            pub["data_default_root"] = str(paths.default_root())
        except Exception:  # noqa: BLE001 - 读不到就不显示，不影响启动
            pass
        # 引擎数据目录同理：空设置时实际会用哪个位置
        try:
            import datadir
            default_engine = datadir.default_home()
            pub["finesub_home_default"] = str(default_engine)
            pub["finesub_home_actual"] = ((c.get("finesub_home") or "").strip()
                                          or str(default_engine))
        except Exception:  # noqa: BLE001
            pass
        from pipeline import _default_output_dir
        pub["default_output_dir"] = str(_default_output_dir())
        return {"settings": pub, "voices": cfg.VOICES, "langs": cfg.LANGS,
                "links": cfg.APPLY_LINKS, "app_version": APP_VERSION,
                "tools": self.env_check(), "status": self.get_status()}

    def env_check(self) -> dict:
        """重新自检随包组件（界面上的「重新检测」按钮 / 设置里改完路径后调用）"""
        tools.refresh()
        self._tools = tools.self_check()
        return self._tools

    def env_report(self, deep: bool = False, params: dict | None = None) -> dict:
        """环境自检：启动时快检（不联网），用户点「环境自检」时再带一次联网检查

        能自动修的后端在自检里顺手修掉（例如引擎数据目录 os error 448），
        用户看到的只是"已自动改用 X"。
        """
        import doctor
        try:
            rep = doctor.check(params or {}, bool(deep))
        except Exception as e:  # noqa: BLE001 - 自检本身不该把界面搞崩
            diag.write_exception(e, "环境自检")
            rep = {"state": "bad", "summary": "自检没能跑完", "bad": 1, "warn": 0,
                   "items": [{"id": "self", "title": "自检", "state": "bad",
                              "detail": f"{type(e).__name__}: {e}",
                              "fix": "", "fix_label": ""}]}
        tools.refresh()
        self._tools = tools.self_check()
        rep["tools"] = self._tools
        return rep

    def env_fix(self, kind: str) -> dict:
        """自检里的一键修复（目前只有装引擎和更新下载器是后端动作，其余交给前端打开设置/重新检测）"""
        if kind == "engine":
            return self.engine_install()
        if kind == "downloader":
            return self.update_downloader()
        return {"ok": False, "error": "未知的修复项"}

    def open_url(self, url: str) -> dict:
        """用系统默认浏览器打开链接（申请 key 用）"""
        if isinstance(url, str) and url.startswith("http"):
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False, "error": "无效链接"}

    # ---------------- 诊断 ----------------

    def open_logs(self) -> dict:
        """打开日志目录"""
        diag.LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(diag.LOG_DIR))  # noqa: S606
        return {"ok": True, "path": str(diag.LOG_DIR)}

    def export_diag(self) -> dict:
        """导出诊断报告（环境信息 + 日志，不含 key），并打开所在目录"""
        try:
            p = diag.export_report()
            subprocess.Popen(["explorer", "/select,", str(p)], **no_window_kwargs())
            diag.write(f"诊断报告已导出: {p}", "app")
            return {"ok": True, "path": str(p)}
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "导出诊断报告")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def open_doc(self, which: str) -> dict:
        """打开随软件附带的文档（用户协议 / 开源许可）"""
        docs = {"terms": "TERMS.md", "licenses": "THIRD_PARTY_LICENSES.md"}
        name = docs.get(str(which))
        if not name:
            return {"ok": False, "error": "未知文档"}
        p = BASE_DIR / name
        # ⚠️ 每种布局都试一遍（2026-10-16 修「点不开」）：Nuitka 生产版里
        #    后端 exe 在 `{app}\backend\`，而文档由 build.py 放在**应用根目录**
        #    （`{app}\THIRD_PARTY_LICENSES.md`）—— 原来的三处探测一个都够不着，
        #    点「感谢」只会静默失败。开发版则是 `_internal\` 那一层。
        for cand in (
            BASE_DIR / name,                              # 开发：脚本目录；PyInstaller：_MEIPASS
            Path(sys.executable).parent / name,           # 后端目录兜底
            Path(sys.executable).parent / "_internal" / name,
            Path(sys.executable).parent.parent / name,    # ★ 安装版：backen\ 的上一级 = 应用根
            Path(sys.executable).parent.parent / "_internal" / name,
        ):
            if cand.exists():
                p = cand
                break
        else:
            diag.write(f"打开文档失败（找不到文件）: {name}", "app")
            return {"ok": False, "error": f"未找到 {name}（可在软件目录里手动打开）"}
        try:
            os.startfile(str(p))  # noqa: S606
            diag.write(f"已打开文档: {p}", "app")
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - 没有 .md 关联程序等
            try:
                subprocess.Popen(["notepad.exe", str(p)], **no_window_kwargs())
                return {"ok": True}
            except Exception:
                diag.write_exception(e, f"打开文档 {name}")
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------------- 检查更新 ----------------

    def update_state(self) -> dict:
        """返回启动时自动检查的结果（前端据此显示提示条）"""
        return dict(self._update_info or {"ok": False, "has_update": False})

    def auto_check_update(self):
        """启动时后台静默检查（不阻塞界面）"""
        def run():
            try:
                self._update_info = self.check_update()
            except Exception as e:  # noqa: BLE001
                diag.write(f"自动检查更新失败: {e}", "update")
                self._update_info = {"ok": False, "has_update": False}
        threading.Thread(target=run, daemon=True).start()

    def check_update(self) -> dict:
        """检查是否有新版本"""
        import updater
        c = cfg.load()
        return updater.check(APP_VERSION, c.get("update_url", ""), c.get("proxy", ""))

    def do_update(self, pkg_url: str = "", sha256: str = "") -> dict:
        """下载并安装新版本（下载在后台线程，完成后退出并静默安装）"""
        import updater
        if not (pkg_url or "").startswith("http"):
            return {"ok": False, "error": "更新地址无效"}
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务正在运行，请等它完成后再更新"}
            self._job = Job()
            self._status = {"running": True, "stage": "update", "percent": 0,
                            "log": ["开始更新…"], "result": None, "error": ""}
        threading.Thread(target=self._do_update, args=(pkg_url, sha256), daemon=True).start()
        return {"ok": True}

    def _do_update(self, pkg_url: str, sha256: str = ""):
        import updater
        try:
            c = cfg.load()
            pkg = updater.download(pkg_url, c.get("proxy", ""), self._job, self._report, sha256)
            self._report("update", 100, "下载完成，正在安装并重启…")
            updater.install_and_restart(pkg, restart=True)
            time.sleep(1.0)
            os._exit(0)          # 立刻退出，让安装程序接替（避免文件占用）
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 已取消更新")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            diag.write_exception(e, "应用更新")
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ 更新失败：{msg}")

    def log_js_error(self, message: str) -> dict:
        """前端 JS 报错也记入日志"""
        diag.write(f"前端错误: {message}", "js")
        return {"ok": True}

    # ---------------- 下载器（yt-dlp）----------------

    def downloader_state(self) -> dict:
        """下载器当前装的是哪一版、走的哪个通道（设置页展示）"""
        import ytdlp
        try:
            return {"ok": True, **ytdlp.state(cfg.load())}
        except Exception as e:  # noqa: BLE001 - 读不到不该把设置页搞崩
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def update_downloader(self) -> dict:
        """手动更新下载器（后台跑，进度写进主日志区）"""
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务正在运行，请等它完成后再更新"}
            self._job = Job()
            self._status = {"running": True, "stage": "ytdlp", "percent": 0,
                            "log": ["开始检查下载器更新…"], "result": None, "error": ""}
        threading.Thread(target=self._update_downloader, daemon=True).start()
        return {"ok": True}

    def _update_downloader(self):
        import ytdlp
        try:
            r = ytdlp.update(cfg.load(), self._report, job=self._job)
            if not r.get("ok"):
                raise RuntimeError(r.get("error") or "更新失败")
            done = (f"✅ 下载器已更新到 {r['version']}（{r['channel']}）" if r.get("changed")
                    else f"✅ 下载器已是最新版本（{r.get('version') or '未知'}）")
            with self._lock:
                self._status.update(running=False, stage="done", percent=100)
            self._report("done", 100, done)
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 已取消")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            diag.write_exception(e, "下载器更新")
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ 下载器更新失败：{msg}")

    def auto_update_downloader(self):
        """启动时后台静默把下载器（yt-dlp）更新到最新

        YouTube 的反爬是按周变的、软件是月更，中间这段只能让软件自己把下载器跟上
        （见 ytdlp.py）。**全程静默**：成功失败都只写日志文件，不占用界面的日志区
        ——用户还没点「开始翻译」，那块突然刷字很奇怪；要看的可以去设置里点「立即更新」。
        """
        def run():
            try:
                import ytdlp
                with self._lock:
                    if self._status["running"]:
                        return                   # 有活儿在跑就别动它正在用的 exe
                ytdlp.auto(cfg.load())
            except Exception as e:  # noqa: BLE001
                diag.write(f"下载器自动更新失败: {type(e).__name__}: {e}", "ytdlp")
        threading.Thread(target=run, daemon=True).start()

    def copy_text(self, text: str) -> dict:
        """复制文本到系统剪贴板（Windows clip）

        这里不能用 no_window_kwargs：它会把 stdin 设成 DEVNULL，而 input= 自己就要占 stdin。
        """
        try:
            from job import CREATE_NO_WINDOW
            subprocess.run("clip", input=str(text), text=True, shell=True, check=True,
                           creationflags=CREATE_NO_WINDOW)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------------- 密钥连通性测试 ----------------

    def test_key(self, kind: str, key: str = "") -> dict:
        """测试某个服务的 key 是否可用（优先用传入的，否则用已保存的）"""
        import urllib.error
        import mirrors
        import urllib.request
        c = cfg.load()
        # 有可用代理就走，没有就强制直连（别让 urllib 自己去读已经关掉的系统代理）
        opener = mirrors.opener(c)
        try:
            if kind == "mimo":
                k = (key or cfg.resolve_mimo_key(c) or "").strip()
                req = urllib.request.Request("https://api.xiaomimimo.com/v1/models",
                                             headers={"api-key": k})
                name = "MiMo"
            elif kind == "deepseek":
                k = (key or c.get("deepseek_key") or "").strip()
                req = urllib.request.Request("https://api.deepseek.com/models",
                                             headers={"Authorization": f"Bearer {k}"})
                name = "DeepSeek"
            elif kind == "gemini":
                k = (key or c.get("gemini_key") or "").strip()
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={k}")
                name = "Gemini"
            else:
                return {"ok": False, "error": "未知服务"}
            if not k:
                return {"ok": False, "error": "未配置 key"}
            with opener.open(req, timeout=25) as r:
                return {"ok": True, "detail": f"{name} 连接正常"}
        except urllib.error.HTTPError as e:
            # 给用户看的只有人话，状态码只写进日志（小白看不懂 401/429）
            hint = {400: "密钥格式不对，检查是不是没复制全",
                    401: "密钥无效，检查是不是复制错了",
                    403: "这个密钥没有权限",
                    429: "额度用完或请求太频繁"}.get(e.code, f"测试失败，服务返回 {e.code}")
            diag.write(f"key 测试失败（{kind}）: {hint}", "app")
            return {"ok": False, "error": hint}
        except Exception as e:  # noqa: BLE001
            diag.write(f"key 测试异常（{kind}）: {type(e).__name__}: {e}", "app")
            return {"ok": False, "error": "连不上，检查一下网络或代理设置"}

    def deepseek_balance(self, key: str = "", timeout: float = 15.0) -> dict:
        """查询 DeepSeek 账户余额（GET /user/balance），不记录、不返回 Key"""
        import json
        import urllib.error
        import urllib.request
        import mirrors
        c = cfg.load()
        k = (key or c.get("deepseek_key") or "").strip()
        if not k:
            return {"ok": False, "error": "未配置 DeepSeek key"}
        opener = mirrors.opener(c)
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {k}", "Accept": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            hint = {401: "密钥无效", 403: "这个密钥没有权限",
                    429: "请求太频繁"}.get(e.code, f"服务返回 {e.code}")
            diag.write(f"查询 DeepSeek 额度失败: {hint}", "app")
            return {"ok": False, "error": hint}
        except Exception as e:  # noqa: BLE001
            diag.write(f"查询 DeepSeek 额度异常: {type(e).__name__}: {e}", "app")
            return {"ok": False, "error": "连不上，检查网络或代理"}

        infos = data.get("balance_infos") or []
        if not infos:
            return {"ok": False, "error": "接口没有返回余额"}
        info = next((i for i in infos
                     if str(i.get("currency") or "").upper() == "CNY"), infos[0])

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        return {"ok": True, "is_available": bool(data.get("is_available")),
                "currency": str(info.get("currency") or ""),
                "total": _num(info.get("total_balance")),
                "granted": _num(info.get("granted_balance")),
                "topped_up": _num(info.get("topped_up_balance"))}

    # ---------------- 音色试听 ----------------

    def preview_voice(self, voice: str) -> dict:
        """合成一句样音供试听（同一音色 + 同一风格缓存复用），返回可播放的相对文件名"""
        import hashlib

        voice = (voice or "").strip() or "冰糖"
        key = cfg.resolve_mimo_key()
        if not key:
            return {"ok": False, "error": "未配置 MiMo API key（去设置里填写）"}
        import dubber

        # 试听要跟成片一个念法：把「配音怎么读」那条风格也用上，改了风格就换一份缓存
        style = (cfg.load().get("tts_style") or "").strip() or dubber.DEFAULT_STYLE
        tag = hashlib.sha1(style.encode("utf-8")).hexdigest()[:6]
        safe = "".join(ch for ch in voice if ch.isalnum() or ch in "_-") or "voice"
        out = WEB_DIR / f"voice_{safe}_{tag}.wav"
        if not (out.exists() and out.stat().st_size > 500):
            text = f"大家好，这是「{voice}」音色的试听效果，欢迎使用星译。"
            err = dubber.synth_mimo(text, out, key, voice, style=style)
            if err:
                diag.write(f"试听合成失败（{voice}）: {err}", "app")
                return {"ok": False, "error": f"合成失败：{err}"}
            diag.write(f"试听样音已生成: {out.name}（{voice}）", "app")
        return {"ok": True, "file": out.name}

    # ---------------- 字幕样式预览 ----------------

    def font_info(self, path: str = "") -> dict:
        """读字体文件的族名（设置页选了自定义字体后显示用）。

        族名读不出来也算成功（部分字体 name 表不规整），退回文件名显示。
        """
        import subtitle_fonts
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "family": "", "error": "找不到这个文件"}
        fam = subtitle_fonts.family_of(p) or p.stem
        return {"ok": True, "family": fam, "file": str(p)}

    def reset_subtitle_style(self) -> dict:
        """「字幕样式」整组恢复默认（用户 2026-10-21 要求能重置）。"""
        keys = ("subtitle_font", "subtitle_font_file", "subtitle_size", "subtitle_spacing",
                "subtitle_outline", "subtitle_outline_color", "subtitle_color",
                "subtitle_margin_v", "subtitle_bilingual", "subtitle_bilingual_order",
                "subtitle_bilingual_scale")
        try:
            cfg.save({k: cfg.DEFAULTS[k] for k in keys})
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "恢复默认字幕样式")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        diag.write("字幕样式已恢复默认", "app")
        return {"ok": True}

    def subtitle_preview(self, font: str = "", size: float = 42, outline: float = 2.2,
                         margin_v: int = 30, spacing: float = 0,
                         color: str = "#FFFFFF", font_file: str = "",
                         large: bool = False,
                         bilingual: bool = False, src_order: str = "src_top",
                         src_scale: float = 80, outline_color: str = "") -> dict:
        """用当前样式渲一帧预览图（和烧录成片走同一条 srt2ass + libass 路径）"""
        try:
            path = subtitle_preview.render({
                "font": font,
                "size": size,
                "outline": outline,
                "margin_v": margin_v,
                "spacing": spacing,
                "color": color,
                "outline_color": outline_color,
                # 设置里配了字体文件就用它，否则交给随包的思源黑体
                "font_file": font_file or cfg.load().get("subtitle_font_file") or "",
                "bilingual": bilingual,
                "src_order": src_order,
                "src_scale": src_scale,
            }, large=large)
            return {"ok": True, "file": str(path)}
        except Exception as e:  # noqa: BLE001 - 预览失败不能影响设置页
            diag.write(f"字幕预览渲染失败: {type(e).__name__}: {e}", "app")
            return {"ok": False, "error": "预览生成失败"}

    # ---------------- 视频壁纸静态遮罩 ----------------

    def wallpaper_poster(self, path: str = "", at: float = 1.0) -> dict:
        """从视频壁纸抽一帧，窗口缩放时盖住视频表面，避免绿帧/黑帧/星空底图"""
        try:
            out = wallpaper_poster.render(path, at)
            return {"ok": True, "file": str(out)}
        except Exception as e:  # noqa: BLE001 - 抽帧失败不能影响壁纸播放
            diag.write(f"壁纸遮罩抽帧失败: {type(e).__name__}: {e}", "app")
            return {"ok": False, "error": "壁纸遮罩生成失败"}

    def detect_proxy(self, proxy: str = "") -> dict:
        """设置页「检测代理」：填的地址通不通？不通就自动找系统代理 / 本机常见端口。

        返回 {ok, proxy, source, saved}：
          ok     = 找到了一个真能连上的代理
          proxy  = 找到的地址（可以直接填进设置）
          source = configured（你填的就能用）/ system（Windows 系统代理）/
                   port:7890（本机常见端口）/ none / configured-dead
          saved  = 是不是已经顺手写进设置

        找到且和原来不一样就**自动保存**：用户点这个按钮就是要「帮我把代理弄好」，
        存下来界面才看得见。没存也不影响干活 —— 运行期本来就会自动兜底
        （见 mirrors.effective_proxy）。
        """
        import mirrors
        want = (proxy or "").strip()
        params = dict(cfg.load())
        # 用户可能刚改了框里的值还没失焦保存：以框里的为准
        if want != (params.get("proxy") or "").strip():
            params["proxy"] = want
        r = mirrors.detect_proxy(params)
        saved = False
        if r.get("ok") and r.get("proxy") != (cfg.load().get("proxy") or "").strip():
            cfg.save({"proxy": r["proxy"]})
            saved = True
        mirrors.forget_proxy_cache()
        diag.write(f"检测代理：{r.get('proxy') or '没有可用代理'}（{r.get('source')}）"
                   f"{'，已写入设置' if saved else ''}", "proxy")
        return {**r, "saved": saved}

    def save_settings(self, patch: dict) -> dict:
        """写入设置（界面每次改动都会调，写不进去要能原样告诉用户）"""
        try:
            cfg.save(patch or {})
            if patch and "data_home" in patch:
                import paths
                paths.reset_default()
            if patch and "proxy" in patch:
                import mirrors
                mirrors.forget_proxy_cache()     # 换了代理立刻重判，别再用旧的探测结果
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "保存设置")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}

    def move_home(self, new_dir: str) -> dict:
        """把引擎数据目录**整个搬到** new_dir（换盘不用重下）。

        返回 {ok, moved_to, reason, fallback}：ok=False 时**原处一个字没动**，
        界面据此决定是"就这样"还是退回「在新位置重新准备（要重下几个 GB）」。
        跨盘复制要几分钟，所以前端那边记得给足超时；这里绝不抛（见 relocate.py）。
        """
        try:
            import relocate
            job = Job()
            return relocate.move_home(cfg.load(), str(new_dir), job, self._report)
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "搬引擎数据目录")
            return {"ok": False, "reason": f"{type(e).__name__}: {e}", "fallback": True}

    def move_data_home(self, new_dir: str) -> dict:
        """把「数据目录」整个搬到 new_dir：语音克隆 + 配音缓存 + 缩略图 + wheel 缓存。

        和 move_home 一样是**先搬**（同盘瞬时、跨盘复制几分钟），搬完才改设置；
        返回 {ok, moved_to, moved, reason}，ok=False 时原处一个字没动。
        """
        try:
            import paths
            with self._lock:
                if self._status.get("running") or self._installing:
                    return {"ok": False, "reason": "有任务正在运行，等它完成后再换目录",
                            "moved_to": "", "moved": []}
            job = Job()
            return paths.move_root(str(new_dir), job, self._report)
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "搬数据目录")
            return {"ok": False, "reason": f"{type(e).__name__}: {e}",
                    "moved_to": "", "moved": []}

    # ---------------- 术语库（本机词条，界面「查看术语库」那个窗口用）----------------

    def knowledge_list(self) -> dict:
        """列本机术语库（按域分组）。内部先跑一次 engine 的 refresh，约 0.7s。"""
        try:
            import knowledge
            return knowledge.listing(cfg.load())
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "列术语库")
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "domains": [],
                    "total": 0, "learned": 0, "builtin": 0, "path": ""}

    def knowledge_clear(self) -> dict:
        """清空术语库并重新导入内置词条（回到出厂状态）。理由见 knowledge.py 开头。"""
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务正在运行，请稍后再试"}
            try:
                import knowledge
                r = knowledge.clear(cfg.load(), Job())
                diag.write(f"[kb] 清空：删除 {r.get('removed')} 条，恢复内置 {r.get('restored')} 条", "run")
                return r
            except Exception as e:  # noqa: BLE001
                diag.write_exception(e, "清空术语库")
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------------- 翻译引擎（finesub）----------------

    def engine_status(self) -> dict:
        """检测翻译引擎是否就绪"""
        return engine.status(cfg.load().get("finesub_exe", ""))

    def engine_install(self) -> dict:
        """一键安装翻译引擎（uv + finesub）"""
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务正在运行，请稍后再试"}
            if self._installing:
                return {"ok": False, "error": "安装已在进行中"}
            self._installing = True
            params = cfg.load()
            job = Job()
            self._job = job
            self._status = {"running": True, "stage": "engine", "percent": 0,
                            "log": ["开始安装翻译引擎…"], "result": None, "error": ""}
        threading.Thread(target=self._install_engine, args=(params, job), daemon=True).start()
        return {"ok": True}

    def _install_engine(self, params: dict, job: Job):
        try:
            path = engine.install_finesub(params, job, self._report)
            cfg.save({"finesub_exe": path})
            with self._lock:
                self._status.update(running=False, stage="done", percent=100)
            self._report("done", 100, "🎉 翻译引擎安装完成，可以开始翻译了")
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 安装已取消")
        except Exception as e:  # noqa: BLE001
            # ⚠️ EngineError 的消息**本身就是人话**（比如「磁盘空间不够…」），
            #    再拼个 `RuntimeError: ` 上去只会让用户以为出了什么程序故障。
            msg = f"{e}" if isinstance(e, engine.EngineError) else f"{type(e).__name__}: {e}"
            diag.write_exception(e, "引擎安装", detail=str(getattr(e, "detail", "") or ""))
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ {msg}（详细日志已写入 logs 目录）")
        finally:
            self._installing = False

    # ---------------- 本地语音克隆（备用音色，按需下载） ----------------

    def clone_status(self) -> dict:
        """本地语音克隆装到哪一步了 + 现在用的是哪个引擎"""
        import localclone
        st = localclone.status()
        st["engine"] = "local" if cfg.load().get("clone_engine") == "local" else "cloud"
        return st

    def clone_install(self) -> dict:
        """下载并安装本地语音克隆（约 7 GB：环境 + 模型），进度走状态推送"""
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务正在运行，等它完成后再装"}
            if self._installing:
                return {"ok": False, "error": "已经有一个安装在跑了"}
            self._installing = True
            params = cfg.load()
            job = Job()
            self._job = job
            self._status = {"running": True, "stage": "clone", "percent": 0,
                            "log": ["开始安装本地语音克隆（IndexTTS-2.5）…"],
                            "result": None, "error": ""}
        threading.Thread(target=self._install_clone, args=(params, job), daemon=True).start()
        return {"ok": True}

    def _install_clone(self, params: dict, job: Job):
        import localclone
        try:
            st = localclone.install(params, job, self._report)
            with self._lock:
                self._status.update(running=False, stage="done", percent=100, result=st)
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 已取消（已经下好的部分留着，下次接着装）")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            diag.write_exception(e, "本地语音克隆安装")
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ {msg}（详细日志见 logs 目录）")
        finally:
            self._installing = False

    def clone_uninstall(self) -> dict:
        """把本地语音克隆删掉（腾空间用；云端克隆不受影响）"""
        import shutil
        import localclone
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "有任务在跑，先等它完成"}
        try:
            shutil.rmtree(localclone.home(), ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # 删了就别再指着它干活
        if cfg.load().get("clone_engine") == "local":
            cfg.save({"clone_engine": "cloud", "tts_clone": False})
        diag.write("本地语音克隆已删除", "app")
        return {"ok": True, **localclone.status()}

    def pick_video(self) -> str:
        r = self._window.create_file_dialog(_DIALOG_OPEN, file_types=VIDEO_TYPES)
        return r[0] if r else ""

    def pick_dir(self) -> str:
        r = self._window.create_file_dialog(_DIALOG_DIR)
        return r[0] if r else ""

    def probe(self, source: str) -> dict:
        """按标题猜「背景信息」：链接取一次标题（不下载），本地文件用文件名

        界面在用户填完视频/链接后调这个，把结果填进「背景信息」输入框（用户还能改）。
        认出是哪款软件还有个副作用：知识库里的对应术语条目会被模型挑中，
        专有名词的正确拼写和官方中文定名跟着一起进提示词。
        """
        import re as _re

        src = (source or "").strip()
        if not src:
            return {"ok": False, "error": "没填来源"}
        thumb = ""
        info: dict = {}
        if _re.match(r"^https?://", src, _re.I):
            info = pipeline_probe_info(src, cfg.load())
            title = info.get("title", "")
            thumb = info.get("thumb", "")
            if not title:
                return {"ok": False, "error": "取不到标题（链接或网络问题）"}
        else:
            title = Path(src).stem
        apps = [zh for zh, _en in topics.guess(title)]
        return {"ok": True, "title": title, "extra": topics.build_extra(title),
                "apps": apps, "thumb": thumb,
                "uploader": info.get("uploader", ""), "duration": info.get("duration", "")}

    def probe_titles(self, titles: list) -> dict:
        """一批标题（合集的）→ 背景信息：按命中条数挑软件，比只看第一条稳"""
        titles = [str(t) for t in (titles or []) if str(t).strip()]
        if not titles:
            return {"ok": False, "error": "没有标题"}
        apps = [zh for zh, _en in topics.guess_many(titles)]
        return {"ok": True, "extra": topics.build_extra_many(titles), "apps": apps}

    def start(self, params: dict) -> dict:
        note = ""
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "已有任务在运行中"}
            merged = cfg.load()
            # 单个视频可以带一份用户导入的字幕/文稿；它只对这一次任务有效，
            # 不写进设置（批量模式也不接收，一条字幕对应多个视频没有意义）。
            merged.update({k: v for k, v in (params or {}).items()
                           if k in cfg.DEFAULTS or k == "subtitle_file"})
            key = cfg.resolve_mimo_key(merged)
            if merged.get("do_dub", True) and not key:
                return {"ok": False, "error": "未配置 MiMo API key（配音用）—— 点右上角「设置」，旁边有申请入口"}
            if not (merged.get("deepseek_key") or merged.get("gemini_key")):
                note = "提示：未配置 DeepSeek/Gemini key，将使用本机已有的翻译服务配置"
            if not (merged.get("source") or "").strip():
                return {"ok": False, "error": "请先选择视频文件或填写视频链接"}
            merged["mimo_key"] = key
            self._job = Job()
            self._last_result = None
            self._status = {"running": True, "stage": "download", "percent": 0,
                            "log": [note] if note else [], "result": None, "error": "",
                            "out_dir": "", "subtitle_raw": "", "subtitle_final": ""}
        if note:
            diag.write(f"[translate] {note}", "run")
        self._thread = threading.Thread(target=self._run, args=(merged,), daemon=True)
        self._thread.start()
        return {"ok": True}

    def cancel(self) -> dict:
        if self._job:
            self._job.cancel_now()
        return {"ok": True}

    # ---------------- 批量模式 ----------------

    def video_thumb(self, path: str) -> dict:
        """取一格缩略图（有封面用封面，没有抽第一帧）。界面按行异步来要，抽不出就返回失败。"""
        import thumbs
        try:
            f = thumbs.get(path)
        except Exception as e:  # noqa: BLE001 - 缩略图失败不影响任何功能
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": bool(f), "file": str(f) if f else ""}

    def url_thumbs(self, urls: list) -> dict:
        """把网络封面下到本地缓存，返回 {原地址: 本地路径}

        界面的 BitmapImage 是系统自己下图的，**不走设置里的代理** —— YouTube 的封面
        在国内根本显示不出来。走后端（带代理）拿下来，界面读本地文件。
        """
        import thumbs
        try:
            files = thumbs.fetch_many(urls or [], cfg.load())
        except Exception as e:  # noqa: BLE001 - 封面失败不影响任何功能
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "files": files}

    def scan_source(self, params: dict) -> dict:
        """预览批量条目（文件夹：立即返回；合集链接：yt-dlp 读取，需几秒）"""
        merged = cfg.load()
        merged.update({k: v for k, v in (params or {}).items() if k in cfg.DEFAULTS})
        src = (merged.get("source") or "").strip()
        if not src:
            return {"ok": False, "error": "请先选择文件夹或填写合集链接"}
        try:
            from pipeline import list_folder_videos, list_playlist
            if merged.get("batch_kind") == "playlist":
                items = list_playlist(src, merged, Job(), lambda *a: None)
                # 合集自己的名字和作者（flat 模式下每一集拿不到作者，合集这一层是有的）
                coll = ({"title": items[0].get("coll", ""), "by": items[0].get("coll_by", "")}
                        if items else {})
                return {"ok": True, "items": items, "coll": coll}
            else:
                items = list_folder_videos(Path(src))
            return {"ok": True, "items": items}
        except Exception as e:  # noqa: BLE001
            diag.write_exception(e, "扫描批量条目")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def start_batch(self, params: dict) -> dict:
        with self._lock:
            if self._status["running"]:
                return {"ok": False, "error": "已有任务在运行中"}
            merged = cfg.load()
            merged.update({k: v for k, v in (params or {}).items() if k in cfg.DEFAULTS})
            key = cfg.resolve_mimo_key(merged)
            if merged.get("do_dub", True) and not key:
                return {"ok": False, "error": "未配置 MiMo API key（配音用）—— 点右上角「设置」"}
            if not (merged.get("source") or "").strip():
                return {"ok": False, "error": "请先选择文件夹或填写合集链接"}
            merged["mimo_key"] = key
            self._job = Job()
            self._last_result = None
            self._status = {"running": True, "stage": "download", "percent": 0,
                            "log": [], "result": None, "error": "", "queue": [],
                            "out_dir": "", "subtitle_raw": "", "subtitle_final": ""}
        self._thread = threading.Thread(target=self._run_batch, args=(merged,), daemon=True)
        self._thread.start()
        return {"ok": True}

    def _run_batch(self, params: dict):
        def queue_cb(idx: int, patch: dict):
            with self._lock:
                q = self._status.setdefault("queue", [])
                while len(q) <= idx:
                    q.append({"title": "", "status": "pending", "detail": ""})
                q[idx].update(patch)

        try:
            from pipeline import run_batch
            result = run_batch(params, self._job, self._report, queue_cb)
            with self._lock:
                self._status.update(running=False, stage="done", percent=100, result=result)
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 批量任务已取消（已完成的不受影响）")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            diag.write_exception(e, "批量任务")
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ {msg}（详细日志见 logs 目录）")

    def get_status(self) -> dict:
        with self._lock:
            job = self._job
            if job is not None and job.review_ready and job.review_path:
                # 暂停点由流水线线程维护，状态推送每次现取（日志里那份可能还没跟上）
                if job.review_kind == "final":
                    self._status["subtitle_final"] = job.review_path
                    if job.review_source and Path(job.review_source).is_file():
                        self._status["subtitle_raw"] = job.review_source
                else:
                    self._status["subtitle_raw"] = job.review_path
            s = dict(self._status)
            s["awaiting_review"] = bool(job is not None and job.review_ready)
            s["review_kind"] = job.review_kind if job is not None and job.review_ready else ""
            s["log"] = s["log"][-300:]
            s["tools"] = self._tools
            return s

    # ---------------- 字幕检查 / 编辑 ----------------

    def continue_review(self, path: str = "") -> dict:
        """字幕检查暂停点：用户点「继续翻译」，带着编辑后的字幕继续跑。

        path 为空 = 没改，继续用原始识别结果；否则用用户选定的编辑副本。
        """
        job = self._job
        if job is None or not job.review_ready:
            return {"ok": False, "error": "当前没有在等待检查的字幕"}
        p = Path(str(path or "").strip().strip('"'))
        if not p.is_file() and job.review_path:
            p = Path(job.review_path)
        if not p.is_file():
            return {"ok": False, "error": f"找不到字幕文件：{p}"}
        job.review_continue_path = str(p)
        job.review_ready = False
        job.review_event.set()
        with self._lock:
            self._status["awaiting_review"] = False
        return {"ok": True}

    def subtitle_load(self, path: str) -> dict:
        """读一份 SRT，返回逐段文本（「字幕检查」页用）。"""
        import dubber
        p = Path(str(path or "").strip().strip('"'))
        if not p.is_file():
            return {"ok": False, "error": f"字幕文件不存在：{p}"}
        try:
            cues = dubber.parse_srt(p)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"读不了字幕：{type(e).__name__}: {e}"}
        if not cues:
            return {"ok": False, "error": "这份字幕是空的，或者不是 SRT 格式"}
        return {"ok": True, "path": str(p), "segments": [
            {"i": i, "start": round(s, 3), "end": round(e, 3), "text": t}
            for i, (s, e, t) in enumerate(cues, 1)]}

    def subtitle_save(self, path: str, segments: list) -> dict:
        """把编辑后的逐段文本写回 SRT（先备份 `.bak`，再原子替换）。"""
        import ytsub
        p = Path(str(path or "").strip().strip('"'))
        if not p.name:
            return {"ok": False, "error": "没有指定字幕路径"}
        cues = []
        for x in segments or []:
            try:
                s = float(x.get("start"))
                e = float(x.get("end"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "有一条字幕的时间轴读不出来"}
            text = str(x.get("text") or "").strip()
            if not text:
                continue                      # 清空 = 删掉这一条
            if e <= s:
                e = s + 0.5
            cues.append((s, e, text))
        if not cues:
            return {"ok": False, "error": "字幕全空了，没有保存（至少留一条）"}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            backup = ""
            if p.is_file():
                import shutil
                bak = p.with_name(p.name + ".bak")
                shutil.copy2(p, bak)
                backup = str(bak)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(ytsub.to_srt(cues), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            return {"ok": False, "error": f"写不了字幕：{e}"}
        return {"ok": True, "path": str(p), "count": len(cues), "backup": backup}

    def reburn(self) -> dict:
        """成品字幕改完后重新压制（配音版不变，只换画面上的字幕）。"""
        with self._lock:
            if self._status.get("running"):
                return {"ok": False, "error": "已有任务在运行中"}
            result = dict(self._last_result or {})
            raw = self._status.get("subtitle_raw") or ""
            srt_final = self._status.get("subtitle_final") or ""
        srt = Path(str(result.get("srt") or ""))
        if not srt.is_file():
            return {"ok": False, "error": "还没有成品字幕可以重新压制"}
        video = Path(str(result.get("dubbed") or ""))
        if not video.is_file():
            video = Path(str(result.get("video") or ""))
        if not video.is_file():
            return {"ok": False, "error": "找不到配音版或原视频"}
        out_dir = Path(str(result.get("out_dir") or srt.parent))
        if result.get("final"):
            final = Path(str(result["final"]))
        else:
            stem = video.stem[:-7] if video.stem.endswith("_dubbed") else video.stem
            final = out_dir / f"{stem}_final.mp4"
        params = cfg.load()
        job = Job()
        with self._lock:
            self._job = job
            self._status = {"running": True, "stage": "burn", "percent": 0,
                            "log": ["重新压制字幕…"], "result": result, "error": "",
                            "out_dir": str(out_dir), "subtitle_raw": raw,
                            "subtitle_final": srt_final or str(srt)}
        self._thread = threading.Thread(target=self._run_reburn,
                                        args=(job, video, srt, final, params,
                                              Path(raw) if raw else None), daemon=True)
        self._thread.start()
        return {"ok": True}

    def _run_reburn(self, job: Job, video: Path, srt: Path, final: Path, params: dict,
                    src_srt: Path | None = None):
        try:
            from pipeline import burn
            burn(video, srt, final, job, self._report, params, src_srt=src_srt)
            with self._lock:
                if self._last_result is not None:
                    self._last_result["final"] = str(final)
                    self._status["result"] = dict(self._last_result)
                self._status.update(running=False, stage="done", percent=100)
            self._report("done", 100, "🎉 成片已重新压制")
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled")
            self._report("cancelled", None, "⏹ 已取消")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            diag.write_exception(e, "重新压制字幕")
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ {msg}")

    def open_path(self, path: str) -> dict:
        """用系统默认程序打开文件/文件夹"""
        p = Path(path)
        if p.exists():
            os.startfile(str(p))  # noqa: S606 - 用户本机路径
            return {"ok": True}
        return {"ok": False, "error": "路径不存在"}

    def reveal(self, path: str) -> dict:
        """在资源管理器中定位文件"""
        p = Path(path)
        if p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)], **no_window_kwargs())
            return {"ok": True}
        return {"ok": False, "error": "路径不存在"}

    # ---------------- 内部 ----------------

    def _report(self, stage: str, percent, line: str):
        with self._lock:
            if stage:
                self._status["stage"] = stage
            if percent is not None:
                self._status["percent"] = max(0, min(100, int(percent)))
        if not line:
            return

        # **不折叠、不去重**：用户明确要求「同类信息重复出现，后续已折叠」这种不要。
        # 日志是给人看的（也是给售后看的），被吞掉的行恰恰可能是关键那一行。
        # 只做一个容量上限：界面上留最近 800 行、超了截到 500，免得界面越拖越卡。
        with self._lock:
            self._status["log"].append(line)
            if len(self._status["log"]) > 800:
                self._status["log"] = self._status["log"][-500:]
        self._note_subtitle_paths(line)
        diag.write(f"[{stage}] {line}", "run")

    def _note_subtitle_paths(self, line: str) -> None:
        """从日志里认出输出目录 / 原始字幕，供界面「字幕检查」页做双语对照。"""
        m = _RE_OUT_DIR.search(line)
        if m:
            with self._lock:
                self._status["out_dir"] = m.group(1).strip()
        m = _RE_RAW_SRT.search(line)
        if m:
            raw = m.group(1).strip().strip('"')
            try:
                if not Path(raw).is_file():
                    return
            except OSError:
                return
            with self._lock:
                self._status["subtitle_raw"] = raw

    @staticmethod
    def _money(b: dict | None) -> str:
        cur = str((b or {}).get("currency") or "CNY")
        sym = "¥" if cur.upper() == "CNY" else ("$" if cur.upper() == "USD"
                                               else (cur + " " if cur else ""))
        return f"{sym}{float((b or {}).get('total') or 0):.2f}"

    def _balance_snapshot(self) -> dict | None:
        """查一次余额；失败/没配 key 都返回 None，绝不耽误翻译"""
        try:
            b = self.deepseek_balance(timeout=8)
            return b if b.get("ok") else None
        except Exception:  # noqa: BLE001
            return None

    def _run(self, params: dict):
        bal0 = None
        if not params.get("download_only"):
            bal0 = self._balance_snapshot()
            if bal0:
                self._report("translate", None,
                             f"💳 DeepSeek 当前额度：{self._money(bal0)}")
        try:
            result = run_pipeline(params, self._job, self._report)
            if bal0:
                bal1 = self._balance_snapshot()
                if bal1:
                    spent = float(bal0.get("total") or 0) - float(bal1.get("total") or 0)
                    cur = bal1.get("currency") or bal0.get("currency")
                    result["balance_before"] = bal0.get("total")
                    result["balance_after"] = bal1.get("total")
                    result["balance_spent"] = spent
                    result["balance_currency"] = cur
                    self._report("translate", None,
                                 f"💳 DeepSeek 本次消耗：{self._money({'currency': cur, 'total': spent})}"
                                 f"（翻译前 {self._money(bal0)} → 翻译后 {self._money(bal1)}）")
            with self._lock:
                self._last_result = result
                self._status.update(running=False, stage="done", percent=100, result=result,
                                    subtitle_final=str(result.get("srt") or ""))
                # 日志解析没认出来时（比如引擎跳过了第 4 阶段），按同一套规则再找一遍
                if not self._status.get("subtitle_raw"):
                    try:
                        from pipeline import _work_dir
                        video = Path(str(result.get("video") or ""))
                        raw = _work_dir(video, params) / f"{video.stem}-raw.srt"
                        if raw.is_file():
                            self._status["subtitle_raw"] = str(raw)
                    except Exception:  # noqa: BLE001 - 只是给界面补个路径
                        pass
            self._report("done", 100, "🎉 全部完成")
        except Cancelled:
            with self._lock:
                self._status.update(running=False, stage="cancelled", result=None)
            self._report("cancelled", None, "⏹ 已取消")
        except Exception as e:  # noqa: BLE001 - 统一呈现给用户
            msg = f"{type(e).__name__}: {e}"
            # detail 是流水线在失败现场攒的（子进程退出码 + 最后 120 行输出）。
            # 引擎有不少失败是"子进程没吐任何堆栈就死了"，只记异常类型等于没线索。
            diag.write_exception(e, "翻译任务", detail=str(getattr(e, "detail", "") or ""))
            with self._lock:
                self._status.update(running=False, stage="error", error=msg)
            self._report("error", None, f"❌ {msg}（详细日志已写入 logs 目录，可在设置里打开）")


def _apply_window_icon(title: str = APP_TITLE, ico: Path = APP_ICON_ICO, timeout: float = 15.0):
    """把窗口/任务栏图标换成 assets/app.ico（pywebview 无图标 API，走 Win32）

    在后台线程里等窗口出现后 FindWindowW 找到 HWND，再 WM_SETICON。
    """
    if not ico.exists():
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        deadline = time.time() + timeout
        hwnd = 0
        while time.time() < deadline:
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.3)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        for size, kind in ((16, ICON_SMALL), (32, ICON_BIG)):
            h = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                user32.SendMessageW(hwnd, WM_SETICON, kind, h)
    except Exception:
        pass  # 图标失败不影响功能


def main():
    # 以「后端」身份启动：给 WinUI 3 前端当 JSON-RPC 服务（见 winui_backend.py）。
    # 打包出来的这个 exe 里已经带着整套逻辑（流水线/引擎/配音/更新器），
    # 所以 WinUI 版直接拉它当后端就行，不必再随包带一份 Python。
    if "--rpc" in sys.argv:
        import winui_backend
        raise SystemExit(winui_backend.main())

    # 以 MCP 服务器身份启动：给 Claude Code / 其它 MCP 客户端直接调用。
    # 同一个 exe、同一套设置与 API Key，只是换成 MCP 的 stdio 协议。
    # --mcp-http 是给只收 HTTP 的客户端用的本地 HTTP 模式（只监听 127.0.0.1）。
    # 进程守卫必须有：MCP 客户端关掉服务器时，引擎子进程不能变成孤儿继续占锁。
    if "--mcp" in sys.argv or "--mcp-http" in sys.argv:
        guard_processes()
        import mcp_server
        raise SystemExit(mcp_server.main(http="--mcp-http" in sys.argv))

    diag.install_hooks()
    diag.write(f"进程守卫（关闭时清理子进程树）: {'已启用' if guard_processes() else '未启用'}", "boot")
    diag.start_session(APP_VERSION)
    api = Api()
    window = webview.create_window(
        APP_TITLE, str(WEB_DIR / "index.html"), js_api=api,
        width=1400, height=940, min_size=(980, 700),
    )
    api.attach_window(window)
    try:
        # 关闭窗口时把子进程树带走（详见 Api._on_closing）
        window.events.closing += api._on_closing
    except Exception:  # noqa: BLE001 - 老版 pywebview 没有这个事件，靠 job.guard() 兜底
        pass
    threading.Thread(target=_apply_window_icon, daemon=True).start()
    boot_cfg = cfg.load()
    if boot_cfg.get("auto_update_check", True):
        api.auto_check_update()
    if boot_cfg.get("auto_update_downloader", True):
        api.auto_update_downloader()
    webview.start()


if __name__ == "__main__":
    main()
