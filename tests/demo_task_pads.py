"""Visible 2/3/4/5-task demonstration using the production monitor widgets."""
import json
from pathlib import Path
import sys
import time
import tkinter as tk

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import codex_monitor as monitor

monitor.CONFIG = BASE / "dist" / "monitor-settings.json"
window = monitor.Window(Path.home() / ".codex", demo=True)
window.running = True
window.network_on.set(False)
badge = tk.Toplevel(window.root)
badge.overrideredirect(True)
badge.attributes("-topmost", True)
label = tk.Label(badge, bg="#202b3c", fg="#c9d6e8", padx=10, pady=6,
                 font=("Microsoft YaHei UI", 10))
label.pack()
results = []


def stage(count):
    now = time.time()
    window.snapshot = [monitor.Session(f"demo-{i}", Path("unused"),
                                      f"模拟任务 {i+1}", active=True,
                                      last_activity=now, action="模拟并行任务")
                       for i in range(count)]
    window.set_view(False)
    window.root.update_idletasks()
    label.config(text=f"模拟测试：{count} 项任务同时进行")
    badge.update_idletasks()
    left, top, right, bottom = window.work_area()
    width, height = badge.winfo_reqwidth(), badge.winfo_reqheight()
    x = (window.root.winfo_x() - width - 10 if window.dock_side == "right"
         else window.root.winfo_x() + monitor.TAB_WIDTH + 10)
    y = max(top, min(window.root.winfo_y() + 18, bottom-height))
    badge.geometry(f"{width}x{height}{max(left, min(x, right-width)):+d}{y:+d}")
    window.root.after(1200, lambda: verify(count))
    window.root.after(5000, lambda: stage(count+1) if count < 5 else finish())


def verify(count):
    fills = [window.compact.itemcget(f"task-pad-{i}", "fill") for i in range(5)]
    expected = [monitor.PAD_COLOR if count > i else "" for i in range(5)]
    results.append({"tasks": count, "count_detected": window.task_count,
                    "pink_pads": sum(fill == monitor.PAD_COLOR for fill in fills),
                    "passed": fills == expected and window.task_count == count})


def finish():
    (BASE / "dist" / "task-pad-demo-report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    window.close()


window.root.after(300, lambda: stage(2))
window.run()
raise SystemExit(0 if len(results) == 4 and all(r["passed"] for r in results) else 1)
