"""
Cross-platform idle detector for distributed compute nodes.
Determines whether a workstation is currently idle based on:
1. User input inactivity (mouse/keyboard events via Windows GetLastInputInfo).
2. CPU utilization (preventing worker starvation if other tasks are running).
"""
from __future__ import annotations

import os
import platform
import time
from typing import Tuple


def get_user_idle_seconds() -> float:
    """
    Returns the number of seconds since the last physical user input (mouse/keyboard).
    Returns a large number (999999.0) on headless/non-interactive systems.
    """
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            import ctypes
            from ctypes import Structure, c_uint, sizeof, byref

            class LASTINPUTINFO(Structure):
                _fields_ = [("cbSize", c_uint), ("dwTime", c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return max(0.0, float(millis) / 1000.0)
        except Exception:
            pass

    elif sys_name == "linux":
        # Check X11 idle time if available via libXss
        try:
            import ctypes
            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            xss = ctypes.cdll.LoadLibrary("libXss.so.1")
            display = x11.XOpenDisplay(None)
            if display:
                class XScreenSaverInfo(ctypes.Structure):
                    _fields_ = [
                        ("window", ctypes.c_ulong),
                        ("state", ctypes.c_int),
                        ("kind", ctypes.c_int),
                        ("til_or_since", ctypes.c_ulong),
                        ("idle", ctypes.c_ulong),
                        ("eventMask", ctypes.c_ulong),
                    ]
                info = XScreenSaverInfo()
                root = x11.XDefaultRootWindow(display)
                xss.XScreenSaverQueryInfo(display, root, ctypes.byref(info))
                x11.XCloseDisplay(display)
                return max(0.0, float(info.idle) / 1000.0)
        except Exception:
            pass

    # Default fallback: assume idle if no desktop input mechanism detected
    return 999999.0


def get_cpu_percent(sample_interval: float = 0.2) -> float:
    """Returns approximate system CPU utilization percentage (0 - 100)."""
    try:
        import psutil
        return float(psutil.cpu_percent(interval=sample_interval))
    except ImportError:
        pass

    # Fallback using standard library on Unix/Linux
    if hasattr(os, "getloadavg"):
        try:
            load_1m = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            pct = min(100.0, max(0.0, (load_1m / cores) * 100.0))
            return pct
        except Exception:
            pass

    return 10.0  # Conservative estimate


def is_machine_idle(
    idle_minutes_threshold: float = 5.0,
    max_cpu_percent: float = 30.0,
) -> Tuple[bool, str]:
    """
    Checks if the machine is eligible to process distributed calculation tasks.
    Returns (is_idle, reason).
    """
    idle_seconds = get_user_idle_seconds()
    required_seconds = idle_minutes_threshold * 60.0

    if idle_seconds < required_seconds:
        remaining = int(required_seconds - idle_seconds)
        return False, f"User active ({int(idle_seconds)}s idle, needs {remaining}s more)"

    cpu_pct = get_cpu_percent()
    if cpu_pct > max_cpu_percent:
        return False, f"CPU busy ({cpu_pct:.1f}% > {max_cpu_percent:.1f}% limit)"

    return True, f"Idle ({int(idle_seconds)}s inactive, CPU {cpu_pct:.1f}%)"
