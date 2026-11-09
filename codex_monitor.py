"""Read-only Codex activity monitor. Python 3.10+, Windows, standard library only."""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from clash_failover import ClashClient, FailoverGuard

BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = BASE / "monitor-settings.json"
LOG = logging.getLogger("codex_monitor")
LOG.addHandler(logging.NullHandler())
FAULT_STREAM = None
TAB_WIDTH, TAB_HEIGHT = 72, 64
PAD_COLOR = "#b78498"
NETWORK_ERROR = re.compile(r"connection (?:reset|refused|closed)|timed? out|timeout|dns|proxy|tls|ssl|network is unreachable|stream disconnected|error sending request|failed to connect|websocket.*(?:error|fail)|reconnecting", re.I)


def log_exception(context, exc_type, exc, tb):
    # Record locations without exception messages or source lines that may contain
    # proxy credentials or session contents.
    frames = [(f.filename, f.lineno, f.name, None) for f in traceback.extract_tb(tb)]
    LOG.error("%s: %s\n%s", context, exc_type.__name__, "".join(traceback.format_list(frames)))


def setup_logging():
    global FAULT_STREAM
    try:
        handler = RotatingFileHandler(BASE / "monitor-runtime.log", maxBytes=512 * 1024,
                                      backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    try:
        FAULT_STREAM = (BASE / "monitor-fault.log").open("a", encoding="utf-8")
        faulthandler.enable(file=FAULT_STREAM)
    except (OSError, RuntimeError):
        LOG.warning("Native fault logging unavailable")
    sys.excepthook = lambda *args: log_exception("main", *args)
    threading.excepthook = lambda args: log_exception("worker " + args.thread.name,
                                                     args.exc_type, args.exc_value, args.exc_traceback)


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60:02d} 秒"
    return f"{seconds // 3600} 时 {seconds % 3600 // 60:02d} 分"


@dataclass
class Session:
    id: str
    path: Path
    title: str = "未命名聊天"
    active: bool | None = None
    last_activity: float = 0
    turn_started: float = 0
    action: str = "等待会话记录"
    pending: dict = field(default_factory=dict)
    network_error_at: float = 0
    offset: int = 0
    identity: tuple | None = None

    def consume(self, record):
        p = record.get("payload", {})
        kind, event = record.get("type"), p.get("type")
        at = timestamp(record.get("timestamp"))
        if kind == "event_msg":
            if event in ("task_started", "turn_started"):
                self.active = True
                self.turn_started = at
                self.pending.clear()
                self.network_error_at = 0
                self.action = "任务进行中"
            elif event in ("task_complete", "task_completed", "turn_completed", "turn_aborted", "task_failed"):
                self.active = False
                self.pending.clear()
                self.action = "已中止" if event == "turn_aborted" else ("任务失败" if event == "task_failed" else "已完成")
            elif event in ("error", "stream_error"):
                self.action = "任务报告错误"
                if NETWORK_ERROR.search(json.dumps(p, ensure_ascii=False)):
                    self.network_error_at = at
            else:
                # Usage, bookkeeping and settings writes are not visible progress.
                return
        elif kind == "response_item":
            if event in ("function_call", "custom_tool_call"):
                self.active = True
                name = p.get("name", "工具")
                self.pending[p.get("call_id", p.get("id", name))] = name
                self.action = f"执行工具 · {name}"
            elif event in ("function_call_output", "custom_tool_call_output"):
                self.pending.pop(p.get("call_id"), None)
                self.action = "工具已返回，等待后续" if not self.pending else "等待其他工具返回"
            elif event == "reasoning":
                self.action = "模型思考中"
            elif event == "message" and p.get("role") == "assistant":
                self.action = "正在回复"
                # Explicit terminal events take precedence; some versions omit them.
                if p.get("phase") == "final":
                    self.active = False
                    self.pending.clear()
                    self.action = "已回复，等待任务结束记录"
            else:
                return
        else:
            return
        self.last_activity = max(self.last_activity, at)

    def read(self):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.identity != identity or stat.st_size < self.offset:
            self.active = None
            self.last_activity = 0
            self.turn_started = 0
            self.network_error_at = 0
            self.pending.clear()
            self.action = "等待会话记录"
            self.offset = 0
        self.identity = identity
        with self.path.open("rb") as stream:
            # Bound first read on huge histories; never parse a truncated record.
            if self.offset == 0 and stat.st_size > 4 * 1024 * 1024:
                stream.seek(stat.st_size - 4 * 1024 * 1024)
                stream.readline()
                self.offset = stream.tell()
            stream.seek(self.offset)
            budget = 8 * 1024 * 1024
            while budget > 0:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break  # Retry partial writes at the same offset next time.
                self.offset = stream.tell()
                budget -= len(line)
                try:
                    self.consume(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    continue


def classify(session, now, warn=60, severe=180, running=True, probe_failed=False):
    if not running:
        return "Codex 未运行", "#8b95a7"
    if session is None or not session.last_activity:
        return "尚无活动记录", "#8b95a7"
    age = max(0, now - session.last_activity)
    if session.active is False:
        return "空闲 · 等待新任务", "#8b95a7"
    if age >= warn:
        if session.network_error_at > max(session.turn_started, now - 300):
            return "无新活动 · 近期有连接错误", "#ff7b79"
        if probe_failed:
            return "无新活动 · 网络探测失败", "#ff7b79"
        return ("长时间无新活动", "#ff7b79") if age >= severe else ("持续无新活动", "#f4c567")
    return ("运行中", "#62d6ac") if session.active else ("近期有活动 · 状态待确认", "#7db8ff")


def codex_running(desktop_only=False):
    if os.name != "nt":
        return True
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        inaccessible = False
        while ok:
            name = entry.szExeFile.lower()
            if name in ("codex.exe", "codex-app.exe", "chatgpt.exe"):
                if not desktop_only:
                    return True
                if name == "chatgpt.exe":
                    process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                    if process:
                        try:
                            size = wintypes.DWORD(32768)
                            path = ctypes.create_unicode_buffer(size.value)
                            if kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                                if "openai.codex" in path.value.lower():
                                    return True
                            else:
                                inaccessible = True
                        finally:
                            kernel.CloseHandle(process)
                    else:
                        inaccessible = True
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        # Only ERROR_NO_MORE_FILES confirms a complete process enumeration.
        # A failed enumeration must not be treated as the app having exited.
        return None if inaccessible or ctypes.get_last_error() != 18 else False
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def single_instance():
    if os.name != "nt":
        yield True
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, "Local\\CodexActivityMonitorV1")
    error = ctypes.get_last_error()
    try:
        yield bool(handle) and error != 183
    finally:
        if handle:
            kernel.CloseHandle(handle)


@contextmanager
def readonly_db(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    try:
        yield connection
    finally:
        connection.close()


class Monitor:
    def __init__(self, home):
        self.home = home
        self.sessions = {}
        self.discovered_at = 0
        self.read_error = ""

    def discover(self):
        # Schema names are versioned; prefer newest file, retain a filesystem fallback.
        candidates = sorted(self.home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for db in candidates:
            try:
                with readonly_db(db) as conn:
                    rows = conn.execute("SELECT id, rollout_path, title FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT 100").fetchall()
                break
            except sqlite3.Error:
                continue
        if not rows:
            paths = sorted((self.home / "sessions").glob("*/*/*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
            rows = [(p.stem[-36:], str(p), p.stem[-36:]) for p in paths]
        for sid, path, title in rows:
            if not Path(path).is_file():
                continue
            if sid not in self.sessions:
                self.sessions[sid] = Session(sid, Path(path), title)
            else:
                self.sessions[sid].title = title
                self.sessions[sid].path = Path(path)
        ids = {r[0] for r in rows if Path(r[1]).is_file()}
        self.sessions = {sid: s for sid, s in self.sessions.items() if sid in ids}

    def errors(self, now):
        candidates = sorted(self.home.glob("logs_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return
        try:
            with readonly_db(candidates[0]) as conn:
                rows = conn.execute("SELECT ts,thread_id,feedback_log_body FROM logs WHERE id > (SELECT coalesce(max(id),0)-2000 FROM logs) AND ts > ? AND level IN ('WARN','ERROR','warn','error') ORDER BY id DESC LIMIT 200", (int(now - 300),)).fetchall()
            for at, sid, body in rows:
                if sid in self.sessions and NETWORK_ERROR.search(body or ""):
                    self.sessions[sid].network_error_at = max(self.sessions[sid].network_error_at, at)
        except sqlite3.Error:
            pass

    def poll(self):
        now = time.time()
        self.read_error = ""
        try:
            if now - self.discovered_at > 10:
                self.discover()
                self.errors(now)
                self.discovered_at = now
        except (OSError, sqlite3.Error) as exc:
            self.read_error = f"发现会话失败：{type(exc).__name__}"
        for s in list(self.sessions.values()):
            try:
                s.read()
            except OSError as exc:
                self.read_error = f"部分记录不可读：{type(exc).__name__}"
        return sorted(self.sessions.values(), key=lambda s: s.last_activity, reverse=True)


def probe(url, proxy_port=0):
    start = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "CodexActivityMonitor/1.0"})
    try:
        if proxy_port:
            address = f"http://127.0.0.1:{int(proxy_port)}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": address, "https": address}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(request, timeout=5) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
    except Exception as exc:
        # Never show URLs/proxy credentials from exception messages.
        certificate = isinstance(exc, ssl.SSLCertVerificationError) or isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError)
        return {"ok": False, "kind": "certificate" if certificate else "connection", "text": "证书校验失败" if certificate else f"失败 · {type(exc).__name__}", "at": time.time()}
    elapsed = int((time.monotonic() - start) * 1000)
    # 401/403 prove HTTPS connectivity, not authenticated Codex service health.
    limited = code >= 400
    return {"ok": False if code >= 500 else (None if limited else True), "kind": "http", "text": f"HTTP {code} · {elapsed}ms" + (" · 仅确认可达" if limited and code < 500 else ""), "at": time.time()}


def load_settings():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Window:
    def __init__(self, home, demo=False, follow_codex=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.demo = demo
        self.follow_codex = follow_codex
        self.app_available = False
        self.app_absent_samples = 0
        self.last_visibility_check = 0
        self.last_heartbeat = 0
        self.monitor = Monitor(home)
        self.settings = load_settings()
        self.dock_side = self.settings.get("dock_side", "right")
        self.dock_y = int(self.settings.get("dock_y", self.settings.get("y", 180)))
        self.dragging = False
        self.screen_signature = None
        self.warn = max(10, int(self.settings.get("warn_seconds", 60)))
        self.severe = max(self.warn + 1, int(self.settings.get("severe_seconds", 180)))
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *args: log_exception("Tk callback", *args)
        if follow_codex:
            self.root.withdraw()
        self.root.title("Codex 悬浮监测 · 模拟测试" if demo else "Codex 悬浮监测")
        self.root.configure(bg="#151c28")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if os.name == "nt":
            # Keep color-key composition enabled for both views. The full panel has
            # no magenta pixels, so it remains opaque without a compositor switch.
            self.root.attributes("-transparentcolor", "#ff00ff")
        self.root.geometry(f"320x104+{self.settings.get('x', 60)}+{self.settings.get('y', 80)}")
        self.root.minsize(320, 104)
        self.stop = threading.Event()
        self.updates = queue.Queue()
        self.probes = {}
        self.snapshot = []
        self.running = None
        self.task_count = 0
        self.selected = None
        self.mapping = {}
        self.network_on = tk.BooleanVar(value=self.settings.get("network_probe", True))
        self.probe_enabled = self.network_on.get() and not follow_codex
        self.auto_switch = tk.BooleanVar(value=self.settings.get("auto_switch", True))
        self.switch_enabled = self.auto_switch.get() and not demo
        self.switch_context = None
        self.switch_message = "自动换节点：等待条件满足"
        self.detail = tk.BooleanVar(value=False)
        self.unfocused_since = None
        self.compact = tk.Canvas(self.root, width=TAB_WIDTH, height=TAB_HEIGHT, bg="#ff00ff", highlightthickness=0, cursor="hand2")
        self.compact.bind("<Button-1>", self.tab_press)
        self.compact.bind("<B1-Motion>", self.tab_drag)
        self.compact.bind("<ButtonRelease-1>", self.tab_release)
        self.tab_color, self.tab_seconds = "#7db8ff", "—"
        header = tk.Frame(self.root, bg="#202b3c", height=32)
        self.header = header
        header.pack(fill="x")
        header.pack_propagate(False)
        label = tk.Label(header, text="◉  CODEX MONITOR", bg="#202b3c", fg="#e4ebf5", font=("Segoe UI", 10, "bold"))
        label.pack(side="left", padx=12)
        self.header_label = label
        for widget in (header, label):
            widget.bind("<Button-1>", self.drag_start)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.drag_end)
        tk.Button(header, text="−", command=self.collapse, relief="flat", bg="#202b3c", fg="#b9c6d9", width=3).pack(side="right")
        self.body = tk.Frame(self.root, bg="#151c28")
        self.body.pack(fill="both", expand=True, padx=14, pady=10)
        self.status = self.label("正在读取本地状态…", 15, "#7db8ff", True)
        self.age = self.label("最近活动：—", 11)
        self.action = self.label("", 9, "#97a8c0")
        self.combo = ttk.Combobox(self.body, state="readonly", font=("Microsoft YaHei UI", 9))
        self.combo.pack(fill="x", pady=(8, 6))
        self.combo.bind("<<ComboboxSelected>>", self.select)
        self.net = self.label("网络：尚未探测", 9, "#97a8c0")
        self.general = self.label("", 9, "#97a8c0")
        self.hint = self.label("", 9, "#97a8c0")
        self.failover_label = self.label(self.switch_message, 9, "#97a8c0")
        footer = tk.Frame(self.body, bg="#151c28")
        footer.pack(fill="x", pady=(5, 0))
        tk.Checkbutton(footer, text="网络探测", variable=self.network_on, bg="#151c28", fg="#97a8c0", selectcolor="#202b3c", activebackground="#151c28", activeforeground="#e4ebf5", font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Button(footer, text="阈值设置", command=self.preferences, bg="#202b3c", fg="#c9d6e8", relief="flat", font=("Microsoft YaHei UI", 9)).pack(side="right")
        tk.Checkbutton(self.body, text="连接连续失败时自动换日本节点", variable=self.auto_switch, bg="#151c28", fg="#97a8c0", selectcolor="#202b3c", activebackground="#151c28", activeforeground="#e4ebf5", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 0))
        exit_row = tk.Frame(self.body, bg="#151c28")
        exit_row.pack(side="bottom", fill="x", pady=(12, 0))
        self.exit_button = tk.Button(exit_row, text="退出监测", command=self.close, relief="flat", bg="#202b3c", fg="#97a8c0", font=("Microsoft YaHei UI", 9), padx=10, pady=3)
        self.exit_button.pack(side="right")
        self.root.bind("<Escape>", lambda e: self.set_view(False))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.set_view(False)
        if not demo:
            threading.Thread(target=self.poll_worker, daemon=True).start()
            threading.Thread(target=self.network_worker, daemon=True).start()
        self.root.after(200, self.tick)

    def label(self, text, size, color="#dce5f4", bold=False):
        w = self.tk.Label(self.body, text=text, bg="#151c28", fg=color, anchor="w", justify="left", wraplength=292, font=("Microsoft YaHei UI", size, "bold" if bold else "normal"))
        w.pack(fill="x", pady=2)
        return w

    def drag_start(self, event):
        self.dragging = True
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag_end(self, event=None):
        self.dragging = False

    def drag(self, event):
        x, y = self.drag_origin
        self.root.geometry(f"+{max(0, event.x_root-x)}+{max(0, event.y_root-y)}")

    def collapse(self):
        self.set_view(not self.detail.get())

    def work_area(self):
        if os.name == "nt":
            rect = wintypes.RECT()
            if (ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
                    and rect.right > rect.left and rect.bottom > rect.top):
                return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def dock_geometry(self, width, height):
        left, top, right, bottom = self.work_area()
        x = left if self.dock_side == "left" else right - width
        y = max(top, min(self.dock_y, bottom - height))
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def ensure_visible(self, force=False):
        """Recover from display/DPI/work-area changes without stealing focus."""
        if self.dragging:
            return
        area = self.work_area()
        left, top, right, bottom = area
        if right <= left or bottom <= top:
            return  # Remote desktop/display transitions can briefly report no area.
        signature = (area, round(float(self.root.winfo_fpixels("1i")), 1))
        previous = self.screen_signature
        self.screen_signature = signature
        should_show = not self.follow_codex or self.app_available
        if not should_show:
            return  # Preserve intentional hiding when Codex exits.
        if self.root.state() in ("withdrawn", "iconic"):
            LOG.warning("Restoring unexpected window state=%s", self.root.state())
            self.root.deiconify()
            self.root.update_idletasks()
            force = True
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if (width > right - left or height > bottom - top) and self.detail.get():
            # A very small remote-desktop area may not fit the full panel at all.
            self.set_view(False)
            self.root.update_idletasks()
            width, height = self.root.winfo_width(), self.root.winfo_height()
            force = True
        x, y = self.root.winfo_x(), self.root.winfo_y()
        changed = previous is not None and previous != signature
        outside = x < left or y < top or x + width > right or y + height > bottom
        if force or changed or outside:
            LOG.info("Reposition window force=%s display_changed=%s outside=%s area=%s", force, changed, outside, area)
            if changed:
                old_left, old_top, old_right, old_bottom = previous[0]
                old_span = max(1, old_bottom - old_top - height)
                ratio = min(1.0, max(0.0, (y - old_top) / old_span))
                self.dock_y = top + round(ratio * max(0, bottom - top - height))
            else:
                self.dock_y = max(top, min(y, bottom - height))
            self.dock_geometry(width, height)
            self.root.attributes("-topmost", True)
        # Windows can change z-order without changing Tk's state or geometry.
        # Reassert topmost periodically, without activating or moving the window.
        now = time.monotonic()
        if now - self.last_visibility_check >= 5:
            self.root.attributes("-topmost", True)
            self.last_visibility_check = now
        if now - self.last_heartbeat >= 60:
            LOG.info("UI alive available=%s state=%s viewable=%s rect=%s",
                     self.app_available, self.root.state(), self.root.winfo_viewable(),
                     self.root.geometry())
            self.last_heartbeat = now

    def draw_tab(self):
        c = self.compact
        if c.find_withtag("seconds"):
            if c.itemcget("seconds", "text") != self.tab_seconds or c.itemcget("seconds", "fill") != self.tab_color:
                c.itemconfigure("seconds", text=self.tab_seconds, fill=self.tab_color)
            self.update_task_pads()
            return
        # Five simple outlines; an opaque dark interior keeps the paw clickable.
        ink, paper = "#8297aa", "#273747"
        for bounds in ((4, 15, 18, 32), (19, 3, 33, 24),
                       (38, 3, 52, 24), (54, 15, 68, 32)):
            c.create_oval(*bounds, fill=paper, outline=ink, width=1.3)
        c.create_polygon(11, 43, 20, 30, 30, 28, 36, 32, 43, 28,
                         53, 31, 62, 44, 65, 54, 59, 60, 47, 60,
                         36, 57, 24, 60, 13, 60, 7, 54,
                         smooth=True, splinesteps=24, fill=paper, outline=ink, width=1.3)
        c.create_oval(24, 47, 48, 56, fill="", outline="", tags="task-pad-0")
        for index, bounds in enumerate(((8, 20, 14, 28), (24, 9, 28, 19),
                                         (43, 9, 47, 19), (58, 20, 64, 28)), 1):
            c.create_oval(*bounds, fill="", outline="", tags=f"task-pad-{index}")
        c.create_text(36, 38, text=self.tab_seconds, fill=self.tab_color,
                      font=("Segoe UI", 9, "bold"), tags="seconds")
        self.update_task_pads()

    def update_task_pads(self):
        for index in range(5):
            fill = PAD_COLOR if self.task_count > index else ""
            tag = f"task-pad-{index}"
            if self.compact.itemcget(tag, "fill") != fill:
                self.compact.itemconfigure(tag, fill=fill)

    def tab_press(self, event):
        self.drag_start(event)
        self.tab_start = (event.x_root, event.y_root)
        self.tab_moved = False

    def tab_drag(self, event):
        if abs(event.x_root - self.tab_start[0]) + abs(event.y_root - self.tab_start[1]) > 4:
            self.tab_moved = True
            self.drag(event)

    def tab_release(self, event):
        self.dragging = False
        if self.tab_moved:
            left, _, right, _ = self.work_area()
            self.dock_side = "left" if event.x_root < (left + right) // 2 else "right"
            self.dock_y = self.root.winfo_y()
            self.set_view(False)
        else:
            self.set_view(True)

    def set_view(self, expanded):
        self.unfocused_since = None
        if self.detail.get() and not expanded:
            left, _, right, _ = self.work_area()
            self.dock_side = "left" if self.root.winfo_x() + 160 < (left + right) // 2 else "right"
            self.dock_y = self.root.winfo_y()
        self.detail.set(expanded)
        if not expanded:
            self.header.pack_forget()
            self.body.pack_forget()
            self.root.minsize(TAB_WIDTH, TAB_HEIGHT)
            self.dock_geometry(TAB_WIDTH, TAB_HEIGHT)
            self.compact.pack(fill="both", expand=True)
            self.draw_tab()
        else:
            self.compact.pack_forget()
            self.root.minsize(320, 350)
            self.dock_geometry(320, 350)
            self.header.pack(fill="x")
            self.body.pack(fill="both", expand=True, padx=14, pady=10)
            self.fit_details()

    def fit_details(self):
        # Keep the full panel at 320 pixels; grow vertically for wrapped content.
        self.root.update_idletasks()
        height = max(350, self.header.winfo_reqheight() + self.body.winfo_reqheight() + 20)
        if self.root.winfo_width() != 320 or self.root.winfo_height() != height:
            self.root.minsize(320, height)
            self.dock_geometry(320, height)

    def select(self, event=None):
        self.selected = self.mapping.get(self.combo.get())

    def panel_has_focus(self):
        if os.name == "nt":
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.GetForegroundWindow.restype = wintypes.HWND
            user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            process_id = wintypes.DWORD()
            user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(process_id))
            # Include our settings dialog and combo popup; moving between controls is focus.
            return process_id.value == os.getpid()
        focused = self.root.focus_displayof()
        return focused is not None

    def update_auto_collapse(self, now=None, focused=None):
        if not self.detail.get() or not self.root.winfo_viewable():
            self.unfocused_since = None
            return
        now = time.monotonic() if now is None else now
        focused = self.panel_has_focus() if focused is None else focused
        if focused:
            self.unfocused_since = None
        elif self.unfocused_since is None:
            self.unfocused_since = now
        elif now - self.unfocused_since >= 60:
            self.set_view(False)

    def preferences(self):
        from tkinter import simpledialog
        value = simpledialog.askinteger("无活动提醒", "多少秒没有新活动时提醒？", initialvalue=self.warn, minvalue=10, maxvalue=3600, parent=self.root)
        if value is not None:
            self.warn, self.severe = value, max(value * 3, value + 60)

    def poll_worker(self):
        while not self.stop.is_set():
            try:
                if self.follow_codex:
                    available = codex_running(desktop_only=True)
                    self.updates.put(("availability", available))
                    if available is not True:
                        self.stop.wait(2)
                        continue
                sessions = self.monitor.poll()
                # Queue immutable snapshots; Tk only sees objects owned by the UI thread.
                rows = [Session(s.id, s.path, s.title, s.active, s.last_activity, s.turn_started, s.action, dict(s.pending), s.network_error_at) for s in sessions]
                self.updates.put(("sessions", (rows, codex_running(), self.monitor.read_error)))
            except Exception as exc:
                self.updates.put(("error", type(exc).__name__))
            self.stop.wait(2)

    def network_worker(self):
        # BooleanVar cannot be accessed from a worker; tick mirrors the value.
        guard = FailoverGuard(BASE / "failover-state.json")
        while not self.stop.is_set():
            if getattr(self, "probe_enabled", False):
                try:
                    client = ClashClient.discover() if self.switch_enabled else None
                except Exception:
                    client = None
                for name, url in (("codex", "https://chatgpt.com/backend-api/codex"), ("general", "https://www.microsoft.com")):
                    if self.stop.is_set():
                        return
                    result = probe(url, client.proxy_port if client else 0)
                    result["proxy"] = bool(client and client.proxy_port) or bool(urllib.request.getproxies())
                    self.updates.put((name, result))
                    if name == "codex":
                        context = self.switch_context if self.switch_enabled and self.probe_enabled and client and client.proxy_port else None
                        ready = guard.observe(result, context, time.time())
                        if self.switch_enabled:
                            if client is None:
                                self.updates.put(("failover", "自动换节点：未找到 Clash 本地接口"))
                            elif ready:
                                self.perform_failover(client, guard, context)
                            elif context and guard.streak:
                                self.updates.put(("failover", f"自动换节点：连续失败 {guard.streak}/3 · 受冷却限制" if guard.streak >= 3 else f"自动换节点：连续失败 {guard.streak}/3"))
                            elif result.get("kind") == "certificate":
                                self.updates.put(("failover", "自动换节点：证书错误，不作为换节点依据"))
                            else:
                                self.updates.put(("failover", "自动换节点：等待任务无活动且连续3次失败"))
            else:
                guard.streak = 0
                guard.context = None
            self.stop.wait(10)

    def perform_failover(self, client, guard, context):
        def allowed():
            return (not self.stop.is_set() and self.switch_enabled and self.probe_enabled
                    and self.switch_context == context and context is not None)
        event = {"at": time.time(), "result": "未切换"}
        try:
            if not allowed():
                return
            guard.reserve(time.time())
            self.updates.put(("failover", "自动换节点：正在检测日本备用节点…"))
            plan = client.plan()
            candidate = client.choose(plan, allowed)
            if not candidate:
                self.updates.put(("failover", "自动换节点：未找到可用日本节点或任务已恢复"))
                return
            # Recheck task state and manual selection immediately before changing it.
            if not allowed() or not client.unchanged(plan):
                self.updates.put(("failover", "自动换节点：任务已恢复或检测到手动切换，已取消"))
                return
            client.select(plan["group"], candidate)
            event.update(group=plan["group"], previous=plan["old"], selected=candidate)
            checked = probe("https://chatgpt.com/backend-api/codex", client.proxy_port)
            self.updates.put(("codex", checked))
            if checked.get("ok") is False:
                if client.current(plan["group"]) == candidate:
                    client.select(plan["group"], plan["old"])
                    event["result"] = "切换后探测失败，已回退"
                else:
                    event["result"] = "切换后探测失败，保留手动选择"
            else:
                event["result"] = "已切换，HTTPS可达" if checked.get("ok") is True else "已切换，仅确认HTTPS可达"
            self.updates.put(("failover", "自动换节点：" + event["result"]))
        except Exception as exc:
            event["result"] = "操作失败 · " + type(exc).__name__
            self.updates.put(("failover", "自动换节点：" + event["result"]))
        finally:
            try:
                with (BASE / "node-switch-log.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def set_app_available(self, available):
        if available is None or available == self.app_available:
            return
        LOG.info("Codex availability changed %s -> %s", self.app_available, available)
        if available:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.update_idletasks()
        else:
            self.root.withdraw()
        self.app_available = available
        if available:
            self.ensure_visible(force=True)

    def observe_app_available(self, available):
        # One transient negative result must not hide the entire paw. Unknown
        # results preserve visibility and interrupt the consecutive-miss count.
        if available is False:
            self.app_absent_samples += 1
            if self.app_absent_samples < 3:
                return
        else:
            self.app_absent_samples = 0
        self.set_app_available(available)

    def tick(self):
        if self.stop.is_set():
            return
        try:
            self.refresh()
        except Exception:
            log_exception("UI refresh", *sys.exc_info())
        finally:
            if not self.stop.is_set():
                try:
                    self.ensure_visible()
                except Exception:
                    log_exception("Visibility recovery", *sys.exc_info())
                self.root.after(500, self.tick)

    def refresh(self):
        while True:
            try:
                kind, data = self.updates.get_nowait()
            except queue.Empty:
                break
            if kind == "availability":
                self.observe_app_available(data)
            elif kind == "failover":
                self.switch_message = data
            elif kind == "sessions":
                self.snapshot, self.running, error = data
                self.monitor_error = error
            elif kind == "error":
                self.monitor_error = f"状态读取失败：{data}"
            else:
                self.probes[kind] = data
        self.probe_enabled = self.network_on.get() and (not self.follow_codex or self.app_available)
        self.switch_enabled = self.auto_switch.get() and not self.demo
        self.mapping = {"自动 · 最近有活动的聊天": None}
        self.mapping.update({f"{s.title[:26]} · {s.id[-6:]}": s.id for s in self.snapshot})
        self.combo["values"] = list(self.mapping)
        desired = next((label for label, sid in self.mapping.items() if sid == self.selected), next(iter(self.mapping)))
        self.combo.set(desired)
        if self.selected and self.selected not in {s.id for s in self.snapshot}:
            self.selected = None
        s = next((s for s in self.snapshot if s.id == self.selected), None) if self.selected else next(iter(self.snapshot), None)
        now = time.time()
        active = [x for x in self.snapshot if x.active is True]
        latest_active = max(active, key=lambda x: x.last_activity, default=None)
        self.switch_context = None
        if (self.switch_enabled and self.probe_enabled and self.running is True
                and not getattr(self, "monitor_error", "") and latest_active
                and 0 < now - latest_active.turn_started < 86400
                and latest_active.last_activity and now - latest_active.last_activity >= self.warn):
            self.switch_context = (latest_active.id, latest_active.turn_started, latest_active.last_activity)
        result = self.probes.get("codex", {})
        fresh = now - result.get("at", 0) < 45
        failed = self.probe_enabled and fresh and result.get("ok") is False
        status, color = classify(s, now, self.warn, self.severe, self.running is not False, failed)
        self.status.config(text="●  " + status, fg=color)
        self.tab_color = color
        self.task_count = sum(x.active is True for x in self.snapshot) if self.running is not False else 0
        seconds = max(0, int(now - s.last_activity)) if s and s.last_activity else None
        self.tab_seconds = "—" if seconds is None else (f"{seconds}s" if seconds <= 9999 else "9999+s")
        self.draw_tab()
        self.header_label.config(text="◉  CODEX 模拟测试" if self.demo else "◉  CODEX MONITOR", fg="#e4ebf5")
        self.age.config(text="最近活动：" + (duration(now - s.last_activity) + "前" if s and s.last_activity else "暂无记录"))
        self.action.config(text=(s.action[:42] if s else "未发现聊天记录") + (f" · {self.task_count} 个任务进行中"))
        if not self.probe_enabled:
            self.net.config(text="网络：探测已关闭")
            self.general.config(text="")
        else:
            self.net.config(text="Codex：" + result.get("text", "等待首次探测…") + (" · 已过期" if result and not fresh else ""))
            other = self.probes.get("general", {})
            self.general.config(text="外网：" + other.get("text", "等待探测…") + (" · 代理已配置" if result.get("proxy") else ""))
        error = getattr(self, "monitor_error", "")
        if error:
            self.hint.config(text=error, fg="#f4c567")
        else:
            self.hint.config(text=f"无活动 {self.warn} 秒提醒 / {self.severe} 秒加重 · 不代表已卡死", fg="#97a8c0")
        self.failover_label.config(text=self.switch_message if self.switch_enabled else "自动换节点：已关闭")
        if self.detail.get():
            self.fit_details()
        self.update_auto_collapse()

    def close(self):
        LOG.info("Window closed by request demo=%s", self.demo)
        self.stop.set()
        settings = {"warn_seconds": self.warn, "severe_seconds": self.severe, "x": self.root.winfo_x(), "y": self.root.winfo_y(), "dock_side": self.dock_side, "dock_y": self.root.winfo_y(), "network_probe": self.network_on.get(), "auto_switch": self.auto_switch.get()}
        if not self.demo:
            try:
                CONFIG.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            except OSError:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def self_test(home):
    """Exercise the actual Tk rendering without changing any live Codex records."""
    window = Window(home, demo=True)
    session = Session("self-test", Path("unused"), "本地模拟测试 · 不修改真实会话")
    window.snapshot = [session]
    window.running = True
    window.network_on.set(False)
    report = []
    stages = [
        ("运行", 1, True, False, "运行中", "#62d6ac"),
        ("无活动提醒", window.warn + 2, True, False, "持续无新活动", "#f4c567"),
        ("长时间无活动", window.severe + 2, True, False, "长时间无新活动", "#ff7b79"),
        ("连接异常", window.warn + 2, True, True, "网络探测失败", "#ff7b79"),
        ("活动恢复", 1, True, False, "运行中", "#62d6ac"),
        ("任务完成", window.severe + 2, False, False, "空闲", "#8b95a7"),
    ]

    def stage(index):
        name, age, active, network_failed, expected, color = stages[index]
        session.active = active
        session.last_activity = time.time() - age
        session.action = "模拟场景：" + name
        window.network_on.set(network_failed)
        if network_failed:
            window.probes["codex"] = {"ok": False, "text": "模拟连接失败（非真实故障）", "at": time.time()}
        window.root.after(800, lambda: verify(index))

    def verify(index):
        name, _, _, _, expected, color = stages[index]
        actual = window.status.cget("text")
        passed = (expected in actual and window.status.cget("fg") == color
                  and window.compact.itemcget("seconds", "fill") == color
                  and window.compact.itemcget("seconds", "text").endswith("s"))
        report.append({"test": name, "passed": passed, "display": actual, "color": window.status.cget("fg")})
        print(json.dumps(report[-1], ensure_ascii=False), flush=True)
        if index + 1 < len(stages):
            window.root.after(1800, lambda: stage(index + 1))
        else:
            window.root.after(1800, finish)

    def finish():
        window.root.update_idletasks()
        paw_items = window.compact.find_all()
        window.draw_tab()
        collapsed = window.root.winfo_width() == TAB_WIDTH and window.root.winfo_height() == TAB_HEIGHT and window.compact.winfo_ismapped() and not window.body.winfo_ismapped()
        position = (window.root.winfo_x(), window.root.winfo_y())
        window.collapse()
        window.root.update_idletasks()
        fits = window.root.winfo_width() == 320 and window.root.winfo_height() >= 350 and all(child.winfo_y() + child.winfo_height() <= window.body.winfo_height() for child in window.body.winfo_children())
        stable_transparency = os.name != "nt" or str(window.root.attributes("-transparentcolor")).lower() == "#ff00ff"
        same_position = (position[0] == window.root.winfo_x() if window.dock_side == "left" else position[0] + TAB_WIDTH == window.root.winfo_x() + 320)
        window.collapse()
        window.root.update_idletasks()
        returned = window.root.winfo_width() == TAB_WIDTH and window.root.winfo_height() == TAB_HEIGHT and window.compact.winfo_ismapped()
        report.append({"test": "边缘挂签、展开面板与收回", "passed": bool(collapsed and fits and same_position and returned)})
        report.append({"test": "猫爪复用图形、展开保持透明色", "passed": bool(paw_items == window.compact.find_all() and stable_transparency)})
        window.app_available = True
        window.set_app_available(False)
        window.root.update_idletasks()
        hidden = window.root.state() == "withdrawn"
        window.set_app_available(True)
        window.root.update_idletasks()
        report.append({"test": "跟随 Codex 隐藏和重新显示", "passed": hidden and window.root.state() == "normal" and window.root.winfo_width() == TAB_WIDTH})
        window.set_view(True)
        window.root.update_idletasks()
        window.update_auto_collapse(now=0, focused=False)
        window.update_auto_collapse(now=59.9, focused=False)
        stayed = window.detail.get()
        window.update_auto_collapse(now=60, focused=True)
        reset = window.unfocused_since is None and window.detail.get()
        window.update_auto_collapse(now=100, focused=False)
        window.update_auto_collapse(now=159.9, focused=False)
        stayed = stayed and window.detail.get()
        window.update_auto_collapse(now=160, focused=False)
        report.append({"test": "失焦60秒收起、回焦重新计时", "passed": bool(stayed and reset and not window.detail.get())})
        pad_checks = []
        for count in (0, 1, 2, 3, 4, 5, 7, 4, 2, 1, 0):
            window.task_count = count
            window.draw_tab()
            fills = [window.compact.itemcget(f"task-pad-{i}", "fill") for i in range(5)]
            pad_checks.append(fills == [PAD_COLOR if count > i else "" for i in range(5)])
        report.append({"test": "任务数依次填充掌心和左至右脚趾、减少时熄灭", "passed": all(pad_checks) and paw_items == window.compact.find_all()})
        actual_work_area = window.work_area
        left, top, right, bottom = actual_work_area()
        simulated = (left, top, left + min(1280, right-left), top + min(680, bottom-top))
        window.work_area = lambda: simulated
        window.ensure_visible(force=True)
        window.root.update_idletasks()
        recovered = (window.root.winfo_x() >= simulated[0] and window.root.winfo_y() >= simulated[1]
                     and window.root.winfo_x() + window.root.winfo_width() <= simulated[2]
                     and window.root.winfo_y() + window.root.winfo_height() <= simulated[3])
        window.work_area = actual_work_area
        window.ensure_visible(force=True)
        report.append({"test": "模拟分辨率缩小后猫爪返回可见区域", "passed": recovered})
        (BASE / "self-test-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()

    window.root.after(500, lambda: stage(0))
    window.run()
    return len(report) == 12 and all(row["passed"] for row in report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--once", action="store_true", help="Print a diagnostic snapshot without a window")
    parser.add_argument("--probe", action="store_true", help="Include HTTPS probes in --once")
    parser.add_argument("--self-test", action="store_true", help="Show simulated UI states, then save a test report and close")
    parser.add_argument("--output", type=Path, help="Save --once diagnostics as JSON (useful for a windowed EXE)")
    parser.add_argument("--follow-codex", action="store_true", help="Wait in the background and show only while the Codex desktop app is running")
    args = parser.parse_args()
    if args.self_test:
        import sys
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        raise SystemExit(0 if self_test(args.home) else 1)
    elif args.once:
        import sys
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        monitor = Monitor(args.home)
        sessions = monitor.poll()
        now = time.time()
        running = codex_running()
        result = json.dumps({"process_running": running, "read_error": monitor.read_error, "sessions": [{"id": s.id, "active": s.active, "age_seconds": round(now-s.last_activity) if s.last_activity else None, "state": classify(s, now, running=running is not False)[0], "action": s.action} for s in sessions[:8]], "network": probe("https://chatgpt.com/backend-api/codex") if args.probe else "disabled"}, ensure_ascii=False, indent=2)
        if args.output or sys.stdout is None:
            (args.output or BASE / "diagnostic-report.json").write_text(result, encoding="utf-8")
        else:
            print(result)
    else:
        with single_instance() as acquired:
            if acquired:
                Window(args.home, follow_codex=args.follow_codex).run()


if __name__ == "__main__":
    setup_logging()
    LOG.info("Starting pid=%s frozen=%s follow_codex=%s", os.getpid(),
             bool(getattr(sys, "frozen", False)), "--follow-codex" in sys.argv)
    main()
    LOG.info("Main returned normally")
