"""Isolated UI/native-poll regression test used by the packaged executable."""
import ctypes
import gc
import json
import threading
import time
import tracemalloc

from windows_api import API


def run_stress_test(window_class, detect, home, collector, seconds, output):
    tracemalloc.start()
    window = window_class(home, demo=True, gc_guard=collector)
    window.network_on.set(False)
    # Warm up both views before measuring retained allocations/handles.
    window.set_view(True)
    window.root.update_idletasks()
    window.set_view(False)
    window.root.update_idletasks()
    # Windows input-method/font services initialize lazily on the first real
    # event-loop iterations. Include that startup cost before the leak baseline.
    window.root.after(2000, window.root.quit)
    window.root.mainloop()
    collector.collect(force=True)
    cache = getattr(ctypes, "_pointer_type_cache", {})
    before = {"pointer_types": len(cache), "handles": API.handle_count() if API else None,
              "python_bytes": tracemalloc.get_traced_memory()[0]}
    stats = {"native_polls": 0, "gui_cycles": 0, "finalizers": 0, "restores": 0}
    errors, gc_threads, finalizer_threads = [], set(), set()
    main_thread = threading.get_ident()

    class Cycle:
        def __init__(self):
            self.cycle = self

        def __del__(self):
            finalizer_threads.add(threading.get_ident())
            stats["finalizers"] += 1

    def on_gc(phase, info):
        if phase == "start":
            gc_threads.add(threading.get_ident())

    def native_worker():
        try:
            while not window.stop.is_set():
                detect(desktop_only=True)
                detect()
                stats["native_polls"] += 2
                Cycle()
                window.stop.wait(0.002)
        except Exception as exc:
            errors.append(type(exc).__name__)

    def callback_error(exc_type, exc, tb):
        errors.append("Tk callback: " + exc_type.__name__)
        window.close()

    window.root.report_callback_exception = callback_error
    gc.callbacks.append(on_gc)
    started = time.monotonic()
    worker = threading.Thread(target=native_worker, name="stress_native_poll", daemon=True)
    window.workers.append(worker)
    worker.start()
    last_collection = started
    was_visible = False

    def step():
        nonlocal last_collection, was_visible
        stats["gui_cycles"] += 1
        index = stats["gui_cycles"]
        # Repeatedly create/destroy Tcl widgets while the native thread allocates.
        label = window.tk.Label(window.root, text="stress")
        label.destroy()
        if index % 20 == 0:
            window.set_view(not window.detail.get())
        if index % 30 == 0:
            window.root.withdraw()
            window.ensure_visible()
            window.root.update_idletasks()
            if window.root.state() == "normal":
                stats["restores"] += 1
        now = time.monotonic()
        if now - last_collection >= 1:
            collector.collect(force=True)
            last_collection = now
        if now - started >= seconds:
            window.root.update_idletasks()
            was_visible = bool(window.root.winfo_viewable())
            window.close()
        else:
            window.root.after(100, step)

    window.root.after(100, step)
    try:
        window.run()
        collector.collect(force=True)
        after = {"pointer_types": len(cache), "handles": API.handle_count() if API else None,
                 "python_bytes": tracemalloc.get_traced_memory()[0]}
    finally:
        gc.callbacks.remove(on_gc)
        tracemalloc.stop()
    elapsed = time.monotonic() - started
    checks = {
        "completed": elapsed >= seconds and stats["native_polls"] >= 200,
        "pointer_types_stable": after["pointer_types"] == before["pointer_types"],
        "handles_bounded": before["handles"] is None or after["handles"] <= before["handles"] + 32,
        "python_memory_bounded": after["python_bytes"] - before["python_bytes"] < 8 * 1024 * 1024,
        "gc_on_main_thread": gc_threads == {main_thread},
        "finalizers_on_main_thread": finalizer_threads == {main_thread} and stats["finalizers"] > 0,
        "window_recovered": stats["restores"] > 0 and was_visible,
        "no_callback_or_worker_errors": not errors,
    }
    report = {"passed": all(checks.values()), "seconds": round(elapsed, 2),
              "stats": stats, "before": before, "after": after, "checks": checks, "errors": errors}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["passed"]
