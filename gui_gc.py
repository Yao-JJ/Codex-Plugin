"""Keep cyclic finalization of Tcl/Tk objects on the GUI's owning thread."""
import gc
import threading
import time


class GuiGarbageCollector:
    def __enter__(self):
        self.check_thread()
        self.was_enabled = gc.isenabled()
        gc.disable()
        self.last_collection = time.monotonic()
        self.collections = 0
        return self

    @staticmethod
    def check_thread():
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("GUI garbage collection must run on the main thread")

    def collect(self, force=False):
        self.check_thread()
        now = time.monotonic()
        if force or now - self.last_collection >= 10:
            # Disabling automatic GC does not disable reference counting. Cycles
            # are still reclaimed, here on the thread that owns Tcl/Tk.
            gc.collect()
            self.last_collection = now
            self.collections += 1

    def __exit__(self, exc_type, exc_value, tb):
        try:
            self.collect(force=True)
        finally:
            if self.was_enabled:
                gc.enable()
