import ctypes
import gc
import os
import threading
import unittest
from unittest.mock import Mock, patch

import codex_monitor as monitor
from gui_gc import GuiGarbageCollector
from windows_api import ProcessEntry


@unittest.skipUnless(os.name == "nt", "Windows process enumeration")
class NativeLifetimeTests(unittest.TestCase):
    def kernel(self, rows):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 100
        entries = iter(rows)

        def advance(handle, pointer):
            try:
                name, pid = next(entries)
            except StopIteration:
                ctypes.set_last_error(18)
                return False
            entry = ctypes.cast(pointer, ctypes.POINTER(ProcessEntry)).contents
            entry.szExeFile, entry.th32ProcessID = name, pid
            return True
        kernel.Process32FirstW.side_effect = kernel.Process32NextW.side_effect = advance
        kernel.OpenProcess.return_value = 200
        return kernel

    def detect(self, kernel, desktop_only=True):
        with patch("codex_monitor.WIN32", Mock(kernel=kernel)):
            return monitor.codex_running(desktop_only=desktop_only)

    def test_structure_matches_windows_x64_and_x86_layout(self):
        expected = 568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556
        self.assertEqual(ctypes.sizeof(ProcessEntry), expected)

    def test_1000_polls_do_not_grow_pointer_type_cache(self):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = ctypes.c_void_p(-1).value
        cache = getattr(ctypes, "_pointer_type_cache", None)
        if cache is None:
            self.skipTest("This Python version does not expose the pointer cache")
        before = len(cache)
        with patch("codex_monitor.WIN32", Mock(kernel=kernel)), \
                patch("ctypes.WinDLL", side_effect=AssertionError("DLL rebound in poll")):
            for _ in range(1000):
                self.assertIsNone(monitor.codex_running())
        gc.collect()
        self.assertEqual(len(cache), before)
        kernel.CloseHandle.assert_not_called()

    def test_empty_snapshot_confirms_absence_and_closes_handle(self):
        kernel = self.kernel([])
        self.assertIs(self.detect(kernel), False)
        kernel.CloseHandle.assert_called_once_with(100)

    def test_cli_does_not_count_as_desktop(self):
        self.assertIs(self.detect(self.kernel([("codex.exe", 1)])), False)
        self.assertIs(self.detect(self.kernel([("codex.exe", 1)]), False), True)

    def test_codex_desktop_path_detected_and_both_handles_closed(self):
        kernel = self.kernel([("ChatGPT.exe", 1)])
        def query(handle, flags, buffer, size):
            buffer.value = r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe"
            return True
        kernel.QueryFullProcessImageNameW.side_effect = query
        self.assertIs(self.detect(kernel), True)
        self.assertEqual([call.args for call in kernel.CloseHandle.call_args_list], [(200,), (100,)])

    def test_unrelated_chatgpt_does_not_count_as_codex(self):
        kernel = self.kernel([("ChatGPT.exe", 1)])
        def query(handle, flags, buffer, size):
            buffer.value = r"C:\Program Files\ChatGPT\ChatGPT.exe"
            return True
        kernel.QueryFullProcessImageNameW.side_effect = query
        self.assertIs(self.detect(kernel), False)

    def test_inaccessible_path_keeps_state_unknown_and_releases_handles(self):
        kernel = self.kernel([("ChatGPT.exe", 1)])
        kernel.QueryFullProcessImageNameW.return_value = False
        self.assertIsNone(self.detect(kernel))
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_exception_still_releases_open_handles(self):
        kernel = self.kernel([("ChatGPT.exe", 1)])
        kernel.QueryFullProcessImageNameW.side_effect = OSError("test")
        with self.assertRaises(OSError):
            self.detect(kernel)
        self.assertEqual(kernel.CloseHandle.call_count, 2)


class GuiCollectionTests(unittest.TestCase):
    def test_worker_created_cycles_are_finalized_on_main_thread(self):
        finalized = []
        class Probe:
            def __del__(self):
                finalized.append(threading.get_ident())
        def allocate():
            for _ in range(2000):
                probe = Probe()
                probe.cycle = probe
        enabled = gc.isenabled()
        with GuiGarbageCollector() as guard:
            worker = threading.Thread(target=allocate)
            worker.start()
            worker.join()
            self.assertEqual(finalized, [])
            guard.collect(force=True)
            self.assertEqual(len(finalized), 2000)
            self.assertEqual(set(finalized), {threading.get_ident()})
        self.assertEqual(gc.isenabled(), enabled)

    def test_existing_disabled_gc_is_preserved(self):
        enabled = gc.isenabled()
        gc.disable()
        try:
            with GuiGarbageCollector():
                self.assertFalse(gc.isenabled())
            self.assertFalse(gc.isenabled())
        finally:
            if enabled:
                gc.enable()

    def test_guard_restores_gc_after_exception(self):
        enabled = gc.isenabled()
        with self.assertRaises(ValueError):
            with GuiGarbageCollector():
                raise ValueError("test")
        self.assertEqual(gc.isenabled(), enabled)

    def test_worker_cannot_run_gui_collection(self):
        errors = []
        with GuiGarbageCollector() as guard:
            def collect():
                try:
                    guard.collect(force=True)
                except RuntimeError:
                    errors.append(True)
            worker = threading.Thread(target=collect)
            worker.start()
            worker.join()
        self.assertEqual(errors, [True])

    def test_window_waits_for_workers_before_releasing_owner(self):
        window = monitor.Window.__new__(monitor.Window)
        window.root = Mock()
        window.stop = threading.Event()
        released = []
        worker = threading.Thread(target=lambda: (window.stop.wait(), released.append(True)))
        window.workers = [worker]
        worker.start()
        window.run()
        self.assertFalse(worker.is_alive())
        self.assertEqual(released, [True])


if __name__ == "__main__":
    unittest.main()
