from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass


MAGIC_START = b"\x0F\x0A"
MAGIC_END = b"\x0A\x0F"
HEADER_STRUCT = struct.Struct(">2sHQ")
HEADER_SIZE = HEADER_STRUCT.size
FOOTER_SIZE = len(MAGIC_END)

# Full-frame header: left, top, virtual_w, virtual_h, preview_w, preview_h
FRAME_HEADER = struct.Struct(">iiIIII")

# Delta-frame header: left, top, width, height, tile_size, tile_count
DELTA_FRAME_HEADER = struct.Struct(">iiIIHH")

# Per-tile header inside a delta frame: tile_x, tile_y, tile_w, tile_h, jpeg_len
TILE_HEADER = struct.Struct(">HHHHI")


PKG_HELLO = 0x1001
PKG_HELLO_ACK = 0x1002

PKG_ASSIST_REQUEST = 0x2001
PKG_ASSIST_RESPONSE = 0x2002

PKG_SESSION_KEY = 0x3001
PKG_SESSION_ACK = 0x3002

PKG_SCREEN_FRAME = 0x4001
PKG_SCREEN_FRAME_DELTA = 0x4008
PKG_MOUSE_EVENT = 0x4002
PKG_KEY_EVENT = 0x4003
PKG_HEARTBEAT = 0x4004
PKG_STATUS = 0x4005
PKG_TEXT_INPUT = 0x4006
PKG_STREAM_CONFIG = 0x4007

PKG_CLOSE = 0x5001

PKG_PROXY_REGISTER = 0x6001
PKG_PROXY_REGISTER_ACK = 0x6002
PKG_PROXY_AGENT_LIST = 0x6003
PKG_PROXY_REQUEST_CODE = 0x6004
PKG_PROXY_CODE_VERIFY = 0x6005
PKG_PROXY_CODE_VERIFY_RESULT = 0x6006
PKG_PROXY_CONNECT = 0x6007
PKG_PROXY_SESSION_READY = 0x6008
PKG_PROXY_RELAY = 0x6009

CONTROL_PACKET_IDS = {
    PKG_SCREEN_FRAME,
    PKG_SCREEN_FRAME_DELTA,
    PKG_MOUSE_EVENT,
    PKG_KEY_EVENT,
    PKG_HEARTBEAT,
    PKG_STATUS,
    PKG_TEXT_INPUT,
    PKG_STREAM_CONFIG,
}


class ProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class Packet:
    package_id: int
    payload: bytes


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        block = sock.recv(length - len(chunks))
        if not block:
            raise ConnectionError("Socket closed while reading packet data.")
        chunks.extend(block)
    return bytes(chunks)


def read_packet(sock: socket.socket) -> Packet:
    header = recv_exact(sock, HEADER_SIZE)
    magic, package_id, payload_length = HEADER_STRUCT.unpack(header)
    if magic != MAGIC_START:
        raise ProtocolError(f"Invalid packet prefix: {magic!r}")
    payload = recv_exact(sock, payload_length)
    footer = recv_exact(sock, FOOTER_SIZE)
    if footer != MAGIC_END:
        raise ProtocolError(f"Invalid packet suffix: {footer!r}")
    return Packet(package_id=package_id, payload=payload)


def send_packet(sock: socket.socket, package_id: int, payload: bytes) -> None:
    header = HEADER_STRUCT.pack(MAGIC_START, package_id, len(payload))
    sock.sendall(header + payload + MAGIC_END)


def encode_json(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def decode_json(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))
