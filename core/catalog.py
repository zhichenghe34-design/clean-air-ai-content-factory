from __future__ import annotations

import ctypes
import json
import os
import shutil
import string
import subprocess
from pathlib import Path


class CatalogError(RuntimeError):
    pass


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


class PackageCatalog:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            catalog = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CatalogError(f"能力目录不存在: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise CatalogError(f"能力目录 JSON 无效: {exc}") from exc
        self.validate(catalog)
        return catalog

    @staticmethod
    def validate(catalog: dict) -> None:
        policy = catalog.get("policy", {})
        if policy.get("agent_may_browse_for_install_sources") is not False:
            raise CatalogError("安全策略必须禁止 Agent 搜索安装源")
        if policy.get("agent_may_submit_urls") is not False:
            raise CatalogError("安全策略必须禁止 Agent 提交下载地址")
        if policy.get("auto_install_enabled") is not False:
            raise CatalogError("首版能力目录不得开启自动安装")

        package_ids = [item.get("id") for item in catalog.get("packages", [])]
        if not package_ids or any(not item for item in package_ids):
            raise CatalogError("能力目录中存在缺少 ID 的能力包")
        if len(package_ids) != len(set(package_ids)):
            raise CatalogError("能力包 ID 必须唯一")

        allowed_schemes = ("https://", "http://127.0.0.1", "http://localhost")
        for package in catalog.get("packages", []):
            for source in package.get("sources", []):
                url = str(source.get("url", ""))
                if not url.startswith(allowed_schemes):
                    raise CatalogError(f"{package['id']} 包含不允许的来源地址")

    @staticmethod
    def select_profile(catalog: dict, vram_gb: float) -> dict:
        for profile in catalog.get("hardware_profiles", []):
            selector = profile.get("selection", {})
            minimum = float(selector.get("min_vram_gb", 0))
            maximum = selector.get("max_vram_gb_exclusive")
            if vram_gb >= minimum and (maximum is None or vram_gb < float(maximum)):
                return profile
        raise CatalogError(f"没有覆盖 {vram_gb:.1f}GB 显存的硬件档位")

    @staticmethod
    def recommendations(catalog: dict, profile_id: str) -> list[dict]:
        return [
            package
            for package in catalog.get("packages", [])
            if profile_id in package.get("recommended_profiles", [])
        ]


class HardwareProbe:
    @staticmethod
    def probe() -> dict:
        gpu = HardwareProbe._gpu()
        memory = HardwareProbe._memory()
        return {
            "gpu": gpu,
            "memory": memory,
            "disks": HardwareProbe._disks(),
            "platform": os.name,
        }

    @staticmethod
    def _gpu() -> dict:
        command = [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
            first = next(line for line in result.stdout.splitlines() if line.strip())
            name, memory_mib, driver = [part.strip() for part in first.split(",", 2)]
            return {
                "available": True,
                "name": name,
                "vram_mib": int(memory_mib),
                "vram_gb": round(int(memory_mib) / 1024, 1),
                "driver": driver,
            }
        except (FileNotFoundError, subprocess.SubprocessError, StopIteration, ValueError):
            return {"available": False, "name": "未检测到 NVIDIA GPU", "vram_mib": 0, "vram_gb": 0.0}

    @staticmethod
    def _memory() -> dict:
        if os.name == "nt":
            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return {
                    "total_gb": round(status.total_physical / 1024**3, 1),
                    "available_gb": round(status.available_physical / 1024**3, 1),
                }
        return {"total_gb": None, "available_gb": None}

    @staticmethod
    def _disks() -> list[dict]:
        roots = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
        if not roots:
            roots = [str(Path.cwd().anchor or "/")]
        disks = []
        for root in roots:
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            disks.append(
                {
                    "root": root,
                    "total_gb": round(usage.total / 1024**3, 1),
                    "free_gb": round(usage.free / 1024**3, 1),
                }
            )
        return disks

