from __future__ import annotations

import re
import subprocess

from common.platform import is_linux, is_macos, is_windows


def enable_dpi_awareness() -> None:
    """Make the process DPI-aware where applicable."""
    if is_windows():
        _enable_windows_dpi_awareness()
        return
    if is_macos():
        # macOS handles Retina scaling automatically for most apps.
        return
    if is_linux():
        # GTK/Qt scaling is handled via environment variables.
        return


def _enable_windows_dpi_awareness() -> None:
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the virtual desktop."""
    # Prefer mss because it works uniformly across platforms.
    try:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[0]
            return mon["left"], mon["top"], mon["width"], mon["height"]
    except Exception:
        pass

    if is_windows():
        return _get_windows_virtual_screen_bounds()
    if is_macos():
        return _get_macos_virtual_screen_bounds()
    if is_linux():
        return _get_linux_virtual_screen_bounds()

    raise RuntimeError("Unable to determine virtual screen bounds on this platform.")


def _get_windows_virtual_screen_bounds() -> tuple[int, int, int, int]:
    import ctypes

    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    return left, top, width, height


def _get_macos_virtual_screen_bounds() -> tuple[int, int, int, int]:
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab()
        return 0, 0, img.width, img.height
    except Exception:
        pass

    script = 'tell application "Finder" to bounds of window of desktop'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True
    )
    if result.returncode == 0:
        parts = result.stdout.strip().replace(",", "").split()
        if len(parts) == 4:
            _, _, w, h = map(int, parts)
            return 0, 0, w, h

    raise RuntimeError("Unable to determine macOS screen bounds.")


def _get_linux_virtual_screen_bounds() -> tuple[int, int, int, int]:
    try:
        result = subprocess.run(
            ["xrandr"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lefts, tops, rights, bottoms = [], [], [], []
            for line in result.stdout.splitlines():
                if " connected " in line:
                    match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                    if match:
                        w, h, x, y = map(int, match.groups())
                        lefts.append(x)
                        tops.append(y)
                        rights.append(x + w)
                        bottoms.append(y + h)
            if lefts:
                return (
                    min(lefts),
                    min(tops),
                    max(rights) - min(lefts),
                    max(bottoms) - min(tops),
                )
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["xdpyinfo"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", result.stdout)
            if match:
                w, h = map(int, match.groups())
                return 0, 0, w, h
    except Exception:
        pass

    raise RuntimeError(
        "Unable to determine Linux screen bounds. Ensure xrandr or xdpyinfo is available."
    )
