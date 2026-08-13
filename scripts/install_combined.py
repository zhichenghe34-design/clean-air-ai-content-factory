from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


INSTALLER_CONTRACT = "SHIYI_COMBINED_INSTALLER_V1"
ATOMIC_PUBLISH_CONTRACT = "SHIYI_ATOMIC_UPGRADE_WITH_ROLLBACK_V1"
MINIMUM_FREE_BYTES = 2 * 1024 * 1024 * 1024
PREFERRED_DRIVE_LETTER = "D:"
PRODUCT_DIRECTORY_NAME = "时宜Agent内容工厂"
APPLICATION_DIRECTORY_NAME = "App"
FALLBACK_PARENT_NAME = "Programs"
PACKAGE_MANIFEST_NAME = "PACKAGE-MANIFEST.json"
PACKAGE_PRODUCT = "时宜 Agent 内容工厂"
PACKAGE_KIND = "windows_x64_combined_portable"
PRODUCT_OWNERSHIP_MARKERS = (
    "启动时宜Agent内容工厂.bat",
    "安装到D盘.bat",
    "tools/verify_combined_portable.pyc",
)


class InstallerError(RuntimeError):
    """An expected, user-readable installer failure."""


@dataclass(frozen=True)
class PreferredDriveStatus:
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class InstallSelection:
    target: Path
    uses_preferred_drive: bool
    preferred_drive_reasons: tuple[str, ...]


def assess_preferred_drive(
    *,
    exists: bool,
    fixed_local: bool | None = None,
    filesystem: str | None = None,
    reparse: bool | None = None,
    writable: bool | None = None,
    free_bytes: int | None = None,
) -> PreferredDriveStatus:
    """Evaluate the exact D-drive contract without touching a real disk."""

    if not exists:
        return PreferredDriveStatus(False, ("D 盘不存在",))

    reasons: list[str] = []
    if fixed_local is not True:
        reasons.append("D 盘不是本机固定磁盘")
    if not isinstance(filesystem, str) or filesystem.casefold() != "ntfs":
        reasons.append("D 盘不是 NTFS 文件系统")
    if reparse is not False:
        reasons.append("D 盘根目录是重解析点，不能作为安全安装目标")
    if writable is not True:
        reasons.append("当前 Windows 用户不能写入 D 盘")
    if free_bytes is None:
        reasons.append("无法读取 D 盘可用空间")
    elif free_bytes < MINIMUM_FREE_BYTES:
        reasons.append("D 盘可用空间不足 2GB")
    return PreferredDriveStatus(not reasons, tuple(reasons))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    junction = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or (callable(junction) and junction())
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _windows_drive_type(root: Path) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))


def _windows_filesystem_name(root: Path) -> str:
    import ctypes

    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    maximum_component_length = ctypes.c_ulong()
    filesystem_flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        str(root),
        volume_name,
        len(volume_name),
        ctypes.byref(serial_number),
        ctypes.byref(maximum_component_length),
        ctypes.byref(filesystem_flags),
        filesystem_name,
        len(filesystem_name),
    )
    if not ok:
        raise OSError("Windows 无法读取 D 盘文件系统类型")
    return filesystem_name.value


def _probe_directory_writable(root: Path) -> bool:
    probe = root / f".shiyi-install-write-probe-{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, b"probe")
        os.close(descriptor)
        descriptor = None
        probe.unlink()
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.path.lexists(probe):
            try:
                probe.unlink()
            except OSError:
                pass


def inspect_preferred_d_drive() -> PreferredDriveStatus:
    if os.name != "nt":
        return PreferredDriveStatus(False, ("当前系统不是受支持的 Windows",))
    root = Path(PREFERRED_DRIVE_LETTER + os.sep)
    if not root.is_dir():
        return assess_preferred_drive(exists=False)
    try:
        fixed_local = _windows_drive_type(root) == 3  # DRIVE_FIXED
    except OSError:
        fixed_local = False
    try:
        filesystem = _windows_filesystem_name(root)
    except OSError:
        filesystem = None
    reparse = _is_reparse_point(root)
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        free_bytes = None
    # Never create even the tiny write probe on a removable, network, non-NTFS,
    # reparse, or already-too-full D drive.  Those drives are ineligible before
    # write access matters.
    preflight = assess_preferred_drive(
        exists=True,
        fixed_local=fixed_local,
        filesystem=filesystem,
        reparse=reparse,
        writable=True,
        free_bytes=free_bytes,
    )
    if not preflight.eligible:
        return preflight
    writable = _probe_directory_writable(root)
    return assess_preferred_drive(
        exists=True,
        fixed_local=fixed_local,
        filesystem=filesystem,
        reparse=reparse,
        writable=writable,
        free_bytes=free_bytes,
    )


def select_install_target(
    status: PreferredDriveStatus,
    environment: Mapping[str, str] | None = None,
) -> InstallSelection:
    if status.eligible:
        root = Path(PREFERRED_DRIVE_LETTER + os.sep)
        return InstallSelection(
            root / PRODUCT_DIRECTORY_NAME / APPLICATION_DIRECTORY_NAME,
            True,
            (),
        )

    values = os.environ if environment is None else environment
    raw_local_app_data = values.get("LOCALAPPDATA", "").strip()
    if not raw_local_app_data:
        raise InstallerError("Windows 未提供 LOCALAPPDATA，无法选择安全的备用安装目录")
    local_app_data = Path(raw_local_app_data)
    if not local_app_data.is_absolute():
        raise InstallerError("LOCALAPPDATA 不是绝对路径，无法选择安全的备用安装目录")
    return InstallSelection(
        local_app_data
        / FALLBACK_PARENT_NAME
        / PRODUCT_DIRECTORY_NAME
        / APPLICATION_DIRECTORY_NAME,
        False,
        status.reasons,
    )


def _verify_package_folder(folder: Path) -> list[str]:
    verifier_path = folder / "tools" / "verify_combined_portable.pyc"
    if not verifier_path.is_file():
        verifier_path = folder / "tools" / "verify_combined_portable.py"
    if not verifier_path.is_file() or _is_reparse_point(verifier_path):
        return ["缺少受清单约束的安装包验证器"]
    try:
        namespace = runpy.run_path(
            str(verifier_path),
            run_name=f"shiyi_install_verifier_{uuid.uuid4().hex}",
        )
        verify_folder = namespace.get("verify_folder")
        if not callable(verify_folder):
            return ["安装包验证器没有可调用的 verify_folder"]
        errors = verify_folder(folder)
    except Exception as exc:
        return [f"安装包验证器执行失败：{exc}"]
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        return ["安装包验证器返回了无效结果"]
    return errors


def _existing_anchor(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            raise InstallerError(f"无法找到安装目标所在的磁盘：{path}")
        current = parent
    if not current.is_dir():
        raise InstallerError(f"安装目标的上级路径不是文件夹：{current}")
    return current


def _reject_reparse_ancestors(path: Path) -> None:
    current = path
    while True:
        if os.path.lexists(current) and _is_reparse_point(current):
            raise InstallerError(f"安装路径包含重解析点，已拒绝：{current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _copy_package_tree(source: Path, destination: Path) -> None:
    for current, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        if _is_reparse_point(current_path):
            raise InstallerError("源安装包包含符号链接、Junction 或其他重解析目录")
        relative = current_path.relative_to(source)
        destination_directory = destination / relative
        destination_directory.mkdir(parents=True, exist_ok=True)
        kept_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            child = current_path / name
            if _is_reparse_point(child):
                raise InstallerError("源安装包包含符号链接、Junction 或其他重解析目录")
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names, key=str.casefold):
            child = current_path / name
            if _is_reparse_point(child) or not child.is_file():
                raise InstallerError("源安装包包含非普通文件或重解析文件")
            shutil.copy2(child, destination_directory / name)


def _inspect_existing_install(
    source: Path,
    target: Path,
    verifier: Callable[[Path], list[str]],
) -> tuple[bool, bool]:
    """Return ``(verified, same_version)`` for an existing App directory.

    An ordinary but damaged App can be repaired from a fully verified package.
    Reparse points and non-directory targets remain fail-closed because moving or
    deleting them could escape the product directory.
    """

    if not target.is_dir() or _is_reparse_point(target):
        raise InstallerError(f"已有安装不是可验证的普通文件夹，未覆盖：{target}")
    target_errors = verifier(target)
    if target_errors:
        if not _has_explicit_product_identity(target):
            raise InstallerError(
                "目标文件夹不是可确认归属的时宜内容工厂 App，未移动、未覆盖、未删除："
                + str(target)
            )
        _reject_reparse_tree(target)
        return False, False
    source_manifest = source / PACKAGE_MANIFEST_NAME
    target_manifest = target / PACKAGE_MANIFEST_NAME
    try:
        identical = source_manifest.read_bytes() == target_manifest.read_bytes()
    except OSError as exc:
        raise InstallerError("已有安装无法确认版本，未覆盖任何文件") from exc
    return True, identical


def _has_explicit_product_identity(target: Path) -> bool:
    """Recognize a damaged install without claiming an arbitrary folder.

    Repair is deliberately narrower than upgrade: the target must still carry
    a parseable manifest with this exact product and package-kind identity.
    Missing, malformed, or foreign manifests are never moved or deleted.
    """

    manifest_path = target / PACKAGE_MANIFEST_NAME
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") in {2, 3}
        and payload.get("product") == PACKAGE_PRODUCT
        and payload.get("package_kind") == PACKAGE_KIND
        and isinstance(payload.get("source"), dict)
        and isinstance(payload["source"].get("repository_commit"), str)
        and len(payload["source"]["repository_commit"]) == 40
        and all(
            (target / Path(relative)).is_file()
            and not _is_reparse_point(target / Path(relative))
            for relative in PRODUCT_OWNERSHIP_MARKERS
        )
    )


def _reject_reparse_tree(root: Path) -> None:
    """Reject a tree that cannot be moved and removed within a known boundary."""

    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if _is_reparse_point(current_path):
            raise InstallerError(f"已有安装包含重解析目录，不能安全修复：{current_path}")
        for name in directory_names:
            child = current_path / name
            if _is_reparse_point(child):
                raise InstallerError(f"已有安装包含重解析目录，不能安全修复：{child}")
        for name in file_names:
            child = current_path / name
            if _is_reparse_point(child):
                raise InstallerError(f"已有安装包含重解析文件，不能安全修复：{child}")


def _remove_owned_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if not path.is_dir() or _is_reparse_point(path):
        raise InstallerError(f"安装器管理的旧版本路径不安全，未删除：{path}")
    _reject_reparse_tree(path)
    shutil.rmtree(path)


def _restore_after_failed_publish(
    *,
    target: Path,
    displaced: Path,
    previous: Path | None,
    previous_displaced: Path | None,
    rename: Callable[[Path, Path], None],
) -> None:
    """Best-effort rollback; raise a clear hard failure if identity cannot recover."""

    restore_errors: list[str] = []
    try:
        if os.path.lexists(displaced) and not os.path.lexists(target):
            rename(displaced, target)
    except OSError as exc:
        restore_errors.append(f"恢复原 App 失败：{exc}")
    if previous is not None and previous_displaced is not None:
        try:
            if os.path.lexists(previous_displaced) and not os.path.lexists(previous):
                rename(previous_displaced, previous)
        except OSError as exc:
            restore_errors.append(f"恢复上一回滚版本失败：{exc}")
    if restore_errors:
        raise InstallerError("新版发布失败，且自动恢复未完整成功：" + "；".join(restore_errors))


def _publish_staged_install(
    *,
    staging: Path,
    target: Path,
    existing_verified: bool | None,
    verifier: Callable[[Path], list[str]],
    rename: Callable[[Path, Path], None],
) -> None:
    """Publish a fresh install, upgrade, or repair using sibling renames.

    Verified upgrades retain exactly one ``App.previous`` rollback.  A damaged
    App is restored if publishing fails, then removed after a successful repair;
    it is never advertised as a safe rollback version.
    """

    if existing_verified is None:
        rename(staging, target)
        return

    parent = target.parent
    displaced = parent / f".App-displaced-{uuid.uuid4().hex}"
    previous = target.with_name(target.name + ".previous") if existing_verified else None
    previous_displaced: Path | None = None
    if previous is not None and os.path.lexists(previous):
        if not previous.is_dir() or _is_reparse_point(previous):
            raise InstallerError(f"回滚版本路径不安全，未升级：{previous}")
        if not _has_explicit_product_identity(previous):
            raise InstallerError(
                "已有 App.previous 不是可确认归属的回滚版本，未移动、未覆盖、未删除："
                + str(previous)
            )
        previous_errors = verifier(previous)
        if previous_errors:
            raise InstallerError(
                "已有 App.previous 不是完整、可确认归属的回滚版本，未移动、未覆盖、未删除："
                + "；".join(previous_errors[:5])
            )
        previous_displaced = parent / f".App-previous-old-{uuid.uuid4().hex}"
        rename(previous, previous_displaced)

    try:
        rename(target, displaced)
    except OSError:
        if previous is not None and previous_displaced is not None:
            try:
                rename(previous_displaced, previous)
            except OSError as restore_exc:
                raise InstallerError(f"无法移动当前 App，且回滚版本恢复失败：{restore_exc}")
        raise

    try:
        rename(staging, target)
    except OSError:
        _restore_after_failed_publish(
            target=target,
            displaced=displaced,
            previous=previous,
            previous_displaced=previous_displaced,
            rename=rename,
        )
        raise

    if existing_verified:
        assert previous is not None
        try:
            rename(displaced, previous)
        except OSError as exc:
            # The new App is valid, but the one-version rollback contract is
            # part of a successful upgrade. Restore the old App and previous.
            failed_new = parent / f".App-new-rejected-{uuid.uuid4().hex}"
            try:
                rename(target, failed_new)
                rename(displaced, target)
                if previous_displaced is not None:
                    rename(previous_displaced, previous)
                _remove_owned_tree(failed_new)
            except (OSError, InstallerError) as restore_exc:
                raise InstallerError(
                    f"新版已复制但回滚版本无法建立，自动恢复也失败：{restore_exc}"
                ) from exc
            raise InstallerError("新版已复制但无法建立回滚版本，已恢复原版本") from exc
        if previous_displaced is not None:
            _remove_owned_tree(previous_displaced)
    else:
        _remove_owned_tree(displaced)


def install_package(
    source_root: Path,
    target: Path,
    *,
    verifier: Callable[[Path], list[str]] = _verify_package_folder,
    free_bytes: Callable[[Path], int] | None = None,
    rename: Callable[[Path, Path], None] = os.rename,
) -> Path:
    try:
        source = source_root.resolve(strict=True)
    except OSError as exc:
        raise InstallerError(f"源安装包不存在：{source_root}") from exc
    if not source.is_dir() or _is_reparse_point(source):
        raise InstallerError("源安装包必须是普通文件夹，不能是符号链接或 Junction")

    target = Path(os.path.abspath(target))
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise InstallerError("源安装包与安装目标不能相同或互相嵌套")

    source_errors = verifier(source)
    if source_errors:
        raise InstallerError("源安装包完整性校验失败：" + "；".join(source_errors[:5]))
    existing_verified: bool | None = None
    if os.path.lexists(target):
        existing_verified, same_version = _inspect_existing_install(source, target, verifier)
        if same_version:
            return target

    parent = target.parent
    anchor = _existing_anchor(parent)
    _reject_reparse_ancestors(anchor)
    available = (free_bytes or (lambda path: shutil.disk_usage(path).free))(anchor)
    if available < MINIMUM_FREE_BYTES:
        raise InstallerError("安装目标所在磁盘可用空间不足 2GB")
    parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_ancestors(parent)
    if os.path.lexists(target) and existing_verified is None:
        existing_verified, same_version = _inspect_existing_install(source, target, verifier)
        if same_version:
            return target

    staging = Path(tempfile.mkdtemp(prefix=".App-staging-", dir=parent))
    published = False
    try:
        _copy_package_tree(source, staging)
        staging_errors = verifier(staging)
        if staging_errors:
            raise InstallerError("复制后的安装包完整性校验失败：" + "；".join(staging_errors[:5]))
        if os.path.lexists(target) and existing_verified is None:
            existing_verified, same_version = _inspect_existing_install(source, target, verifier)
            if same_version:
                return target
        # All paths are siblings on one volume. Fresh installs are one atomic
        # rename; upgrades keep App.previous and roll back on any publish error.
        _publish_staged_install(
            staging=staging,
            target=target,
            existing_verified=existing_verified,
            verifier=verifier,
            rename=rename,
        )
        published = True
        return target
    finally:
        if not published and os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装时宜 Agent 内容工厂")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="已经完整解压的便携包根目录",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        status = inspect_preferred_d_drive()
        selection = select_install_target(status)
        if selection.uses_preferred_drive:
            print("[安装位置] D 盘符合固定磁盘、NTFS、非重解析、可写和 2GB 空间要求。")
        else:
            print("[安装位置] 未采用 D 盘：" + "；".join(selection.preferred_drive_reasons))
            print("[安装位置] 已改用当前用户的本机应用目录。")
        print(f"[安装位置] {selection.target}")
        print(
            "[数据位置] App 只保存程序；任务、成片、设置和加密 Key 仍保存在 "
            "%LOCALAPPDATA%\\ShiyiContentFactory\\UserData。"
        )
        print("[校验] 正在校验源包、复制到同盘临时目录并复核完整性，请稍候……")
        installed = install_package(args.source_root, selection.target)
        print(f"[完成] 已安装、升级或修复到：{installed}")
        print("[说明] 安装程序没有启动软件、没有联网下载，也没有删除原始解压包。")
        print("[下一步] 请打开安装目录，双击“启动时宜Agent内容工厂.bat”。")
        return 0
    except (InstallerError, OSError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        print("[保护] 新包发布失败时会恢复原 App；未通过校验的临时副本不会发布。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
