from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageChops, ImageGrab

from common.crypto import DuplexCipher, SessionMaterial, generate_rsa_keypair, rsa_decrypt
from common.protocol import (
    CONTROL_PACKET_IDS,
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
    PKG_PROXY_CODE_VERIFY,
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
from common.remote_input import (
    keyboard_event,
    mouse_button,
    mouse_wheel,
    send_text,
    set_mouse_position,
)
from common.windows import enable_dpi_awareness, get_virtual_screen_bounds


DEFAULT_FRAME_INTERVAL = 1.0 / 120.0

# Delta-frame tuning
FRAME_TILE_SIZE = 64
DELTA_CHANNEL_THRESHOLD = 10
DELTA_SUM_THRESHOLD = DELTA_CHANNEL_THRESHOLD * 3
DELTA_FULL_FRAME_RATIO = 0.5
KEY_FRAME_INTERVAL = 60


def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


class AgentApp:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("HiCtrl Agent")
        self.root.geometry("520x720")

        self.private_key, public_pem = generate_rsa_keypair()
        self.public_pem = public_pem

        self.server_socket: socket.socket | None = None
        self.client_socket: socket.socket | None = None
        self.proxy_socket: socket.socket | None = None
        self.cipher: DuplexCipher | None = None
        self._stop = threading.Event()
        self._session_stop = threading.Event()
        self._send_lock = threading.Lock()
        self._session_active = False
        self._stream_max = (1920, 1080)
        self._stream_quality = 72
        self._frame_interval = DEFAULT_FRAME_INTERVAL
        self._proxy_relay_active = False
        self._last_frame: Image.Image | None = None
        self._frame_counter = 0

        # -- tk vars --
        self.mode_var = tk.StringVar(value="direct")
        self.host_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="48221")
        self.proxy_host_var = tk.StringVar()
        self.proxy_port_var = tk.StringVar()
        self.proxy_status_var = tk.StringVar(value="Not connected")
        self.status_var = tk.StringVar(value="Idle")
        self.banner_var = tk.StringVar(
            value="No active session. Remote control is disabled."
        )
        self.temp_code_var = tk.StringVar(value="------")
        self.code_validity_var = tk.StringVar(value="")

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

        banner = tk.Label(
            main, textvariable=self.banner_var, bg="#7A0C0C", fg="white",
            padx=12, pady=10, wraplength=460, justify="left",
        )
        banner.pack(fill="x")
        self.banner_label = banner

        # mode
        mode_frame = ttk.LabelFrame(main, text="Connection Mode", padding=8)
        mode_frame.pack(fill="x", pady=(14, 8))
        ttk.Radiobutton(mode_frame, text="Direct Connection", variable=self.mode_var,
                        value="direct", command=self._on_mode_change).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mode_frame, text="Via Proxy Server", variable=self.mode_var,
                        value="proxy", command=self._on_mode_change).pack(side="left")

        # direct
        self.direct_frame = ttk.Frame(main)
        self.direct_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(self.direct_frame, text="Listen Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.direct_frame, textvariable=self.host_var, width=18).grid(
            row=0, column=1, padx=(8, 16), sticky="ew")
        ttk.Label(self.direct_frame, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(self.direct_frame, textvariable=self.port_var, width=10).grid(
            row=0, column=3, padx=(8, 0), sticky="w")
        self.direct_frame.columnconfigure(1, weight=1)

        # proxy
        self.proxy_frame = ttk.Frame(main)
        self.proxy_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(self.proxy_frame, text="Proxy Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.proxy_frame, textvariable=self.proxy_host_var, width=18).grid(
            row=0, column=1, padx=(8, 16), sticky="ew")
        ttk.Label(self.proxy_frame, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(self.proxy_frame, textvariable=self.proxy_port_var, width=10).grid(
            row=0, column=3, padx=(8, 0), sticky="w")
        self.proxy_frame.columnconfigure(1, weight=1)
        self.proxy_status_label = ttk.Label(
            main, textvariable=self.proxy_status_var, foreground="gray")
        self.proxy_status_label.pack(anchor="w")

        # controls
        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(8, 10))
        self.start_button = ttk.Button(controls, text="Start Listening", command=self._start_server)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop Session", command=self._stop_session,
                                      state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.proxy_button = ttk.Button(controls, text="Connect Proxy", command=self._connect_proxy,
                                       state="disabled")
        self.proxy_button.pack(side="left", padx=(8, 0))

        # code display
        code_frame = ttk.LabelFrame(main, text="Temporary Access Code", padding=8)
        code_frame.pack(fill="x", pady=(8, 10))
        tk.Label(code_frame, textvariable=self.temp_code_var,
                 font=("Consolas", 28, "bold"), fg="#0E5A2A").pack()
        ttk.Label(code_frame, textvariable=self.code_validity_var, foreground="gray").pack()

        ttk.Label(main, text="Status").pack(anchor="w")
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(2, 12))

        note = ("Incoming support requests always require approval. "
                "Mouse and keyboard control remain visible and can be stopped here.")
        ttk.Label(main, text=note, wraplength=480, justify="left").pack(anchor="w")

        self._on_mode_change()

    # ── helpers ───────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self.root.after(0, self.status_var.set, text)

    def _set_banner(self, active: bool, text: str) -> None:
        def update() -> None:
            self.banner_label.configure(bg="#0E5A2A" if active else "#7A0C0C")
            self.banner_var.set(text)
        self.root.after(0, update)

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()
        if mode == "direct":
            self.direct_frame.pack(fill="x", pady=(0, 8))
            self.proxy_frame.pack_forget()
            self.proxy_button.configure(state="disabled")
            self.start_button.configure(state="normal")
        else:
            self.direct_frame.pack_forget()
            self.proxy_frame.pack(fill="x", pady=(0, 8))
            self.start_button.configure(state="disabled")
            self.proxy_button.configure(state="normal")

    # ── proxy connection ──────────────────────────────────────────

    def _connect_proxy(self) -> None:
        if self.proxy_socket is not None:
            # Disconnect from proxy
            try:
                send_packet(self.proxy_socket, PKG_CLOSE,
                            encode_json({"reason": "Agent disconnecting."}))
            except OSError:
                pass
            try:
                self.proxy_socket.close()
            except OSError:
                pass
            self.proxy_socket = None
            self._proxy_relay_active = False
            self.root.after(0, lambda: self.proxy_status_var.set("Not connected"))
            self.root.after(0, lambda: self.proxy_button.configure(
                text="Connect Proxy", state="normal"))
            self._log("Disconnected from proxy.")
            return
        host = self.proxy_host_var.get().strip()
        port = int(self.proxy_port_var.get().strip())
        self.proxy_button.configure(state="disabled")
        threading.Thread(target=self._proxy_connect_worker, args=(host, port), daemon=True).start()

    def _proxy_connect_worker(self, host: str, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            send_packet(sock, PKG_HELLO, encode_json({
                "app": "HiCtrl", "version": 1, "role": "agent",
                "agent_name": socket.gethostname(),
                "public_key_pem": self.public_pem.decode("ascii"),
            }))
            ack = read_packet(sock)
            if ack.package_id != PKG_HELLO_ACK:
                raise RuntimeError("Expected HELLO_ACK from proxy.")
            self.proxy_socket = sock
            self.root.after(0, lambda: self.proxy_status_var.set(
                f"Connected to proxy {host}:{port}"))
            self.root.after(0, lambda: self.proxy_button.configure(text="Disconnect Proxy",
                                                                    state="normal"))
            self._proxy_loop(sock)
        except Exception as exc:
            self.root.after(0, lambda: self.proxy_status_var.set(
                f"Proxy connection failed: {exc}"))
            self.root.after(0, lambda: self.proxy_button.configure(state="normal"))

    def _proxy_loop(self, sock: socket.socket) -> None:
        """Single reader for the proxy socket.

        Handles management packets and, once a session is active, decrypts and
        dispatches relayed control packets. This avoids multiple threads reading
        the same TCP socket, which caused lost packets on repeated sessions."""
        sock.settimeout(2.0)
        while not self._stop.is_set():
            try:
                pkt = read_packet(sock)
            except socket.timeout:
                if self._session_active and self._session_stop.is_set():
                    self._end_proxy_session("Local user stopped session.")
                continue
            except Exception as exc:
                print(f"[AGENT] _proxy_loop read error: {exc}")
                break
            if pkt.package_id == PKG_CLOSE:
                reason = decode_json(pkt.payload).get("reason", "Remote session closed.")
                print(f"[AGENT] Received CLOSE: {reason}, session_active={self._session_active}")
                if self._session_active:
                    self._end_proxy_session(reason)
                    sock.settimeout(2.0)
                    continue  # keep proxy connection alive for future requests
                break
            if self._session_active:
                self._handle_proxy_session_packet(pkt)
            else:
                print(f"[AGENT] Received packet 0x{pkt.package_id:04X} from proxy")
                if pkt.package_id == PKG_PROXY_REQUEST_CODE:
                    print("[AGENT] Handling REQUEST_CODE")
                    self._on_proxy_request_code(sock, decode_json(pkt.payload))
                elif pkt.package_id == PKG_PROXY_SESSION_READY:
                    self._on_proxy_session_ready()
        print("[AGENT] _proxy_loop exiting")
        if self.proxy_socket is not None:
            self.proxy_socket = None
            self.root.after(0, lambda: self.proxy_status_var.set("Disconnected from proxy"))
            self.root.after(0, lambda: self.proxy_button.configure(text="Connect Proxy", state="normal"))

    def _on_proxy_request_code(self, sock: socket.socket, data: dict) -> None:
        code = data.get("code", "")
        validity = data.get("validity_seconds", 300)
        controller_name = data.get("controller_name", "Unknown")
        print(f"[AGENT] Showing consent dialog for {controller_name}, code={code}")

        self.root.after(0, lambda: self.temp_code_var.set(code))
        self.root.after(0, lambda: self.code_validity_var.set(
            f"Valid for {validity} seconds. Controller: {controller_name}"))

        # countdown timer
        def timer() -> None:
            for i in range(validity, 0, -1):
                if self.proxy_socket is None or self._session_active:
                    return
                self.root.after(0, lambda i=i: self.code_validity_var.set(
                    f"Expires in {i} seconds"))
                time.sleep(1)
            self.root.after(0, lambda: self.code_validity_var.set("Code expired"))
        threading.Thread(target=timer, daemon=True).start()

        allowed = self._request_consent(
            controller_name=controller_name,
            controller_ip="Proxy",
            capabilities=["screen view", "mouse control", "keyboard control", "unicode text input"],
        )
        print(f"[AGENT] Consent result: allowed={allowed}")

        send_packet(sock, PKG_PROXY_CODE_VERIFY,
                    encode_json({"code": code, "accepted": allowed}))
        if not allowed:
            self.root.after(0, lambda: self.temp_code_var.set("------"))
            self.root.after(0, lambda: self.code_validity_var.set(""))

    def _on_proxy_session_ready(self) -> None:
        """Proxy session ready – perform key exchange inline and start screen stream."""
        sock = self.proxy_socket
        if sock is None:
            return
        self._cleanup_session()
        try:
            # Wait for SESSION_KEY relayed from controller through proxy
            sock.settimeout(30.0)
            pkt = read_packet(sock)
            if pkt.package_id != PKG_SESSION_KEY:
                self._log("Proxy session: expected SESSION_KEY, got something else.")
                return
            material = SessionMaterial.from_bytes(
                rsa_decrypt(self.private_key, pkt.payload))
            self.cipher = DuplexCipher(
                material.key,
                send_prefix=material.agent_to_controller_prefix,
                recv_prefix=material.controller_to_agent_prefix,
            )
            send_packet(sock, PKG_SESSION_ACK, encode_json({"status": "ready"}))
            self._session_active = True
            self._session_stop.clear()
            self._proxy_relay_active = True
            self.root.after(0, lambda: self.stop_button.configure(state="normal"))
            self._set_banner(True,
                "Remote assistance active. Screen is being shared and input control "
                "is enabled. Use 'Stop Session' to terminate immediately.")
            self._log("Proxy session active (encrypted).")
            self.root.after(0, lambda: self.temp_code_var.set("------"))
            self.root.after(0, lambda: self.code_validity_var.set(""))
            threading.Thread(target=self._screen_stream_loop, daemon=True).start()
        except Exception as exc:
            self._log(f"Proxy session key exchange failed: {exc}")
            self._cleanup_session()

    def _handle_proxy_session_packet(self, pkt) -> None:
        """Dispatch a relayed control packet while a proxy session is active."""
        if self.cipher is None:
            return
        try:
            payload = self.cipher.decrypt(pkt.payload)
        except Exception:
            return
        if pkt.package_id == PKG_MOUSE_EVENT:
            self._apply_mouse(decode_json(payload))
        elif pkt.package_id == PKG_KEY_EVENT:
            self._apply_key(decode_json(payload))
        elif pkt.package_id == PKG_TEXT_INPUT:
            self._apply_text(decode_json(payload))
        elif pkt.package_id == PKG_STREAM_CONFIG:
            self._apply_stream_config(decode_json(payload))
        elif pkt.package_id == PKG_HEARTBEAT:
            pass

    def _end_proxy_session(self, reason: str) -> None:
        """Return to proxy management state after an active session ends."""
        self._session_active = False
        self._session_stop.set()
        self._proxy_relay_active = False
        self.client_socket = None
        self.cipher = None
        self.root.after(0, lambda: self.stop_button.configure(state="disabled"))
        self._set_banner(False, "No active session. Remote control is disabled.")
        self._log(f"Session ended: {reason}")
        print(f"[AGENT] Session ended, returning to proxy management state")
        # Reset the socket timeout so _proxy_loop can poll for management packets
        if self.proxy_socket is not None:
            try:
                self.proxy_socket.settimeout(2.0)
            except OSError:
                pass

    # ── direct server ─────────────────────────────────────────────

    def _start_server(self) -> None:
        if self.server_socket is not None:
            return
        host = self.host_var.get().strip()
        port = int(self.port_var.get().strip())
        self._stop.clear()
        threading.Thread(target=self._server_loop, args=(host, port), daemon=True).start()
        self.start_button.configure(state="disabled")
        self._log(f"Listening on {host}:{port}")

    def _server_loop(self, host: str, port: int) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(1)
            server.settimeout(1.0)
            self.server_socket = server
            while not self._stop.is_set():
                try:
                    client, addr = server.accept()
                except socket.timeout:
                    continue
                if self._session_active:
                    send_packet(client, PKG_CLOSE,
                                encode_json({"reason": "Agent is already in an active session."}))
                    client.close()
                    continue
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.client_socket = client
                self._handle_client(client, addr)
        except OSError as exc:
            self._log(f"Listener stopped: {exc}")
        finally:
            try:
                server.close()
            except OSError:
                pass
            self.server_socket = None
            self.root.after(0, lambda: self.start_button.configure(state="normal"))

    def _handle_client(self, client: socket.socket, address: tuple) -> None:
        self._log(f"Incoming connection from {address[0]}:{address[1]}")
        client.settimeout(10.0)
        try:
            hello = read_packet(client)
            if hello.package_id != PKG_HELLO:
                raise RuntimeError("Expected HELLO packet.")
            hello_data = decode_json(hello.payload)
            send_packet(client, PKG_HELLO_ACK, encode_json({
                "app": "HiCtrl", "version": 1, "role": "agent",
                "agent_name": socket.gethostname(),
                "public_key_pem": self.public_pem.decode("ascii"),
            }))

            req = read_packet(client)
            if req.package_id != PKG_ASSIST_REQUEST:
                raise RuntimeError("Expected ASSIST_REQUEST packet.")
            req_data = decode_json(req.payload)
            allowed = self._request_consent(
                controller_name=req_data.get("controller_name",
                                             hello_data.get("controller_name", "Unknown")),
                controller_ip=address[0],
                capabilities=req_data.get("capabilities", []),
            )
            send_packet(client, PKG_ASSIST_RESPONSE, encode_json({"accepted": allowed}))
            if not allowed:
                self._log("Support request rejected by local user.")
                client.close()
                return

            session_pkt = read_packet(client)
            if session_pkt.package_id != PKG_SESSION_KEY:
                raise RuntimeError("Expected SESSION_KEY packet.")
            material = SessionMaterial.from_bytes(
                rsa_decrypt(self.private_key, session_pkt.payload))
            self.cipher = DuplexCipher(
                material.key,
                send_prefix=material.agent_to_controller_prefix,
                recv_prefix=material.controller_to_agent_prefix,
            )
            send_packet(client, PKG_SESSION_ACK, encode_json({"status": "ready"}))

            self._session_active = True
            self._session_stop.clear()
            self.root.after(0, lambda: self.stop_button.configure(state="normal"))
            self._set_banner(True,
                "Remote assistance active. Screen is being shared and input control "
                "is enabled. Use 'Stop Session' to terminate immediately.")
            self._log("Encrypted control session active.")

            threading.Thread(target=self._screen_stream_loop, daemon=True).start()

            client.settimeout(2.0)
            while not self._session_stop.is_set():
                try:
                    pkt = read_packet(client)
                except socket.timeout:
                    continue
                if pkt.package_id == PKG_CLOSE:
                    break
                assert self.cipher
                payload = self.cipher.decrypt(pkt.payload)
                if pkt.package_id == PKG_MOUSE_EVENT:
                    self._apply_mouse(decode_json(payload))
                elif pkt.package_id == PKG_KEY_EVENT:
                    self._apply_key(decode_json(payload))
                elif pkt.package_id == PKG_TEXT_INPUT:
                    self._apply_text(decode_json(payload))
                elif pkt.package_id == PKG_STREAM_CONFIG:
                    self._apply_stream_config(decode_json(payload))
                elif pkt.package_id == PKG_HEARTBEAT:
                    continue
        except Exception as exc:
            self._log(f"Session ended: {exc}")
        finally:
            self._cleanup_session()

    def _request_consent(self, controller_name: str, controller_ip: str,
                         capabilities: list[str]) -> bool:
        decision = {"accepted": False}
        done = threading.Event()

        def prompt() -> None:
            cap_text = ", ".join(capabilities) if capabilities else "screen"
            msg = (f"{controller_name} ({controller_ip}) requests remote assistance.\n\n"
                   f"This session will allow: {cap_text}.\n\n"
                   "A visible session banner will remain on screen until you stop the session.\n"
                   "Do you want to allow this connection?")
            decision["accepted"] = messagebox.askyesno(
                title="Allow Remote Assistance", message=msg, parent=self.root)
            done.set()

        self.root.after(0, prompt)
        done.wait()
        return bool(decision["accepted"])

    # ── screen stream ─────────────────────────────────────────────

    def _screen_stream_loop(self) -> None:
        sock = self.client_socket if self.client_socket else self.proxy_socket
        cipher = self.cipher
        if sock is None or cipher is None:
            return
        self._frame_counter = 0
        while not self._session_stop.is_set():
            started = time.perf_counter()
            try:
                left, top, rw, rh = get_virtual_screen_bounds()
                screenshot = ImageGrab.grab(
                    bbox=(left, top, left + rw, top + rh), all_screens=True)
                mw, mh = self._stream_max
                if mw > 0 and mh > 0:
                    scale = min(mw / rw, mh / rh, 1.0)
                    tw, th = max(1, round(rw * scale)), max(1, round(rh * scale))
                    if (tw, th) != screenshot.size:
                        screenshot = screenshot.resize((tw, th), Image.Resampling.BILINEAR)

                pkg_id, payload = self._encode_frame(screenshot, left, top, rw, rh)
                if payload is None:
                    # No meaningful change; skip this frame
                    elapsed = time.perf_counter() - started
                    sleep = self._frame_interval - elapsed
                    if sleep > 0:
                        time.sleep(sleep)
                    continue

                encrypted = cipher.encrypt(payload)
                with self._send_lock:
                    send_packet(sock, pkg_id, encrypted)
            except OSError:
                break
            except Exception as exc:
                self._log(f"Screen stream stopped: {exc}")
                self._session_stop.set()
                break
            elapsed = time.perf_counter() - started
            sleep = self._frame_interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def _encode_frame(self, screenshot: Image.Image, left: int, top: int,
                      rw: int, rh: int) -> tuple[int, bytes] | tuple[None, None]:
        """Return either a key-frame or a delta-frame payload.

        A key frame is sent for the first frame, when dimensions change, when
        most of the screen changed, or periodically to bound error drift.
        """
        self._frame_counter += 1
        width, height = screenshot.size

        force_key = (
            self._last_frame is None
            or self._last_frame.size != screenshot.size
            or self._frame_counter % KEY_FRAME_INTERVAL == 0
        )

        if not force_key:
            tiles = self._detect_changed_tiles(self._last_frame, screenshot)
            total_tiles = (
                ((width + FRAME_TILE_SIZE - 1) // FRAME_TILE_SIZE)
                * ((height + FRAME_TILE_SIZE - 1) // FRAME_TILE_SIZE)
            )
            if len(tiles) > total_tiles * DELTA_FULL_FRAME_RATIO:
                force_key = True

        if force_key:
            buf = io.BytesIO()
            screenshot.save(buf, format="JPEG", quality=self._stream_quality, optimize=False)
            encoded = buf.getvalue()
            payload = FRAME_HEADER.pack(left, top, rw, rh, width, height) + encoded
            self._last_frame = screenshot.copy()
            return PKG_SCREEN_FRAME, payload

        # Update reference even if nothing changed, so tiny sub-threshold drift
        # does not accumulate over time.
        self._last_frame = screenshot.copy()

        if not tiles:
            return None, None

        payload = self._encode_delta_payload(left, top, width, height, tiles)
        return PKG_SCREEN_FRAME_DELTA, payload

    def _detect_changed_tiles(self, prev_img: Image.Image,
                              curr_img: Image.Image) -> list[tuple[int, int, Image.Image]]:
        """Return changed tiles using per-pixel average channel difference.

        A pixel is considered changed when
            (|R2-R| + |G2-G| + |B2-B|) / 3 >= 10,
        i.e. the sum of absolute channel differences >= 30.
        """
        diff = ImageChops.difference(curr_img, prev_img)
        r, g, b = diff.split()
        sum_diff = ImageChops.add(ImageChops.add(r, g), b)

        tiles: list[tuple[int, int, Image.Image]] = []
        width, height = curr_img.size
        for y in range(0, height, FRAME_TILE_SIZE):
            for x in range(0, width, FRAME_TILE_SIZE):
                tw = min(FRAME_TILE_SIZE, width - x)
                th = min(FRAME_TILE_SIZE, height - y)
                tile_sum = sum_diff.crop((x, y, x + tw, y + th))
                if tile_sum.getextrema()[1] >= DELTA_SUM_THRESHOLD:
                    tiles.append((x, y, curr_img.crop((x, y, x + tw, y + th))))
        return tiles

    def _encode_delta_payload(self, left: int, top: int, width: int, height: int,
                              tiles: list[tuple[int, int, Image.Image]]) -> bytes:
        body = bytearray(DELTA_FRAME_HEADER.pack(
            left, top, width, height, FRAME_TILE_SIZE, len(tiles)))
        for x, y, tile in tiles:
            tw, th = tile.size
            buf = io.BytesIO()
            tile.save(buf, format="JPEG", quality=self._stream_quality, optimize=False)
            jpeg = buf.getvalue()
            body.extend(TILE_HEADER.pack(x, y, tw, th, len(jpeg)))
            body.extend(jpeg)
        return bytes(body)

    # ── input handlers ────────────────────────────────────────────

    def _apply_mouse(self, ev: dict) -> None:
        set_mouse_position(int(ev.get("x", 0)), int(ev.get("y", 0)))
        act = ev.get("action")
        if act in ("down", "up"):
            mouse_button(act, ev.get("button", "left"))
        elif act == "wheel":
            mouse_wheel(int(ev.get("delta", 0)))

    def _apply_key(self, ev: dict) -> None:
        try:
            keyboard_event(ev.get("action", "down"), ev["key"])
        except Exception:
            pass

    def _apply_text(self, ev: dict) -> None:
        text = str(ev.get("text", ""))
        if text:
            try:
                send_text(text)
            except Exception as exc:
                self._log(f"Text input failed: {exc}")

    def _apply_stream_config(self, ev: dict) -> None:
        w, h = int(ev.get("max_width", self._stream_max[0])), int(ev.get("max_height", self._stream_max[1]))
        q = int(ev.get("quality", self._stream_quality))
        self._stream_max = (0, 0) if w <= 0 or h <= 0 else (w, h)
        self._stream_quality = max(30, min(95, q))
        # Fixed at 120 FPS; ignore any fps value from the controller.
        self._frame_interval = DEFAULT_FRAME_INTERVAL
        label = "native" if self._stream_max == (0, 0) else f"{self._stream_max[0]}x{self._stream_max[1]}"
        self._log(f"Stream updated: {label}, JPEG quality {self._stream_quality}, 120 FPS")

    # ── session management ────────────────────────────────────────

    def _stop_session(self) -> None:
        self._session_stop.set()
        # In proxy mode, send CLOSE through proxy socket so it gets relayed
        sock = self.proxy_socket if self._proxy_relay_active else self.client_socket
        if sock:
            try:
                send_packet(sock, PKG_CLOSE, encode_json({"reason": "Local user stopped session."}))
            except OSError:
                pass
        if self.client_socket:
            try:
                self.client_socket.close()
            except OSError:
                pass
        self._cleanup_session()

    def _cleanup_session(self) -> None:
        self._session_active = False
        self._session_stop.set()
        self._proxy_relay_active = False
        if self.client_socket:
            try:
                self.client_socket.close()
            except OSError:
                pass
        self.client_socket = None
        self.cipher = None
        self._last_frame = None
        self._frame_counter = 0
        self.root.after(0, lambda: self.stop_button.configure(state="disabled"))
        self._set_banner(False, "No active session. Remote control is disabled.")
        self._log("Idle")
        # The single _proxy_loop keeps running; restore its poll timeout.
        if self.proxy_socket is not None:
            try:
                self.proxy_socket.settimeout(2.0)
            except OSError:
                pass

    def _on_close(self) -> None:
        self._stop.set()
        self._stop_session()
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
        if self.proxy_socket:
            try:
                self.proxy_socket.close()
            except OSError:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    AgentApp().run()