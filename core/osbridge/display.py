r"""Find the physical display that actually has the webcam above it.

Why this is not just "the primary screen": on a docked laptop the primary
display is usually the big external monitor, and an island anchored there
is anchored under nothing. The illusion only works on the built-in panel.

Windows can answer this exactly. ``QueryDisplayConfig`` reports an output
technology per active path, and ``DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL``
marks the built-in panel. We then resolve that path to its GDI device name
(``\\.\DISPLAY1``), which is what ``QScreen.name()`` returns, so the
result can be matched back to a Qt screen.

Everything here degrades safely: any API failure returns ``None`` and the
caller falls back to the primary screen.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_uint32, c_void_p, sizeof, wintypes

# -- constants -------------------------------------------------------------

QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL = 0x80000000
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_DISPLAYPORT_EMBEDDED = 0x00000003
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_UDI_EMBEDDED = 0x0000000B
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
CCHDEVICENAME = 32
ERROR_SUCCESS = 0

# Embedded panels show up under more than one technology id depending on how
# the OEM wired the panel, so treat all three as "built in".
_INTERNAL_TECHNOLOGIES = {
    DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL,
    DISPLAYCONFIG_OUTPUT_TECHNOLOGY_DISPLAYPORT_EMBEDDED,
    DISPLAYCONFIG_OUTPUT_TECHNOLOGY_UDI_EMBEDDED,
}


# -- structures ------------------------------------------------------------


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("statusFlags", c_uint32),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", c_uint32), ("Denominator", c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("outputTechnology", c_uint32),
        ("rotation", c_uint32),
        ("scaling", c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", c_uint32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", c_uint32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # The union payload is 48 bytes (DISPLAYCONFIG_VIDEO_SIGNAL_INFO is the
    # largest member). We never read it; we only have to allocate it so that
    # QueryDisplayConfig has somewhere to write.
    _fields_ = [
        ("infoType", c_uint32),
        ("id", c_uint32),
        ("adapterId", LUID),
        ("_union", ctypes.c_byte * 48),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", c_uint32),
        ("size", c_uint32),
        ("adapterId", LUID),
        ("id", c_uint32),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", wintypes.WCHAR * CCHDEVICENAME),
    ]


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetDisplayConfigBufferSizes.argtypes = [
        c_uint32,
        POINTER(c_uint32),
        POINTER(c_uint32),
    ]
    u.GetDisplayConfigBufferSizes.restype = wintypes.LONG
    u.QueryDisplayConfig.argtypes = [
        c_uint32,
        POINTER(c_uint32),
        POINTER(DISPLAYCONFIG_PATH_INFO),
        POINTER(c_uint32),
        POINTER(DISPLAYCONFIG_MODE_INFO),
        c_void_p,
    ]
    u.QueryDisplayConfig.restype = wintypes.LONG
    u.DisplayConfigGetDeviceInfo.argtypes = [
        POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
    ]
    u.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
    return u


def internal_display_device_names() -> list[str]:
    """GDI device names of every active built-in panel, best effort.

    Returns an empty list when the query fails or the machine genuinely has
    no internal panel (a desktop PC), which the caller must handle.
    """
    try:
        u = _user32()
    except OSError:
        return []

    n_paths = c_uint32(0)
    n_modes = c_uint32(0)
    if u.GetDisplayConfigBufferSizes(
        QDC_ONLY_ACTIVE_PATHS, byref(n_paths), byref(n_modes)
    ) != ERROR_SUCCESS:
        return []
    if n_paths.value == 0:
        return []

    paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
    modes = (DISPLAYCONFIG_MODE_INFO * max(n_modes.value, 1))()
    if u.QueryDisplayConfig(
        QDC_ONLY_ACTIVE_PATHS,
        byref(n_paths),
        paths,
        byref(n_modes),
        modes,
        None,
    ) != ERROR_SUCCESS:
        return []

    found: list[str] = []
    for i in range(n_paths.value):
        path = paths[i]
        if path.targetInfo.outputTechnology not in _INTERNAL_TECHNOLOGIES:
            continue
        req = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        req.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        req.header.size = sizeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME)
        req.header.adapterId = path.sourceInfo.adapterId
        req.header.id = path.sourceInfo.id
        if u.DisplayConfigGetDeviceInfo(byref(req.header)) == ERROR_SUCCESS:
            name = req.viewGdiDeviceName
            if name and name not in found:
                found.append(name)
    return found


def describe_displays() -> str:
    """Human-readable dump for the tray diagnostics panel."""
    names = internal_display_device_names()
    if not names:
        return "internal panel: not detected (will use primary screen)"
    return "internal panel: " + ", ".join(names)
