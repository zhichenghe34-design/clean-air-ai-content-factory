from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class SecretStorageError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def protect_secret(value: str) -> dict[str, str | int]:
    if os.name != "nt":
        raise SecretStorageError("非Windows系统不支持DPAPI持久化")
    raw = value.encode("utf-8")
    input_blob, keepalive = _blob(raw)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "ShiyiContentFactory",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise SecretStorageError(str(ctypes.WinError()))
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del keepalive
    return {
        "version": 2,
        "scheme": "dpapi-current-user",
        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
    }


def unprotect_secret(payload: dict) -> str:
    if os.name != "nt":
        raise SecretStorageError("非Windows系统不能解密DPAPI密钥")
    if payload.get("version") != 2 or payload.get("scheme") != "dpapi-current-user":
        raise SecretStorageError("未知的密钥存储格式")
    try:
        encrypted = base64.b64decode(str(payload["ciphertext"]), validate=True)
    except Exception as exc:
        raise SecretStorageError("密钥密文格式无效") from exc
    input_blob, keepalive = _blob(encrypted)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise SecretStorageError(str(ctypes.WinError()))
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del keepalive
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStorageError("DPAPI密钥不是UTF-8文本") from exc
