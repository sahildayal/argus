"""Thin ctypes layer over the Win32 calls Argus needs.

Importing this module makes the process per-monitor DPI aware (v2), so every
coordinate Argus reads and every pixel it captures is physical. It has to run
before mss, tkinter or anything else touches the screen, which is why
``argus/__init__.py`` does not import it lazily.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import uuid
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from .geometry import Rect

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("Argus only runs on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shcore = ctypes.WinDLL("shcore")
shell32 = ctypes.WinDLL("shell32")
ole32 = ctypes.WinDLL("ole32")
version_dll = ctypes.WinDLL("version")

HWND = wintypes.HWND
LRESULT = ctypes.c_ssize_t


def _fn(dll, name: str, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


# --------------------------------------------------------------------------- DPI

_SetProcessDpiAwarenessContext = _fn(user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, ctypes.c_void_p)
_GetThreadDpiAwarenessContext = _fn(user32, "GetThreadDpiAwarenessContext", ctypes.c_void_p)
_GetAwarenessFromDpiAwarenessContext = _fn(user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, ctypes.c_void_p)


def _make_dpi_aware() -> str:
    if _SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
        return "per-monitor-v2"
    try:
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "per-monitor"
    except OSError:
        pass
    # Already set by a manifest or an earlier call: report what we actually have.
    awareness = _GetAwarenessFromDpiAwarenessContext(_GetThreadDpiAwarenessContext())
    return {0: "unaware", 1: "system", 2: "per-monitor"}.get(awareness, "unknown")


DPI_AWARENESS = _make_dpi_aware()


# --------------------------------------------------------------------- structs


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def to_rect(self) -> Rect:
        return Rect.ltrb(self.left, self.top, self.right, self.bottom)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

    @classmethod
    def parse(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


# DisplayConfig structures (friendly monitor names and built-in detection).


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # The trailing 48-byte union is never read, only sized correctly.
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID), ("info", ctypes.c_uint64 * 6)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


# ------------------------------------------------------------------- constants

GWL_STYLE = -16
GWL_EXSTYLE = -20
GW_OWNER = 4
GA_ROOT = 2
GA_ROOTOWNER = 3

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_CHILD = 0x40000000

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
LWA_ALPHA = 0x2
PW_RENDERFULLCONTENT = 0x2
MONITOR_DEFAULTTONEAREST = 2
QDC_ONLY_ACTIVE_PATHS = 2
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY values that mean "panel built into the device".
BUILTIN_OUTPUT_TECH = {0x80000000, 11, 13}  # INTERNAL, DISPLAYPORT_EMBEDDED, UDI_EMBEDDED

FOLDERID_SCREENSHOTS = "{b7bede81-df94-4682-a7d8-57a52620b86f}"
FOLDERID_PICTURES = "{33E28130-4E1E-4676-835A-98395C3BC3BB}"

# ------------------------------------------------------------------ prototypes

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

EnumWindows = _fn(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
EnumChildWindows = _fn(user32, "EnumChildWindows", wintypes.BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
GetWindowTextLengthW = _fn(user32, "GetWindowTextLengthW", ctypes.c_int, HWND)
GetWindowTextW = _fn(user32, "GetWindowTextW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
GetClassNameW = _fn(user32, "GetClassNameW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
IsWindow = _fn(user32, "IsWindow", wintypes.BOOL, HWND)
IsWindowVisible = _fn(user32, "IsWindowVisible", wintypes.BOOL, HWND)
IsIconic = _fn(user32, "IsIconic", wintypes.BOOL, HWND)
IsZoomed = _fn(user32, "IsZoomed", wintypes.BOOL, HWND)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wintypes.DWORD, HWND, ctypes.POINTER(wintypes.DWORD))
GetWindowLongW = _fn(user32, "GetWindowLongW", wintypes.LONG, HWND, ctypes.c_int)
SetWindowLongW = _fn(user32, "SetWindowLongW", wintypes.LONG, HWND, ctypes.c_int, wintypes.LONG)
GetWindow = _fn(user32, "GetWindow", HWND, HWND, wintypes.UINT)
GetAncestor = _fn(user32, "GetAncestor", HWND, HWND, wintypes.UINT)
GetForegroundWindow = _fn(user32, "GetForegroundWindow", HWND)
WindowFromPoint = _fn(user32, "WindowFromPoint", HWND, POINT)
GetCursorPos = _fn(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(POINT))
GetWindowRect = _fn(user32, "GetWindowRect", wintypes.BOOL, HWND, ctypes.POINTER(RECT))
GetLayeredWindowAttributes = _fn(
    user32,
    "GetLayeredWindowAttributes",
    wintypes.BOOL,
    HWND,
    ctypes.POINTER(wintypes.COLORREF),
    ctypes.POINTER(wintypes.BYTE),
    ctypes.POINTER(wintypes.DWORD),
)
EnumDisplayMonitors = _fn(user32, "EnumDisplayMonitors", wintypes.BOOL, wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM)
GetMonitorInfoW = _fn(user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
GetDpiForMonitor = _fn(shcore, "GetDpiForMonitor", ctypes.c_long, wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT))
DwmGetWindowAttribute = _fn(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
PrintWindow = _fn(user32, "PrintWindow", wintypes.BOOL, HWND, wintypes.HDC, wintypes.UINT)
GetDC = _fn(user32, "GetDC", wintypes.HDC, HWND)
ReleaseDC = _fn(user32, "ReleaseDC", ctypes.c_int, HWND, wintypes.HDC)
CreateCompatibleDC = _fn(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
DeleteDC = _fn(gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
CreateDIBSection = _fn(
    gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
)
SelectObject = _fn(gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
DeleteObject = _fn(gdi32, "DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ)
GdiFlush = _fn(gdi32, "GdiFlush", wintypes.BOOL)
GetConsoleWindow = _fn(kernel32, "GetConsoleWindow", HWND)
GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wintypes.DWORD)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
CreateMutexW = _fn(kernel32, "CreateMutexW", wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
CloseHandle = _fn(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", wintypes.DWORD)
AddClipboardFormatListener = _fn(user32, "AddClipboardFormatListener", wintypes.BOOL, HWND)
SetWinEventHook = _fn(
    user32, "SetWinEventHook", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
    WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
)
UnhookWinEvent = _fn(user32, "UnhookWinEvent", wintypes.BOOL, wintypes.HANDLE)
GetMessageW = _fn(user32, "GetMessageW", ctypes.c_int, ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT)
TranslateMessage = _fn(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
PostThreadMessageW = _fn(user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
RegisterHotKey = _fn(user32, "RegisterHotKey", wintypes.BOOL, HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
UnregisterHotKey = _fn(user32, "UnregisterHotKey", wintypes.BOOL, HWND, ctypes.c_int)
DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
CreateWindowExW = _fn(
    user32, "CreateWindowExW", HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetDisplayConfigBufferSizes = _fn(user32, "GetDisplayConfigBufferSizes", ctypes.c_long, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32))
QueryDisplayConfig = _fn(
    user32, "QueryDisplayConfig", ctypes.c_long, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(DISPLAYCONFIG_PATH_INFO), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p,
)
DisplayConfigGetDeviceInfo = _fn(user32, "DisplayConfigGetDeviceInfo", ctypes.c_long, ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER))
SHGetKnownFolderPath = _fn(shell32, "SHGetKnownFolderPath", ctypes.c_long, ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))
CoTaskMemFree = _fn(ole32, "CoTaskMemFree", None, ctypes.c_void_p)
GetFileVersionInfoSizeW = _fn(version_dll, "GetFileVersionInfoSizeW", wintypes.DWORD, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
GetFileVersionInfoW = _fn(version_dll, "GetFileVersionInfoW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
VerQueryValueW = _fn(version_dll, "VerQueryValueW", wintypes.BOOL, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


RegisterClassW = _fn(user32, "RegisterClassW", wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
HWND_MESSAGE = -3
WM_HOTKEY = 0x0312
WM_CLIPBOARDUPDATE = 0x031D
WM_QUIT = 0x0012
WM_APP = 0x8000
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000


def hwnd_int(h) -> int:
    """Normalise an HWND (ctypes int / None) to a plain int (0 for null)."""
    return int(h or 0)


# --------------------------------------------------------------------- helpers


def window_text(hwnd: int) -> str:
    n = GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def ex_style(hwnd: int) -> int:
    return GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF


def style(hwnd: int) -> int:
    return GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF


def is_cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD()
    hr = DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0 and value.value != 0


def window_rect(hwnd: int) -> Rect:
    r = RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r.to_rect()


def frame_rect(hwnd: int) -> Rect:
    """The visible frame, without the invisible resize borders Windows 10/11 add."""
    r = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return r.to_rect()
    return window_rect(hwnd)


def layered_alpha(hwnd: int) -> int | None:
    """Constant alpha of a layered window, or None if it doesn't use one."""
    alpha, flags = wintypes.BYTE(), wintypes.DWORD()
    if GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)) and flags.value & LWA_ALPHA:
        return alpha.value & 0xFF
    return None


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def root_window_at(x: int, y: int) -> int:
    h = hwnd_int(WindowFromPoint(POINT(x, y)))
    return hwnd_int(GetAncestor(h, GA_ROOT)) if h else 0


def owner(hwnd: int) -> int:
    return hwnd_int(GetWindow(hwnd, GW_OWNER))


def top_level_windows() -> list[int]:
    """All top-level windows, front to back."""
    found: list[int] = []

    def cb(h, _lparam):
        found.append(hwnd_int(h))
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return found


def child_windows(hwnd: int) -> list[int]:
    found: list[int] = []

    def cb(h, _lparam):
        found.append(hwnd_int(h))
        return True

    EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return found


def monitors_raw() -> list[tuple[int, str, Rect, Rect, bool, int]]:
    """(hmonitor, device, rect, work_rect, primary, dpi) for each active monitor."""
    out: list[tuple[int, str, Rect, Rect, bool, int]] = []

    def cb(hmon, _hdc, _rect, _lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if GetMonitorInfoW(hmon, ctypes.byref(mi)):
            dx, dy = wintypes.UINT(), wintypes.UINT()
            dpi = dx.value if GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 else 96
            out.append((int(hmon), mi.szDevice, mi.rcMonitor.to_rect(), mi.rcWork.to_rect(), bool(mi.dwFlags & 1), dpi or 96))
        return True

    EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


def display_names() -> dict[str, tuple[str, int]]:
    """Map a GDI device name (``\\\\.\\DISPLAY1``) to (monitor friendly name, output technology)."""
    npaths, nmodes = ctypes.c_uint32(), ctypes.c_uint32()
    for _ in range(3):
        if GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(npaths), ctypes.byref(nmodes)) != 0:
            return {}
        paths = (DISPLAYCONFIG_PATH_INFO * max(npaths.value, 1))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(nmodes.value, 1))()
        rc = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(npaths), paths, ctypes.byref(nmodes), modes, None)
        if rc == 0:
            break
        if rc != ERROR_INSUFFICIENT_BUFFER:  # a monitor was plugged in between the two calls: retry
            return {}
    else:
        return {}

    out: dict[str, tuple[str, int]] = {}
    for path in paths[: npaths.value]:
        src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        src.header.type = 1  # DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        src.header.size = ctypes.sizeof(src)
        src.header.adapterId = path.sourceInfo.adapterId
        src.header.id = path.sourceInfo.id
        if DisplayConfigGetDeviceInfo(ctypes.byref(src.header)) != 0:
            continue
        tgt = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        tgt.header.type = 2  # DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        tgt.header.size = ctypes.sizeof(tgt)
        tgt.header.adapterId = path.targetInfo.adapterId
        tgt.header.id = path.targetInfo.id
        name, tech = "", path.targetInfo.outputTechnology
        if DisplayConfigGetDeviceInfo(ctypes.byref(tgt.header)) == 0:
            name, tech = tgt.monitorFriendlyDeviceName, tgt.outputTechnology
        out.setdefault(src.viewGdiDeviceName, (name.strip(), tech))  # cloned outputs: first one wins
    return out


def print_window(hwnd: int) -> tuple[bytes, int, int] | None:
    """Render a window offscreen (works when it's covered). Returns BGRX bytes + size
    of the full window rect, or None when the app refuses to draw."""
    wr = window_rect(hwnd)
    w, h = wr.w, wr.h
    if w <= 0 or h <= 0 or w * h > 16384 * 16384:
        return None
    screen_dc = GetDC(None)
    mem_dc = CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h  # negative height = top-down rows
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
    bits = ctypes.c_void_p()
    bitmap = CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    try:
        if not bitmap or not bits.value:
            return None
        previous = SelectObject(mem_dc, bitmap)
        ok = PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        GdiFlush()
        data = ctypes.string_at(bits.value, w * h * 4) if ok else None
        SelectObject(mem_dc, previous)
    finally:
        if bitmap:
            DeleteObject(bitmap)
        DeleteDC(mem_dc)
        ReleaseDC(None, screen_dc)
    return (data, w, h) if data else None


def known_folder(guid: str) -> Path | None:
    g = GUID.parse(guid)
    p = ctypes.c_void_p()
    if SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return Path(ctypes.wstring_at(p.value))
    finally:
        CoTaskMemFree(p)


@lru_cache(maxsize=512)
def file_description(path: str) -> str:
    """The 'File description' shown in an exe's Properties (e.g. 'Google Chrome')."""
    if not path:
        return ""
    size = GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not GetFileVersionInfoW(path, 0, size, buf):
        return ""
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    codes = []
    if VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) and n.value >= 4:
        lang, codepage = struct.unpack("<HH", ctypes.string_at(ptr.value, 4))
        codes.append(f"{lang:04x}{codepage:04x}")
    codes += ["040904b0", "040904e4", "04090000"]
    for code in codes:
        if VerQueryValueW(buf, f"\\StringFileInfo\\{code}\\FileDescription", ctypes.byref(ptr), ctypes.byref(n)) and n.value:
            text = ctypes.wstring_at(ptr.value, n.value).rstrip("\0").strip()
            if text:
                return text
    return ""


def single_instance(name: str) -> wintypes.HANDLE | None:
    """Hold a named mutex for the life of the process; None if another instance has it."""
    handle = CreateMutexW(None, False, name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        CloseHandle(handle)
        return None
    return handle
