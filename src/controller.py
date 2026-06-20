from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from common.crypto import DuplexCipher, build_session_material, rsa_encrypt
from common.protocol import (
    DELTA_FRAME_HEADER,
    FRAME_HEADER,
    PKG_ASSIST_REQUEST,
    PKG_ASSIST_RESPONSE,
    PKG_CLOSE,
    PKG_HEARTBEAT,
    PKG_HELLO,
    PKG_HELLO_ACK,
    PKG_KEY_EVENT,
    PKG_MOUSE_EVENT,
    PKG_PROXY_AGENT_LIST,
    PKG_PROXY_CODE_VERIFY,
    PKG_PROXY_CODE_VERIFY_RESULT,
    PKG_PROXY_REQUEST_CODE,
    PKG_PROXY_SESSION_READY,
    PKG_SCREEN_FRAME,
    PKG_SCREEN_FRAME_DELTA,
    PKG_SESSION_ACK,
    PKG_SESSION_KEY,
    PKG_STREAM_CONFIG,
    PKG_TEXT_INPUT,
    TILE_HEADER,
    decode_json,
    encode_json,
    read_packet,
    send_packet,
)
from common.windows import enable_dpi_awareness


STREAM_PRESETS = {
    "1280x720": (1280, 720, 58, 120),
    "1600x900": (1600, 900, 62, 120),
    "1920x1080": (1920, 1080, 66, 120),
    "2560x1440": (2560, 1440, 72, 120),
    "Native": (0, 0, 75, 120),
}


def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


class ControllerApp:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("HiCtrl Controller")
        self.root.geometry("2360x1520")

        self.socket: socket.socket | None = None
        self.proxy_socket: socket.socket | None = None
        self.cipher: DuplexCipher | None = None
        self._connected = False
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

        self.remote_size = (1, 1)
        self.remote_origin = (0, 0)
        self.preview_size = (1, 1)
        self.render_origin = (0, 0)
        self.render_size = (1, 1)
        self._last_motion = 0.0
        self.photo_image: ImageTk.PhotoImage | None = None
        self._last_frame: Image.Image | None = None

        # -- tk vars --
        self.mode_var = tk.StringVar(value="direct")
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="48221")
        self.proxy_host_var = tk.StringVar()
        self.proxy_port_var = tk.StringVar()
        self.proxy_status_var = tk.StringVar(value="Not connected")
        self.name_var = tk.StringVar(value=socket.gethostname())
        self.status_var = tk.StringVar(value="Disconnected")
        self.stream_profile_var = tk.StringVar(value="1920x1080")
        self.text_input_var = tk.StringVar()

        self.agent_list: list[dict] = []
        self.selected_agent_id: str | None = None
        self.temp_code_var = tk.StringVar()
        self._agent_accepted = False
        self._proxy_relay_active = False

        self._load_proxy_config()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── config ────────────────────────────────────────────────────

    def _load_proxy_config(self) -> None:
        cfg = _load_config()
        proxy = cfg.get("proxy", {})
        self.proxy_host_var.set(proxy.get("host", ""))
        self.proxy_port_var.set(str(proxy.get("port", "48222")))

    # ── ui ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # mode
        mode_frame = ttk.LabelFrame(main, text="Connection Mode", padding=8)
        mode_frame.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(mode_frame, text="Direct Connection", variable=self.mode_var,
                        value="direct", command=self._on_mode_change).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mode_frame, text="Via Proxy Server", variable=self.mode_var,
                        value="proxy", command=self._on_mode_change).pack(side="left")

        # direct frame
        self.direct_frame = ttk.Frame(main)
        self.direct_frame.pack(fill="x", pady=(0, 10))
        top = ttk.Frame(self.direct_frame)
        top.pack(fill="x")
        ttk.Label(top, text="Agent Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.host_var, width=18).grid(
            row=0, column=1, padx=(8, 16), sticky="ew")
        ttk.Label(top, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.port_var, width=10).grid(
            row=0, column=3, padx=(8, 16), sticky="w")
        ttk.Label(top, text="Your Name").grid(row=0, column=4, sticky="w")
        ttk.Entry(top, textvariable=self.name_var, width=18).grid(
            row=0, column=5, padx=(8, 16), sticky="ew")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(5, weight=1)

        # proxy frame
        self.proxy_frame = ttk.Frame(main)
        self.proxy_frame.pack(fill="x", pady=(0, 10))
        ptop = ttk.Frame(self.proxy_frame)
        ptop.pack(fill="x")
        ttk.Label(ptop, text="Proxy Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(ptop, textvariable=self.proxy_host_var, width=18).grid(
            row=0, column=1, padx=(8, 16), sticky="ew")
        ttk.Label(ptop, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(ptop, textvariable=self.proxy_port_var, width=10).grid(
            row=0, column=3, padx=(8, 16), sticky="w")
        ttk.Label(ptop, text="Your Name").grid(row=0, column=4, sticky="w")
        ttk.Entry(ptop, textvariable=self.name_var, width=18).grid(
            row=0, column=5, padx=(8, 16), sticky="ew")
        ptop.columnconfigure(1, weight=1)
        ptop.columnconfigure(5, weight=1)

        self.proxy_status_label = ttk.Label(
            main, textvariable=self.proxy_status_var, foreground="gray")
        self.proxy_status_label.pack(anchor="w")

        # agent list
        self.agent_listbox = tk.Listbox(main, height=4)
        self.agent_listbox.pack(fill="x", pady=(8, 0))
        self.agent_listbox.bind("<<ListboxSelect>>", self._on_agent_select)

        # code input
        code_frame = ttk.Frame(main)
        code_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(code_frame, text="Temporary Code:").pack(side="left")
        ttk.Entry(code_frame, textvariable=self.temp_code_var, width=12).pack(
            side="left", padx=(8, 8))
        self.request_button = ttk.Button(
            code_frame, text="Request Code", command=self._request_code, state="disabled")
        self.request_button.pack(side="left")
        self.verify_button = ttk.Button(
            code_frame, text="Verify & Connect", command=self._verify_code, state="disabled")
        self.verify_button.pack(side="left", padx=(8, 0))

        # controls
        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(10, 10))
        self.connect_button = ttk.Button(controls, text="Connect", command=self._on_connect_click)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(
            controls, text="Disconnect", command=self._disconnect, state="disabled")
        self.disconnect_button.pack(side="left", padx=(8, 0))
        ttk.Label(controls, text="Stream").pack(side="left", padx=(16, 6))
        self.stream_profile_box = ttk.Combobox(
            controls, textvariable=self.stream_profile_var, width=12,
            state="readonly", values=list(STREAM_PRESETS.keys()))
        self.stream_profile_box.pack(side="left")
        self.stream_profile_box.bind("<<ComboboxSelected>>", self._on_stream_change)

        ttk.Label(main, text="Status").pack(anchor="w")
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(2, 12))

        # text bar
        text_bar = ttk.Frame(main)
        text_bar.pack(fill="x", pady=(0, 10))
        ttk.Label(text_bar, text="Send Text").pack(side="left")
        self.text_entry = ttk.Entry(text_bar, textvariable=self.text_input_var)
        self.text_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.text_entry.bind("<Return>", self._on_send_text)
        self.send_text_button = ttk.Button(
            text_bar, text="Send", command=self._send_text, state="disabled")
        self.send_text_button.pack(side="left")

        # canvas
        self.canvas = tk.Canvas(main, bg="#101214", highlightthickness=1)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._draw_placeholder()
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<ButtonPress-1>", self._on_left_down)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_up)
        self.canvas.bind("<ButtonPress-3>", self._on_right_down)
        self.canvas.bind("<ButtonRelease-3>", self._on_right_up)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<KeyPress>", self._on_key_press)
        self.canvas.bind("<KeyRelease>", self._on_key_release)

        self._on_mode_change()

    def _set_status(self, text: str) -> None:
        self.root.after(0, self.status_var.set, text)

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()
        if mode == "direct":
            self.direct_frame.pack(fill="x", pady=(0, 10))
            self.proxy_frame.pack_forget()
            self.agent_listbox.pack_forget()
            self.request_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
            self.connect_button.configure(state="normal")
        else:
            self.direct_frame.pack_forget()
            self.proxy_frame.pack(fill="x", pady=(0, 10))
            self.agent_listbox.pack(fill="x", pady=(8, 0))
            self.connect_button.configure(
                state="normal" if self.proxy_socket is None else "disabled")

    def _on_connect_click(self) -> None:
        if self.mode_var.get() == "direct":
            self._connect_direct()
        else:
            self._connect_proxy()

    # ── proxy connection ──────────────────────────────────────────

    def _connect_proxy(self) -> None:
        if self.proxy_socket is not None:
            return
        host = self.proxy_host_var.get().strip()
        port = int(self.proxy_port_var.get().strip())
        self.connect_button.configure(state="disabled")
        threading.Thread(target=self._proxy_connect_worker, args=(host, port), daemon=True).start()

    def _proxy_connect_worker(self, host: str, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            send_packet(sock, PKG_HELLO, encode_json({
                "app": "HiCtrl", "version": 1, "role": "controller",
                "controller_name": self.name_var.get().strip() or socket.gethostname(),
            }))
            ack = read_packet(sock)
            if ack.package_id != PKG_HELLO_ACK:
                raise RuntimeError("Expected HELLO_ACK from proxy.")
            self.proxy_socket = sock
            self.root.after(0, lambda: self.proxy_status_var.set(
                f"Connected to proxy {host}:{port}"))
            self.root.after(0, lambda: self.connect_button.configure(state="disabled"))
            self._refresh_agent_list()
            self._proxy_loop(sock)
        except Exception as exc:
            self.root.after(0, lambda: self.proxy_status_var.set(
                f"Proxy connection failed: {exc}"))
            self.root.after(0, lambda: self.connect_button.configure(state="normal"))

    def _proxy_loop(self, sock: socket.socket) -> None:
        """Main reader for the proxy socket.

        Handles proxy management packets and, once a session is active,
        decrypts and dispatches relayed screen/control packets."""
        sock.settimeout(60.0)
        while not self._stop.is_set():
            try:
                pkt = read_packet(sock)
            except socket.timeout:
                if self._connected:
                    self._send_heartbeat()
                continue
            except Exception:
                break
            if pkt.package_id == PKG_CLOSE:
                if self._connected:
                    reason = decode_json(pkt.payload).get("reason", "Remote session closed.")
                    self._set_status(f"Disconnected: {reason}")
                    self.root.after(0, self._reset_connection)
                    continue  # keep proxy connection alive for future requests
                break
            # Proxy management packets
            if pkt.package_id == PKG_PROXY_AGENT_LIST:
                self._on_agent_list(decode_json(pkt.payload))
            elif pkt.package_id == PKG_PROXY_CODE_VERIFY_RESULT:
                self._on_code_verify_result(decode_json(pkt.payload))
            elif pkt.package_id == PKG_PROXY_SESSION_READY:
                self._on_proxy_session_ready_with_data(decode_json(pkt.payload))
            # Relayed encrypted session packets
            elif self._connected and self.cipher:
                try:
                    payload = self.cipher.decrypt(pkt.payload)
                except Exception:
                    continue
                if pkt.package_id == PKG_SCREEN_FRAME:
                    self._handle_frame(payload)
                elif pkt.package_id == PKG_SCREEN_FRAME_DELTA:
                    self._handle_delta_frame(payload)
                elif pkt.package_id == PKG_HEARTBEAT:
                    continue
        if not self._connected:
            self.proxy_socket = None
            self.root.after(0, lambda: self.proxy_status_var.set("Disconnected from proxy"))
            self.root.after(0, lambda: self.connect_button.configure(state="normal"))

    def _refresh_agent_list(self) -> None:
        if self.proxy_socket:
            send_packet(self.proxy_socket, PKG_PROXY_AGENT_LIST, encode_json({}))

    def _on_agent_list(self, data: dict) -> None:
        self.agent_list = data.get("agents", [])
        self.root.after(0, self._update_agent_listbox)

    def _update_agent_listbox(self) -> None:
        self.agent_listbox.delete(0, tk.END)
        for a in self.agent_list:
            self.agent_listbox.insert(tk.END,
                f"{a.get('agent_name', 'Unknown')} ({a.get('address', 'N/A')})")

    def _on_agent_select(self, _event) -> None:
        sel = self.agent_listbox.curselection()
        if sel:
            idx = sel[0]
            if idx < len(self.agent_list):
                self.selected_agent_id = self.agent_list[idx].get("agent_id")
        # Allow re-selecting a different agent during/after request flow
        self.request_button.configure(
            state="normal" if self.proxy_socket and self.selected_agent_id else "disabled")

    # ── proxy code flow ───────────────────────────────────────────

    def _request_code(self) -> None:
        if not self.selected_agent_id or not self.proxy_socket:
            return
        self._agent_accepted = False
        print(f"[CTRL] Requesting code for agent {self.selected_agent_id}")
        send_packet(self.proxy_socket, PKG_PROXY_REQUEST_CODE,
                    encode_json({"agent_id": self.selected_agent_id}))
        self.request_button.configure(state="disabled")
        self.verify_button.configure(state="disabled")
        self._set_status("Waiting for agent to accept...")

    def _on_code_verify_result(self, data: dict) -> None:
        success = data.get("success", False)
        reason = data.get("reason", "")
        print(f"[CTRL] Code verify result: success={success}, reason={reason}")
        if success:
            self._agent_accepted = True
            self._set_status("Agent accepted. Enter the temporary code and click Verify & Connect.")
            self.root.after(0, lambda: self.verify_button.configure(state="normal"))
            self.root.after(0, lambda: self.request_button.configure(state="normal"))
        else:
            self._set_status(f"Agent rejected or error: {reason}")
            self.root.after(0, lambda: self.verify_button.configure(state="disabled"))
            self.root.after(0, lambda: self.request_button.configure(
                state="normal" if self.proxy_socket and self.selected_agent_id else "disabled"))

    def _verify_code(self) -> None:
        if not self.proxy_socket:
            return
        code = self.temp_code_var.get().strip()
        if not code:
            messagebox.showwarning("Input Required", "Please enter the temporary code.")
            return
        send_packet(self.proxy_socket, PKG_PROXY_CODE_VERIFY,
                    encode_json({"code": code}))
        self.verify_button.configure(state="disabled")
        self.request_button.configure(state="disabled")
        self._set_status("Verifying code...")

    def _on_proxy_session_ready_with_data(self, data: dict) -> None:
        """Called from _proxy_loop with the session_ready payload."""
        sock = self.proxy_socket
        if sock is None:
            return
        public_key_pem = data.get("public_key_pem", "").encode("ascii")
        if not public_key_pem:
            self._set_status("No public key from agent.")
            return

        material = build_session_material()
        send_packet(sock, PKG_SESSION_KEY, rsa_encrypt(public_key_pem, material.to_bytes()))

        # Wait for agent's SESSION_ACK (relayed through proxy) before starting receiver
        try:
            sock.settimeout(15.0)
            ack = read_packet(sock)
            if ack.package_id != PKG_SESSION_ACK:
                self._set_status(f"Proxy session: expected SESSION_ACK, got 0x{ack.package_id:04X}")
                return
        except Exception as exc:
            self._set_status(f"Proxy session ACK failed: {exc}")
            return

        self.socket = sock
        self.cipher = DuplexCipher(
            material.key,
            send_prefix=material.controller_to_agent_prefix,
            recv_prefix=material.agent_to_controller_prefix,
        )
        self._connected = True
        self._stop.clear()
        self._proxy_relay_active = True
        self.root.after(0, self._set_connected_ui)
        self._set_status("Connected via proxy. Waiting for screen frames...")
        # Note: _proxy_loop continues reading and dispatches relayed packets.
        self.root.after(0, self.canvas.focus_set)
        self._send_stream_config()

    # ── direct connection ─────────────────────────────────────────

    def _connect_direct(self) -> None:
        if self._connected:
            return
        threading.Thread(target=self._connect_worker, daemon=True).start()
        self.connect_button.configure(state="disabled")
        self._set_status("Connecting...")

    def _connect_worker(self) -> None:
        host = self.host_var.get().strip()
        port = int(self.port_var.get().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(10.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((host, port))
            send_packet(sock, PKG_HELLO, encode_json({
                "app": "HiCtrl", "version": 1, "role": "controller",
                "controller_name": self.name_var.get().strip() or socket.gethostname(),
            }))
            ack = read_packet(sock)
            if ack.package_id != PKG_HELLO_ACK:
                raise RuntimeError("Expected HELLO_ACK packet.")
            hello_data = decode_json(ack.payload)
            public_key_pem = hello_data["public_key_pem"].encode("ascii")

            send_packet(sock, PKG_ASSIST_REQUEST, encode_json({
                "controller_name": self.name_var.get().strip() or socket.gethostname(),
                "capabilities": ["screen view", "mouse control", "keyboard control", "unicode text input"],
            }))

            resp = read_packet(sock)
            if resp.package_id != PKG_ASSIST_RESPONSE:
                raise RuntimeError("Expected ASSIST_RESPONSE packet.")
            if not decode_json(resp.payload).get("accepted", False):
                raise PermissionError("The remote user rejected the assistance request.")

            material = build_session_material()
            send_packet(sock, PKG_SESSION_KEY, rsa_encrypt(public_key_pem, material.to_bytes()))

            sack = read_packet(sock)
            if sack.package_id != PKG_SESSION_ACK:
                raise RuntimeError("Expected SESSION_ACK packet.")

            self.socket = sock
            self.cipher = DuplexCipher(
                material.key,
                send_prefix=material.controller_to_agent_prefix,
                recv_prefix=material.agent_to_controller_prefix,
            )
            self._connected = True
            self._stop.clear()
            self.root.after(0, self._set_connected_ui)
            self._set_status("Connected. Waiting for screen frames...")
            threading.Thread(target=self._receiver_loop, daemon=True).start()
            self.root.after(0, self.canvas.focus_set)
            self._send_stream_config()
        except Exception as exc:
            try:
                sock.close()
            except OSError:
                pass
            error_msg = str(exc)
            self.root.after(0, lambda: self.connect_button.configure(state="normal"))
            self._set_status(error_msg)
            self.root.after(0, lambda: messagebox.showerror(
                "Connection Failed", error_msg, parent=self.root))

    # ── receiver ──────────────────────────────────────────────────

    def _receiver_loop(self) -> None:
        sock = self.socket
        cipher = self.cipher
        if sock is None:
            return
        sock.settimeout(2.0)
        try:
            while not self._stop.is_set():
                try:
                    pkt = read_packet(sock)
                except socket.timeout:
                    self._send_heartbeat()
                    continue
                if pkt.package_id == PKG_CLOSE:
                    reason = decode_json(pkt.payload).get("reason", "Remote session closed.")
                    raise ConnectionAbortedError(reason)
                if cipher:
                    payload = cipher.decrypt(pkt.payload)
                else:
                    payload = pkt.payload
                if pkt.package_id == PKG_SCREEN_FRAME:
                    self._handle_frame(payload)
                elif pkt.package_id == PKG_SCREEN_FRAME_DELTA:
                    self._handle_delta_frame(payload)
                elif pkt.package_id == PKG_HEARTBEAT:
                    continue
        except Exception as exc:
            self._set_status(f"Disconnected: {exc}")
        finally:
            self.root.after(0, self._reset_connection)

    def _handle_frame(self, payload: bytes) -> None:
        left, top, rw, rh, pw, ph = FRAME_HEADER.unpack(payload[:FRAME_HEADER.size])
        image = Image.open(io.BytesIO(payload[FRAME_HEADER.size:]))
        image.load()
        self.remote_origin = (left, top)
        self.remote_size = (rw, rh)
        self.preview_size = (pw, ph)
        self._last_frame = image.copy()
        self.root.after(0, lambda img=image: self._draw_frame(img))

    def _handle_delta_frame(self, payload: bytes) -> None:
        """Reconstruct a full frame from the previous frame and changed tiles."""
        left, top, width, height, tile_size, tile_count = DELTA_FRAME_HEADER.unpack(
            payload[:DELTA_FRAME_HEADER.size])
        offset = DELTA_FRAME_HEADER.size

        if self._last_frame is None or self._last_frame.size != (width, height):
            # Reference frame size mismatch; skip and wait for the next key frame
            return

        frame = self._last_frame.copy()
        try:
            for _ in range(tile_count):
                tx, ty, tw, th, jpeg_len = TILE_HEADER.unpack(
                    payload[offset:offset + TILE_HEADER.size])
                offset += TILE_HEADER.size
                jpeg = payload[offset:offset + jpeg_len]
                offset += jpeg_len
                tile = Image.open(io.BytesIO(jpeg))
                tile.load()
                frame.paste(tile, (tx, ty))
        except Exception as exc:
            self._set_status(f"Delta frame decode failed: {exc}")
            return

        self.remote_origin = (left, top)
        self._last_frame = frame
        self.root.after(0, lambda img=frame: self._draw_frame(img))

    def _draw_frame(self, image: Image.Image) -> None:
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        frame = image.copy()
        frame.thumbnail((cw, ch))
        self.photo_image = ImageTk.PhotoImage(frame)
        self.canvas.delete("all")
        x, y = cw // 2, ch // 2
        self.render_size = (self.photo_image.width(), self.photo_image.height())
        self.render_origin = (x - self.render_size[0] // 2, y - self.render_size[1] // 2)
        self.canvas.create_image(x, y, image=self.photo_image)
        self.canvas.focus_set()
        self._set_status(f"Connected. Remote desktop {self.remote_size[0]}x{self.remote_size[1]}")

    def _draw_placeholder(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
            text="Connect to an agent to view its desktop.",
            fill="#D5D9E0", font=("Segoe UI", 20))

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        if self.photo_image is None:
            self._draw_placeholder()

    # ── input mapping ─────────────────────────────────────────────

    def _canvas_to_remote(self, event: tk.Event) -> tuple[int, int] | None:
        if self.photo_image is None:
            return None
        ox, oy = self.render_origin
        iw, ih = self.render_size
        if not (ox <= event.x < ox + iw and oy <= event.y < oy + ih):
            return None
        rx = self.remote_origin[0] + round(
            (event.x - ox) / max(iw - 1, 1) * max(self.remote_size[0] - 1, 0))
        ry = self.remote_origin[1] + round(
            (event.y - oy) / max(ih - 1, 1) * max(self.remote_size[1] - 1, 0))
        return rx, ry

    def _send_control(self, pkg_id: int, msg: dict) -> None:
        if not self._connected or self.socket is None or self.cipher is None:
            return
        # In proxy mode all control traffic goes through proxy socket
        sock = self.proxy_socket if self.proxy_socket and self.proxy_socket is self.socket else self.socket
        try:
            payload = self.cipher.encrypt(encode_json(msg))
            with self._send_lock:
                send_packet(sock, pkg_id, payload)
        except OSError:
            self._disconnect()

    def _send_stream_config(self) -> None:
        profile = STREAM_PRESETS.get(self.stream_profile_var.get(), STREAM_PRESETS["1920x1080"])
        self._send_control(PKG_STREAM_CONFIG, {
            "max_width": profile[0], "max_height": profile[1],
            "quality": profile[2], "fps": profile[3],
        })

    def _on_stream_change(self, _event: tk.Event) -> None:
        self._send_stream_config()

    def _send_heartbeat(self) -> None:
        if not self._connected or self.socket is None or self.cipher is None:
            return
        # In proxy mode heartbeat goes through proxy socket
        sock = self.proxy_socket if self.proxy_socket and self.proxy_socket is self.socket else self.socket
        try:
            payload = self.cipher.encrypt(encode_json({"ts": time.time()}))
            with self._send_lock:
                send_packet(sock, PKG_HEARTBEAT, payload)
        except OSError:
            pass

    def _on_mouse_move(self, event: tk.Event) -> None:
        now = time.monotonic()
        if now - self._last_motion < 0.03:
            return
        self._last_motion = now
        pt = self._canvas_to_remote(event)
        if pt:
            self._send_control(PKG_MOUSE_EVENT, {"action": "move", "x": pt[0], "y": pt[1]})

    def _on_left_down(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        self._send_mouse_button(event, "left", "down")

    def _on_left_up(self, event: tk.Event) -> None:
        self._send_mouse_button(event, "left", "up")

    def _on_right_down(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        self._send_mouse_button(event, "right", "down")

    def _on_right_up(self, event: tk.Event) -> None:
        self._send_mouse_button(event, "right", "up")

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        pt = self._canvas_to_remote(event)
        if pt:
            self._send_control(PKG_MOUSE_EVENT,
                {"action": "wheel", "x": pt[0], "y": pt[1], "delta": event.delta})

    def _send_mouse_button(self, event: tk.Event, btn: str, act: str) -> None:
        pt = self._canvas_to_remote(event)
        if pt:
            self._send_control(PKG_MOUSE_EVENT,
                {"action": act, "button": btn, "x": pt[0], "y": pt[1]})

    def _on_key_press(self, event: tk.Event) -> None:
        if event.keysym == "??":
            return
        self._send_control(PKG_KEY_EVENT, {"action": "down", "key": event.keysym})

    def _on_key_release(self, event: tk.Event) -> None:
        if event.keysym == "??":
            return
        self._send_control(PKG_KEY_EVENT, {"action": "up", "key": event.keysym})

    def _on_send_text(self, _event: tk.Event) -> str:
        self._send_text()
        return "break"

    def _send_text(self) -> None:
        text = self.text_input_var.get()
        if text:
            self._send_control(PKG_TEXT_INPUT, {"text": text})
            self.text_input_var.set("")
            self.canvas.focus_set()

    # ── disconnect ────────────────────────────────────────────────

    def _disconnect(self) -> None:
        sock = self.socket
        if sock:
            try:
                send_packet(sock, PKG_CLOSE,
                            encode_json({"reason": "Controller disconnected."}))
            except OSError:
                pass
        # For proxy mode we keep the proxy socket alive so the user can request
        # another code. Do not set _stop here; _proxy_loop must keep reading.
        if self.proxy_socket and self.proxy_socket is self.socket:
            self._reset_connection()
            return
        # Direct mode: stop the receiver loop and close everything.
        self._stop.set()
        if self.proxy_socket:
            try:
                self.proxy_socket.close()
            except OSError:
                pass
        self.proxy_socket = None
        self._reset_connection()

    def _reset_connection(self) -> None:
        self._connected = False
        self._stop.clear()
        self.socket = None
        self.cipher = None
        self._agent_accepted = False
        self._proxy_relay_active = False
        self._last_frame = None
        self.temp_code_var.set("")
        self.connect_button.configure(state="disabled" if self.proxy_socket else "normal")
        self.disconnect_button.configure(state="disabled")
        self.send_text_button.configure(state="disabled")
        self.verify_button.configure(state="disabled")
        self.stream_profile_box.configure(state="readonly")
        self.photo_image = None
        self.render_origin = (0, 0)
        self.render_size = (1, 1)
        self._draw_placeholder()
        # Re-enable request button if proxy is connected and an agent is selected
        if self.proxy_socket and self.selected_agent_id:
            self.request_button.configure(state="normal")
        # Reset socket timeout so _proxy_loop can continue with management packets
        if self.proxy_socket:
            try:
                self.proxy_socket.settimeout(60.0)
            except OSError:
                pass

    def _set_connected_ui(self) -> None:
        self.disconnect_button.configure(state="normal")
        self.send_text_button.configure(state="normal")
        self.stream_profile_box.configure(state="readonly")

    def _on_close(self) -> None:
        self._disconnect()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ControllerApp().run()