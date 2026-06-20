from __future__ import annotations

import ctypes


user32 = ctypes.windll.user32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

SPECIAL_KEYS = {
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
    SPECIAL_KEYS[f"F{index}"] = 0x6F + index


INPUT_KEYBOARD = 1


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


def set_mouse_position(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def mouse_button(action: str, button: str) -> None:
    down_flag, up_flag = BUTTON_FLAGS.get(button, BUTTON_FLAGS["left"])
    flag = down_flag if action == "down" else up_flag
    user32.mouse_event(flag, 0, 0, 0, 0)


def mouse_wheel(delta: int) -> None:
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)


def _virtual_key_for_name(key_name: str) -> int:
    if key_name in SPECIAL_KEYS:
        return SPECIAL_KEYS[key_name]
    if len(key_name) == 1:
        vk = user32.VkKeyScanW(ord(key_name))
        if vk != -1:
            return vk & 0xFF
        return ord(key_name.upper())
    if key_name.startswith("KP_") and len(key_name) == 4 and key_name[-1].isdigit():
        return 0x60 + int(key_name[-1])
    raise KeyError(f"Unsupported key: {key_name}")


def keyboard_event(action: str, key_name: str) -> None:
    vk = _virtual_key_for_name(key_name)
    flags = 0 if action == "down" else KEYEVENTF_KEYUP
    user32.keybd_event(vk, 0, flags, 0)


def send_text(text: str) -> None:
    inputs: list[INPUT] = []
    utf16 = text.encode("utf-16-le")
    for index in range(0, len(utf16), 2):
        codepoint = int.from_bytes(utf16[index : index + 2], "little")
        inputs.append(
            INPUT(
                type=INPUT_KEYBOARD,
                u=_INPUTUNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=codepoint,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=None,
                    )
                ),
            )
        )
        inputs.append(
            INPUT(
                type=INPUT_KEYBOARD,
                u=_INPUTUNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=codepoint,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=None,
                    )
                ),
            )
        )
    if not inputs:
        return
    array_type = INPUT * len(inputs)
    sent = user32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError("SendInput failed while injecting Unicode text.")
