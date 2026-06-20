from __future__ import annotations

import sys
from abc import ABC, abstractmethod

from PIL import Image


class CaptureBackend(ABC):
    @abstractmethod
    def grab(
        self,
        bbox: tuple[int, int, int, int] | None = None,
        all_screens: bool = False,
    ) -> Image.Image:
        raise NotImplementedError


class MSSBackend(CaptureBackend):
    def __init__(self) -> None:
        import mss

        self._sct = mss.mss()

    def grab(
        self,
        bbox: tuple[int, int, int, int] | None = None,
        all_screens: bool = False,
    ) -> Image.Image:
        if all_screens and bbox is None:
            monitor = self._sct.monitors[0]
        elif bbox:
            monitor = {
                "left": bbox[0],
                "top": bbox[1],
                "width": bbox[2] - bbox[0],
                "height": bbox[3] - bbox[1],
            }
        else:
            monitor = self._sct.monitors[1]
        raw = self._sct.grab(monitor)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


class PILBackend(CaptureBackend):
    def grab(
        self,
        bbox: tuple[int, int, int, int] | None = None,
        all_screens: bool = False,
    ) -> Image.Image:
        from PIL import ImageGrab

        kwargs: dict = {}
        if bbox:
            kwargs["bbox"] = bbox
        # all_screens is only reliably supported on Windows by PIL.
        if all_screens and sys.platform == "win32":
            kwargs["all_screens"] = True
        return ImageGrab.grab(**kwargs)


def create_capture_backend() -> CaptureBackend:
    try:
        import mss  # noqa: F401

        return MSSBackend()
    except ImportError:
        pass

    if sys.platform in ("win32", "darwin"):
        return PILBackend()

    raise RuntimeError(
        "Screen capture requires 'mss' on this platform. "
        "Install it with: pip install mss"
    )
