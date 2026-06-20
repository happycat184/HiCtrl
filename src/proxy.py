from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from dataclasses import dataclass, field

from common.protocol import (
    PKG_CLOSE,
    PKG_HELLO,
    PKG_HELLO_ACK,
    PKG_PROXY_AGENT_LIST,
    PKG_PROXY_CODE_VERIFY,
    PKG_PROXY_CODE_VERIFY_RESULT,
    PKG_PROXY_RELAY,
    PKG_PROXY_REQUEST_CODE,
    PKG_PROXY_SESSION_READY,
    PKG_SESSION_ACK,
    PKG_SESSION_KEY,
    CONTROL_PACKET_IDS,
    decode_json,
    encode_json,
    read_packet,
    send_packet,
)

RELAY_PACKET_IDS = CONTROL_PACKET_IDS | {PKG_SESSION_KEY, PKG_SESSION_ACK, PKG_CLOSE}


@dataclass
class AgentInfo:
    agent_id: str
    agent_name: str
    public_key_pem: str
    socket: socket.socket
    address: tuple


@dataclass
class ControllerInfo:
    controller_id: str
    controller_name: str
    socket: socket.socket
    address: tuple


@dataclass
class PendingSession:
    code: str
    code_expires_at: float
    controller_id: str
    agent_id: str
    agent_accepted: bool = False
    controller_verified: bool = False


@dataclass
class ProxyConfig:
    host: str = "0.0.0.0"
    port: int = 48222
    default_code_validity_seconds: int = 300


def generate_code() -> str:
    return "".join(random.choices("0123456789", k=8))


class ProxyServer:
    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()
        self.agents: dict[str, AgentInfo] = {}
        self.controllers: dict[str, ControllerInfo] = {}
        self.pending_sessions: dict[str, PendingSession] = {}
        self._relay_map: dict[str, str] = {}  # agent_id -> controller_id, controller_id -> agent_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None

    # ── server lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.config.host, self.config.port))
        server.listen(50)
        server.settimeout(1.0)
        self._server = server
        print(f"Proxy listening on {self.config.host}:{self.config.port}")
        while not self._stop.is_set():
            try:
                client, addr = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._on_client, args=(client, addr), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass

    # ── client dispatch ───────────────────────────────────────────

    def _on_client(self, client: socket.socket, addr: tuple) -> None:
        try:
            pkt = read_packet(client)
            if pkt.package_id != PKG_HELLO:
                client.close()
                return
            data = decode_json(pkt.payload)
            role = data.get("role", "")
            if role == "agent":
                self._handle_agent(client, addr, data)
            elif role == "controller":
                self._handle_controller(client, addr, data)
            else:
                client.close()
        except Exception:
            try:
                client.close()
            except OSError:
                pass

    # ── agent ─────────────────────────────────────────────────────

    def _handle_agent(self, sock: socket.socket, addr: tuple, data: dict) -> None:
        agent_id = f"agent_{addr[0]}_{addr[1]}_{int(time.time())}"
        agent_name = data.get("agent_name", "unknown")
        public_key = data.get("public_key_pem", "")

        send_packet(sock, PKG_HELLO_ACK, encode_json({
            "app": "HiCtrl", "version": 1, "role": "proxy", "agent_id": agent_id,
        }))

        info = AgentInfo(agent_id=agent_id, agent_name=agent_name,
                         public_key_pem=public_key, socket=sock, address=addr)
        with self._lock:
            self.agents[agent_id] = info

        self._broadcast_agent_list()
        self._agent_loop(sock, agent_id)

        with self._lock:
            self.agents.pop(agent_id, None)
            # clean up pending sessions for this agent
            for code, sess in list(self.pending_sessions.items()):
                if sess.agent_id == agent_id:
                    del self.pending_sessions[code]
        self._broadcast_agent_list()
        try:
            sock.close()
        except OSError:
            pass

    def _agent_loop(self, sock: socket.socket, agent_id: str) -> None:
        sock.settimeout(60.0)
        clean_close = False
        while not self._stop.is_set():
            try:
                pkt = read_packet(sock)
            except socket.timeout:
                continue
            except Exception:
                break
            if pkt.package_id == PKG_CLOSE:
                # Agent explicitly ended the session. Relay to the paired
                # controller, tear down the relay pair, but keep the agent
                # connected to the proxy so it can accept future requests.
                self._relay_and_cleanup(agent_id, pkt.package_id, pkt.payload)
                clean_close = True
                continue
            if pkt.package_id == PKG_PROXY_CODE_VERIFY:
                self._on_agent_code_verify(agent_id, decode_json(pkt.payload))
            elif pkt.package_id in RELAY_PACKET_IDS:
                self._relay_from(agent_id, pkt.package_id, pkt.payload)
        if not clean_close:
            self._relay_and_cleanup(agent_id, PKG_CLOSE, encode_json({"reason": "Agent disconnected."}))

    # ── controller ────────────────────────────────────────────────

    def _handle_controller(self, sock: socket.socket, addr: tuple, data: dict) -> None:
        cid = f"ctrl_{addr[0]}_{addr[1]}_{int(time.time())}"
        cname = data.get("controller_name", "unknown")

        send_packet(sock, PKG_HELLO_ACK, encode_json({
            "app": "HiCtrl", "version": 1, "role": "proxy", "controller_id": cid,
        }))

        info = ControllerInfo(controller_id=cid, controller_name=cname,
                              socket=sock, address=addr)
        with self._lock:
            self.controllers[cid] = info

        self._controller_loop(sock, cid)

        with self._lock:
            self.controllers.pop(cid, None)
        try:
            sock.close()
        except OSError:
            pass

    def _controller_loop(self, sock: socket.socket, cid: str) -> None:
        sock.settimeout(60.0)
        clean_close = False
        while not self._stop.is_set():
            try:
                pkt = read_packet(sock)
            except socket.timeout:
                continue
            except Exception:
                break
            if pkt.package_id == PKG_CLOSE:
                # Controller explicitly ended the session. Relay to the paired
                # agent, tear down the relay pair, but keep the controller
                # connected to the proxy so it can request another code later.
                self._relay_and_cleanup(cid, pkt.package_id, pkt.payload)
                clean_close = True
                continue
            if pkt.package_id == PKG_PROXY_AGENT_LIST:
                self._send_agent_list(sock)
            elif pkt.package_id == PKG_PROXY_REQUEST_CODE:
                self._on_ctrl_request_code(cid, decode_json(pkt.payload))
            elif pkt.package_id == PKG_PROXY_CODE_VERIFY:
                self._on_ctrl_code_verify(cid, decode_json(pkt.payload))
            elif pkt.package_id in RELAY_PACKET_IDS:
                self._relay_from(cid, pkt.package_id, pkt.payload)
        if not clean_close:
            self._relay_and_cleanup(cid, PKG_CLOSE, encode_json({"reason": "Controller disconnected."}))

    # ── agent list ────────────────────────────────────────────────

    def _send_agent_list(self, sock: socket.socket) -> None:
        with self._lock:
            items = [{"agent_id": a.agent_id, "agent_name": a.agent_name,
                       "address": f"{a.address[0]}:{a.address[1]}"}
                     for a in self.agents.values()]
        send_packet(sock, PKG_PROXY_AGENT_LIST, encode_json({"agents": items}))

    def _broadcast_agent_list(self) -> None:
        """Send the current agent list to every connected controller."""
        with self._lock:
            items = [{"agent_id": a.agent_id, "agent_name": a.agent_name,
                       "address": f"{a.address[0]}:{a.address[1]}"}
                     for a in self.agents.values()]
            controllers = list(self.controllers.values())
        payload = encode_json({"agents": items})
        for ctrl in controllers:
            try:
                send_packet(ctrl.socket, PKG_PROXY_AGENT_LIST, payload)
            except OSError:
                pass

    # ── code flow ─────────────────────────────────────────────────
    # 1. controller → PKG_PROXY_REQUEST_CODE {"agent_id": ...}
    # 2. proxy generates 8-digit code, sends to agent: PKG_PROXY_REQUEST_CODE
    #    {"code":..., "validity":..., "controller_name":...}
    # 3. agent displays code, user consents → agent sends PKG_PROXY_CODE_VERIFY
    #    {"code":..., "accepted": true}
    # 4. proxy marks agent_accepted, notifies controller:
    #    PKG_PROXY_CODE_VERIFY_RESULT {"success": true}
    # 5. controller enters code → PKG_PROXY_CODE_VERIFY {"code": ...}
    # 6. proxy checks code matches + agent accepted → PKG_PROXY_SESSION_READY to both

    def _on_ctrl_request_code(self, cid: str, data: dict) -> None:
        agent_id = data.get("agent_id", "")
        print(f"[PROXY] Controller {cid} requests code for agent {agent_id}")
        with self._lock:
            agent = self.agents.get(agent_id)
            ctrl = self.controllers.get(cid)
        if not agent or not ctrl:
            print(f"[PROXY] Agent {agent_id} or controller {cid} not found")
            send_packet(ctrl.socket if ctrl else None, PKG_PROXY_CODE_VERIFY_RESULT,
                        encode_json({"success": False, "reason": "Agent not found."}))
            return

        code = generate_code()
        validity = self.config.default_code_validity_seconds
        expires = time.time() + validity

        session = PendingSession(
            code=code, code_expires_at=expires,
            controller_id=cid, agent_id=agent_id,
        )
        with self._lock:
            self.pending_sessions[code] = session

        print(f"[PROXY] Sending REQUEST_CODE to agent {agent_id}: code={code}")
        send_packet(agent.socket, PKG_PROXY_REQUEST_CODE, encode_json({
            "code": code,
            "validity_seconds": validity,
            "controller_name": ctrl.controller_name,
        }))

    def _on_agent_code_verify(self, agent_id: str, data: dict) -> None:
        code = data.get("code", "")
        accepted = data.get("accepted", False)
        print(f"[PROXY] Agent {agent_id} code verify: code={code}, accepted={accepted}")

        with self._lock:
            session = self.pending_sessions.get(code)
            if not session:
                print(f"[PROXY] No pending session for code {code}")
                return
            if session.agent_id != agent_id:
                print(f"[PROXY] Agent ID mismatch")
                return
            if time.time() > session.code_expires_at:
                del self.pending_sessions[code]
                print(f"[PROXY] Code expired")
                return
            if not accepted:
                del self.pending_sessions[code]
                print(f"[PROXY] Agent rejected")
                return
            session.agent_accepted = True
            ctrl = self.controllers.get(session.controller_id)

        if ctrl:
            print(f"[PROXY] Notifying controller {session.controller_id}")
            send_packet(ctrl.socket, PKG_PROXY_CODE_VERIFY_RESULT,
                        encode_json({"success": True}))

    def _on_ctrl_code_verify(self, cid: str, data: dict) -> None:
        code = data.get("code", "")

        with self._lock:
            session = self.pending_sessions.get(code)
            ctrl = self.controllers.get(cid)
            if not session or not ctrl:
                if ctrl:
                    send_packet(ctrl.socket, PKG_PROXY_CODE_VERIFY_RESULT,
                                encode_json({"success": False, "reason": "Invalid code."}))
                return
            if session.controller_id != cid:
                send_packet(ctrl.socket, PKG_PROXY_CODE_VERIFY_RESULT,
                            encode_json({"success": False, "reason": "Code mismatch."}))
                return
            if time.time() > session.code_expires_at:
                del self.pending_sessions[code]
                send_packet(ctrl.socket, PKG_PROXY_CODE_VERIFY_RESULT,
                            encode_json({"success": False, "reason": "Code expired."}))
                return
            if not session.agent_accepted:
                send_packet(ctrl.socket, PKG_PROXY_CODE_VERIFY_RESULT,
                            encode_json({"success": False, "reason": "Agent has not accepted yet."}))
                return

            session.controller_verified = True
            agent = self.agents.get(session.agent_id)

        if agent and ctrl:
            with self._lock:
                self._relay_map[session.agent_id] = cid
                self._relay_map[cid] = session.agent_id
            send_packet(agent.socket, PKG_PROXY_SESSION_READY,
                        encode_json({"controller_id": cid}))
            send_packet(ctrl.socket, PKG_PROXY_SESSION_READY,
                        encode_json({
                            "agent_id": session.agent_id,
                            "public_key_pem": agent.public_key_pem,
                        }))

        with self._lock:
            self.pending_sessions.pop(code, None)

    def _relay_from(self, sender_id: str, pkg_id: int, payload: bytes) -> None:
        with self._lock:
            target_id = self._relay_map.get(sender_id)
            if not target_id:
                return
            target_agent = self.agents.get(target_id)
            target_ctrl = self.controllers.get(target_id)
        target = target_agent or target_ctrl
        if target:
            try:
                send_packet(target.socket, pkg_id, payload)
            except OSError:
                pass

    def _relay_and_cleanup(self, sender_id: str, pkg_id: int, payload: bytes) -> None:
        """Send a final packet to the paired peer (if any) and remove the relay pair."""
        with self._lock:
            target_id = self._relay_map.pop(sender_id, None)
            if target_id:
                self._relay_map.pop(target_id, None)
                target_agent = self.agents.get(target_id)
                target_ctrl = self.controllers.get(target_id)
        target = target_agent or target_ctrl
        if target:
            try:
                send_packet(target.socket, pkg_id, payload)
            except OSError:
                pass


def load_config(config_path: str | None = None) -> ProxyConfig:
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        p = data.get("proxy", {})
        return ProxyConfig(
            host=p.get("host", "0.0.0.0"),
            port=p.get("port", 48222),
            default_code_validity_seconds=p.get("default_code_validity_seconds", 300),
        )
    except (OSError, json.JSONDecodeError):
        return ProxyConfig()


def main() -> None:
    cfg = load_config()
    srv = ProxyServer(cfg)
    try:
        srv.start()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()