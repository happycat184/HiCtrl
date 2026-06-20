from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def generate_rsa_keypair() -> tuple[rsa.RSAPrivateKey, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem


def load_public_key(public_pem: bytes):
    return serialization.load_pem_public_key(public_pem)


def rsa_encrypt(public_pem: bytes, plaintext: bytes) -> bytes:
    public_key = load_public_key(public_pem)
    return public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


def rsa_decrypt(private_key: rsa.RSAPrivateKey, ciphertext: bytes) -> bytes:
    return private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


@dataclass(slots=True)
class SessionMaterial:
    key: bytes
    controller_to_agent_prefix: bytes
    agent_to_controller_prefix: bytes

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "key": b64encode(self.key),
                "controller_to_agent_prefix": b64encode(
                    self.controller_to_agent_prefix
                ),
                "agent_to_controller_prefix": b64encode(
                    self.agent_to_controller_prefix
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "SessionMaterial":
        data = json.loads(payload.decode("utf-8"))
        return cls(
            key=b64decode(data["key"]),
            controller_to_agent_prefix=b64decode(data["controller_to_agent_prefix"]),
            agent_to_controller_prefix=b64decode(data["agent_to_controller_prefix"]),
        )


def build_session_material() -> SessionMaterial:
    return SessionMaterial(
        key=AESGCM.generate_key(bit_length=256),
        controller_to_agent_prefix=os.urandom(4),
        agent_to_controller_prefix=os.urandom(4),
    )


class DuplexCipher:
    def __init__(self, key: bytes, send_prefix: bytes, recv_prefix: bytes) -> None:
        self._aesgcm = AESGCM(key)
        self._send_prefix = send_prefix
        self._recv_prefix = recv_prefix
        self._send_counter = 0
        self._recv_counter = 0

    @staticmethod
    def _nonce(prefix: bytes, counter: int) -> bytes:
        return prefix + counter.to_bytes(8, "big")

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = self._nonce(self._send_prefix, self._send_counter)
        self._send_counter += 1
        return self._aesgcm.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphertext: bytes) -> bytes:
        nonce = self._nonce(self._recv_prefix, self._recv_counter)
        self._recv_counter += 1
        return self._aesgcm.decrypt(nonce, ciphertext, None)
