"""Process-lifetime Win32 bindings; never create ctypes types in polling loops."""
import ctypes
from ctypes import wintypes
import os


class ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class WindowsAPI:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.bind(self.kernel.CreateToolhelp32Snapshot, [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
        self.bind(self.kernel.Process32FirstW, [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL)
        self.bind(self.kernel.Process32NextW, [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL)
        self.bind(self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
        self.bind(self.kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
        self.bind(self.kernel.QueryFullProcessImageNameW,
                  [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
        self.bind(self.kernel.CreateMutexW, [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)
        self.bind(self.kernel.GetCurrentProcess, [], wintypes.HANDLE)
        self.bind(self.kernel.GetProcessHandleCount, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
        self.bind(self.user.SystemParametersInfoW,
                  [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT], wintypes.BOOL)
        self.bind(self.user.GetForegroundWindow, [], wintypes.HWND)
        self.bind(self.user.GetWindowThreadProcessId,
                  [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)

    @staticmethod
    def bind(function, argtypes, restype):
        function.argtypes = argtypes
        function.restype = restype

    def handle_count(self):
        count = wintypes.DWORD()
        if self.kernel.GetProcessHandleCount(self.kernel.GetCurrentProcess(), ctypes.byref(count)):
            return count.value
        return None


# Import/initialization happens on the main thread, before starting workers.
API = WindowsAPI() if os.name == "nt" else None
