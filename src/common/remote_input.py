from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile

from common.platform import is_linux, is_macos, is_windows


def set_mouse_position(x: int, y: int) -> None:
    _backend().set_mouse_position(int(x), int(y))


def mouse_button(action: str, button: str) -> None:
    _backend().mouse_button(action, button)


def mouse_wheel(delta: int) -> None:
    _backend().mouse_wheel(int(delta))


def keyboard_event(action: str, key_name: str) -> None:
    _backend().keyboard_event(action, key_name)


def send_text(text: str) -> None:
    _backend().send_text(text)


class InputBackend:
    """Cross-platform remote input abstraction."""

    def set_mouse_position(self, x: int, y: int) -> None:
        raise NotImplementedError

    def mouse_button(self, action: str, button: str) -> None:
        raise NotImplementedError

    def mouse_wheel(self, delta: int) -> None:
        raise NotImplementedError

    def keyboard_event(self, action: str, key_name: str) -> None:
        raise NotImplementedError

    def send_text(self, text: str) -> None:
        raise NotImplementedError


_backend_instance: InputBackend | None = None


def _backend() -> InputBackend:
    global _backend_instance
    if _backend_instance is None:
        if is_windows():
            _backend_instance = WindowsInputBackend()
        elif is_linux():
            _backend_instance = LinuxInputBackend()
        elif is_macos():
            _backend_instance = MacOSInputBackend()
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")
    return _backend_instance


# ═══════════════════════════════════════════════════════════════════════════════
# Windows implementation (SendInput via ctypes)
# ═══════════════════════════════════════════════════════════════════════════════


class WindowsInputBackend(InputBackend):
    def __init__(self) -> None:
        import ctypes

        self._user32 = ctypes.windll.user32

        self._MOUSEEVENTF_MOVE = 0x0001
        self._MOUSEEVENTF_LEFTDOWN = 0x0002
        self._MOUSEEVENTF_LEFTUP = 0x0004
        self._MOUSEEVENTF_RIGHTDOWN = 0x0008
        self._MOUSEEVENTF_RIGHTUP = 0x0010
        self._MOUSEEVENTF_MIDDLEDOWN = 0x0020
        self._MOUSEEVENTF_MIDDLEUP = 0x0040
        self._MOUSEEVENTF_WHEEL = 0x0800
        self._KEYEVENTF_KEYUP = 0x0002
        self._KEYEVENTF_UNICODE = 0x0004

        self._button_flags = {
            "left": (self._MOUSEEVENTF_LEFTDOWN, self._MOUSEEVENTF_LEFTUP),
            "right": (self._MOUSEEVENTF_RIGHTDOWN, self._MOUSEEVENTF_RIGHTUP),
            "middle": (self._MOUSEEVENTF_MIDDLEDOWN, self._MOUSEEVENTF_MIDDLEUP),
        }

        self._special_keys = self._build_special_keys()
        self._INPUT_KEYBOARD = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint),
                ("time", ctypes.c_uint),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", ctypes.c_uint), ("u", _INPUTUNION)]

        self._KEYBDINPUT = KEYBDINPUT
        self._INPUTUNION = _INPUTUNION
        self._INPUT = INPUT

    def _build_special_keys(self) -> dict[str, int]:
        keys = {
            "BackSpace": 0x08,
            "Tab": 0x09,
            "Return": 0x0D,
            "Shift_L": 0xA0,
            "Shift_R": 0xA1,
            "Control_L": 0xA2,
            "Control_R": 0xA3,
            "Alt_L": 0xA4,
            "Alt_R": 0xA5,
            "Pause": 0x13,
            "Caps_Lock": 0x14,
            "Escape": 0x1B,
            "space": 0x20,
            "Prior": 0x21,
            "Next": 0x22,
            "End": 0x23,
            "Home": 0x24,
            "Left": 0x25,
            "Up": 0x26,
            "Right": 0x27,
            "Down": 0x28,
            "Insert": 0x2D,
            "Delete": 0x2E,
            "Super_L": 0x5B,
            "Super_R": 0x5C,
        }
        for index in range(1, 13):
            keys[f"F{index}"] = 0x6F + index
        return keys

    def _virtual_key_for_name(self, key_name: str) -> int:
        if key_name in self._special_keys:
            return self._special_keys[key_name]
        if len(key_name) == 1:
            vk = self._user32.VkKeyScanW(ord(key_name))
            if vk != -1:
                return vk & 0xFF
            return ord(key_name.upper())
        if (
            key_name.startswith("KP_")
            and len(key_name) == 4
            and key_name[-1].isdigit()
        ):
            return 0x60 + int(key_name[-1])
        raise KeyError(f"Unsupported key: {key_name}")

    def set_mouse_position(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(x, y)

    def mouse_button(self, action: str, button: str) -> None:
        down_flag, up_flag = self._button_flags.get(button, self._button_flags["left"])
        flag = down_flag if action == "down" else up_flag
        self._user32.mouse_event(flag, 0, 0, 0, 0)

    def mouse_wheel(self, delta: int) -> None:
        self._user32.mouse_event(self._MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def keyboard_event(self, action: str, key_name: str) -> None:
        vk = self._virtual_key_for_name(key_name)
        flags = 0 if action == "down" else self._KEYEVENTF_KEYUP
        self._user32.keybd_event(vk, 0, flags, 0)

    def send_text(self, text: str) -> None:
        inputs: list = []
        utf16 = text.encode("utf-16-le")
        for index in range(0, len(utf16), 2):
            codepoint = int.from_bytes(utf16[index : index + 2], "little")
            inputs.append(
                self._INPUT(
                    type=self._INPUT_KEYBOARD,
                    u=self._INPUTUNION(
                        ki=self._KEYBDINPUT(
                            wVk=0,
                            wScan=codepoint,
                            dwFlags=self._KEYEVENTF_UNICODE,
                            time=0,
                            dwExtraInfo=None,
                        )
                    ),
                )
            )
            inputs.append(
                self._INPUT(
                    type=self._INPUT_KEYBOARD,
                    u=self._INPUTUNION(
                        ki=self._KEYBDINPUT(
                            wVk=0,
                            wScan=codepoint,
                            dwFlags=self._KEYEVENTF_UNICODE | self._KEYEVENTF_KEYUP,
                            time=0,
                            dwExtraInfo=None,
                        )
                    ),
                )
            )
        if not inputs:
            return
        array_type = self._INPUT * len(inputs)
        sent = self._user32.SendInput(
            len(inputs), array_type(*inputs), ctypes.sizeof(self._INPUT)
        )
        if sent != len(inputs):
            raise OSError("SendInput failed while injecting Unicode text.")


# ═══════════════════════════════════════════════════════════════════════════════
# Linux implementation (python-xlib/XTest preferred, xdotool fallback)
# ═══════════════════════════════════════════════════════════════════════════════


class LinuxInputBackend(InputBackend):
    def __init__(self) -> None:
        self._x11_backend: InputBackend | None = None
        self._xdotool_backend: InputBackend | None = None

    def _active(self) -> InputBackend:
        if self._x11_backend is None and self._xdotool_backend is None:
            try:
                self._x11_backend = X11InputBackend()
            except Exception:
                self._xdotool_backend = XdotoolInputBackend()
        return self._x11_backend or self._xdotool_backend

    def set_mouse_position(self, x: int, y: int) -> None:
        self._active().set_mouse_position(x, y)

    def mouse_button(self, action: str, button: str) -> None:
        self._active().mouse_button(action, button)

    def mouse_wheel(self, delta: int) -> None:
        self._active().mouse_wheel(delta)

    def keyboard_event(self, action: str, key_name: str) -> None:
        self._active().keyboard_event(action, key_name)

    def send_text(self, text: str) -> None:
        self._active().send_text(text)


class X11InputBackend(InputBackend):
    """Use python-xlib and the XTest extension when available."""

    def __init__(self) -> None:
        from Xlib.display import Display
        from Xlib import X

        self._display = Display()
        self._root = self._display.screen().root
        self._X = X

        # Ensure XTest is available.
        if not self._display.has_extension("XTEST"):
            raise RuntimeError("XTEST extension not available")

    def set_mouse_position(self, x: int, y: int) -> None:
        from Xlib.ext.xtest import fake_input

        fake_input(self._display, self._X.MotionNotify, x=x, y=y)
        self._display.sync()

    def mouse_button(self, action: str, button: str) -> None:
        from Xlib.ext.xtest import fake_input

        x_button = _linux_button_number(button)
        event_type = (
            self._X.ButtonPress if action == "down" else self._X.ButtonRelease
        )
        fake_input(self._display, event_type, x_button)
        self._display.sync()

    def mouse_wheel(self, delta: int) -> None:
        from Xlib.ext.xtest import fake_input

        # X buttons 4/5 are vertical wheel up/down.
        clicks = max(1, abs(delta) // 120)
        button = 4 if delta > 0 else 5
        event = self._X.ButtonPress
        for _ in range(clicks):
            fake_input(self._display, event, button)
        self._display.sync()

    def keyboard_event(self, action: str, key_name: str) -> None:
        from Xlib.ext.xtest import fake_input

        keycode = self._keycode_for_name(key_name)
        event_type = self._X.KeyPress if action == "down" else self._X.KeyRelease
        fake_input(self._display, event_type, keycode)
        self._display.sync()

    def send_text(self, text: str) -> None:
        from Xlib.ext.xtest import fake_input

        for char in text:
            try:
                keysym = ord(char)
                keycode = self._display.keysym_to_keycode(keysym)
            except Exception:
                continue
            fake_input(self._display, self._X.KeyPress, keycode)
            fake_input(self._display, self._X.KeyRelease, keycode)
        self._display.sync()

    def _keycode_for_name(self, key_name: str) -> int:
        # Prefer a direct keysym lookup for single printable characters.
        if len(key_name) == 1:
            return self._display.keysym_to_keycode(ord(key_name))

        symbol = _linux_key_symbol(key_name)
        if symbol is None:
            raise KeyError(f"Unsupported key: {key_name}")
        keycode = self._display.keysym_to_keycode(symbol)
        if keycode == 0:
            raise KeyError(f"Key not mapped on this keyboard: {key_name}")
        return keycode


class XdotoolInputBackend(InputBackend):
    """Fallback that shells out to xdotool."""

    def set_mouse_position(self, x: int, y: int) -> None:
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)

    def mouse_button(self, action: str, button: str) -> None:
        b = _linux_button_number(button)
        cmd = "mousedown" if action == "down" else "mouseup"
        subprocess.run(["xdotool", cmd, str(b)], check=True)

    def mouse_wheel(self, delta: int) -> None:
        clicks = max(1, abs(delta) // 120)
        button = "4" if delta > 0 else "5"
        for _ in range(clicks):
            subprocess.run(["xdotool", "click", button], check=True)

    def keyboard_event(self, action: str, key_name: str) -> None:
        xdo_key = _linux_xdotool_key_name(key_name)
        cmd = "keydown" if action == "down" else "keyup"
        subprocess.run(["xdotool", cmd, xdo_key], check=True)

    def send_text(self, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            subprocess.run(["xdotool", "type", "--file", path], check=True)
        finally:
            import os

            os.unlink(path)


def _linux_button_number(button: str) -> int:
    mapping = {"left": 1, "middle": 2, "right": 3}
    return mapping.get(button, 1)


def _linux_xdotool_key_name(key_name: str) -> str:
    mapping = {
        "BackSpace": "BackSpace",
        "Tab": "Tab",
        "Return": "Return",
        "Shift_L": "shift",
        "Shift_R": "shift",
        "Control_L": "ctrl",
        "Control_R": "ctrl",
        "Alt_L": "alt",
        "Alt_R": "alt",
        "Super_L": "super",
        "Super_R": "super",
        "Pause": "Pause",
        "Caps_Lock": "Caps_Lock",
        "Escape": "Escape",
        "space": "space",
        "Prior": "Prior",
        "Next": "Next",
        "End": "End",
        "Home": "Home",
        "Left": "Left",
        "Up": "Up",
        "Right": "Right",
        "Down": "Down",
        "Insert": "Insert",
        "Delete": "Delete",
    }
    if key_name in mapping:
        return mapping[key_name]
    if key_name.startswith("F") and key_name[1:].isdigit():
        return key_name
    if len(key_name) == 1:
        return key_name
    if key_name.startswith("KP_") and len(key_name) == 4 and key_name[-1].isdigit():
        return "KP_" + key_name[-1]
    raise KeyError(f"Unsupported key: {key_name}")


def _linux_key_symbol(key_name: str) -> int | None:
    """Map key names to X keysyms for python-xlib."""
    from Xlib import XK

    if key_name.startswith("F") and key_name[1:].isdigit():
        n = int(key_name[1:])
        return getattr(XK, f"XK_F{n}", None)

    mapping = {
        "BackSpace": XK.XK_BackSpace,
        "Tab": XK.XK_Tab,
        "Return": XK.XK_Return,
        "Shift_L": XK.XK_Shift_L,
        "Shift_R": XK.XK_Shift_R,
        "Control_L": XK.XK_Control_L,
        "Control_R": XK.XK_Control_R,
        "Alt_L": XK.XK_Alt_L,
        "Alt_R": XK.XK_Alt_R,
        "Super_L": XK.XK_Super_L,
        "Super_R": XK.XK_Super_R,
        "Pause": XK.XK_Pause,
        "Caps_Lock": XK.XK_Caps_Lock,
        "Escape": XK.XK_Escape,
        "space": XK.XK_space,
        "Prior": XK.XK_Prior,
        "Next": XK.XK_Next,
        "End": XK.XK_End,
        "Home": XK.XK_Home,
        "Left": XK.XK_Left,
        "Up": XK.XK_Up,
        "Right": XK.XK_Right,
        "Down": XK.XK_Down,
        "Insert": XK.XK_Insert,
        "Delete": XK.XK_Delete,
    }
    return mapping.get(key_name)


# ═══════════════════════════════════════════════════════════════════════════════
# macOS implementation (Quartz/pyobjc preferred, AppleScript/cliclick fallback)
# ═══════════════════════════════════════════════════════════════════════════════


class MacOSInputBackend(InputBackend):
    def __init__(self) -> None:
        self._quartz_backend: InputBackend | None = None
        self._script_backend: InputBackend | None = None

    def _active(self) -> InputBackend:
        if self._quartz_backend is None and self._script_backend is None:
            try:
                self._quartz_backend = QuartzInputBackend()
            except Exception:
                self._script_backend = AppleScriptInputBackend()
        return self._quartz_backend or self._script_backend

    def set_mouse_position(self, x: int, y: int) -> None:
        self._active().set_mouse_position(x, y)

    def mouse_button(self, action: str, button: str) -> None:
        self._active().mouse_button(action, button)

    def mouse_wheel(self, delta: int) -> None:
        self._active().mouse_wheel(delta)

    def keyboard_event(self, action: str, key_name: str) -> None:
        self._active().keyboard_event(action, key_name)

    def send_text(self, text: str) -> None:
        self._active().send_text(text)


class QuartzInputBackend(InputBackend):
    """Use pyobjc's Quartz framework for low-latency input injection."""

    def __init__(self) -> None:
        import Quartz

        self._Quartz = Quartz
        self._current_location = (0, 0)

    def set_mouse_position(self, x: int, y: int) -> None:
        self._current_location = (x, y)
        event = self._Quartz.CGEventCreateMouseEvent(
            None,
            self._Quartz.kCGEventMouseMoved,
            self._current_location,
            self._Quartz.kCGMouseButtonLeft,
        )
        self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)

    def mouse_button(self, action: str, button: str) -> None:
        button = button.lower()
        if button == "left":
            down_type = self._Quartz.kCGEventLeftMouseDown
            up_type = self._Quartz.kCGEventLeftMouseUp
            btn = self._Quartz.kCGMouseButtonLeft
        elif button == "right":
            down_type = self._Quartz.kCGEventRightMouseDown
            up_type = self._Quartz.kCGEventRightMouseUp
            btn = self._Quartz.kCGMouseButtonRight
        elif button == "middle":
            down_type = self._Quartz.kCGEventOtherMouseDown
            up_type = self._Quartz.kCGEventOtherMouseUp
            btn = self._Quartz.kCGMouseButtonCenter
        else:
            down_type = self._Quartz.kCGEventLeftMouseDown
            up_type = self._Quartz.kCGEventLeftMouseUp
            btn = self._Quartz.kCGMouseButtonLeft

        event_type = down_type if action == "down" else up_type
        event = self._Quartz.CGEventCreateMouseEvent(
            None, event_type, self._current_location, btn
        )
        self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)

    def mouse_wheel(self, delta: int) -> None:
        event = self._Quartz.CGEventCreateScrollWheelEvent(
            None, self._Quartz.kCGScrollEventUnitPixel, 1, delta
        )
        self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)

    def keyboard_event(self, action: str, key_name: str) -> None:
        key_code = _macos_key_code(key_name)
        event = self._Quartz.CGEventCreateKeyboardEvent(
            None, key_code, action == "down"
        )
        self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)

    def send_text(self, text: str) -> None:
        for char in text:
            event = self._Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            self._Quartz.CGEventKeyboardSetUnicodeString(event, 1, char)
            self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)
            event = self._Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            self._Quartz.CGEventKeyboardSetUnicodeString(event, 1, char)
            self._Quartz.CGEventPost(self._Quartz.kCGHIDEventTap, event)


class AppleScriptInputBackend(InputBackend):
    """Fallback using osascript and optionally cliclick for mouse movement."""

    def __init__(self) -> None:
        self._has_cliclick = self._check_cliclick()
        self._last_pos = (0, 0)

    def _check_cliclick(self) -> bool:
        try:
            result = subprocess.run(
                ["cliclick", "-V"], capture_output=True, timeout=2
            )
            return result.returncode == 0
        except Exception:
            return False

    def _run(self, script: str) -> None:
        subprocess.run(["osascript", "-e", script], check=True)

    def set_mouse_position(self, x: int, y: int) -> None:
        self._last_pos = (x, y)
        if self._has_cliclick:
            subprocess.run(["cliclick", f"m:{x},{y}"], check=True)
            return
        # AppleScript cannot move the cursor reliably; rely on cliclick.
        raise RuntimeError(
            "Mouse movement on macOS requires 'cliclick' when pyobjc/Quartz is unavailable."
        )

    def mouse_button(self, action: str, button: str) -> None:
        if not self._has_cliclick:
            raise RuntimeError(
                "Mouse clicks on macOS require 'cliclick' when pyobjc/Quartz is unavailable."
            )
        x, y = self._last_pos
        btn_flag = "c"
        if button == "right":
            btn_flag = "rc"
        elif button == "middle":
            btn_flag = "mc"
        if action == "down":
            # cliclick does not support separate down/up for all buttons.
            subprocess.run(["cliclick", f"{btn_flag}:{x},{y}"], check=True)
        # For "up" we do nothing because cliclick generates full clicks.

    def mouse_wheel(self, delta: int) -> None:
        if not self._has_cliclick:
            raise RuntimeError(
                "Mouse wheel on macOS requires 'cliclick' when pyobjc/Quartz is unavailable."
            )
        direction = "u" if delta > 0 else "d"
        clicks = max(1, abs(delta) // 120)
        for _ in range(clicks):
            subprocess.run(["cliclick", f"{direction}:"], check=True)

    def keyboard_event(self, action: str, key_name: str) -> None:
        key_code = _macos_key_code(key_name)
        if action == "down":
            script = f'tell application "System Events" to key down (key code {key_code})'
        else:
            script = f'tell application "System Events" to key up (key code {key_code})'
        self._run(script)

    def send_text(self, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        self._run(script)


def _macos_key_code(key_name: str) -> int:
    """Map cross-platform key names to macOS key codes."""
    if len(key_name) == 1:
        # For printable characters, Quartz unicode input is preferred;
        # this is used as a best-effort fallback.
        return ord(key_name.upper())

    if key_name.startswith("F") and key_name[1:].isdigit():
        n = int(key_name[1:])
        codes = {
            1: 122,
            2: 120,
            3: 99,
            4: 118,
            5: 96,
            6: 97,
            7: 98,
            8: 100,
            9: 101,
            10: 109,
            11: 103,
            12: 111,
        }
        return codes.get(n, 0)

    mapping = {
        "BackSpace": 51,
        "Tab": 48,
        "Return": 36,
        "Shift_L": 56,
        "Shift_R": 60,
        "Control_L": 59,
        "Control_R": 62,
        "Alt_L": 58,
        "Alt_R": 61,
        "Super_L": 55,
        "Super_R": 54,
        "Pause": 113,
        "Caps_Lock": 57,
        "Escape": 53,
        "space": 49,
        "Prior": 116,
        "Next": 121,
        "End": 119,
        "Home": 115,
        "Left": 123,
        "Up": 126,
        "Right": 124,
        "Down": 125,
        "Insert": 114,
        "Delete": 117,
    }
    if key_name in mapping:
        return mapping[key_name]
    raise KeyError(f"Unsupported key: {key_name}")
