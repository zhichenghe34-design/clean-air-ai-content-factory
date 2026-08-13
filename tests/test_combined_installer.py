from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.install_combined import (
    MINIMUM_FREE_BYTES,
    InstallerError,
    assess_preferred_drive,
    install_package,
    select_install_target,
)


class CombinedInstallerTests(unittest.TestCase):
    @staticmethod
    def _write_package(folder: Path, version: str, payload: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "PACKAGE-MANIFEST.json").write_bytes(
            (
                '{"schema_version":2,"product":"时宜 Agent 内容工厂",'
                '"package_kind":"windows_x64_combined_portable",'
                '"source":{"repository_commit":"' + version.ljust(40, "0")[:40] + '"}}\n'
            ).encode("utf-8")
        )
        (folder / "payload.txt").write_text(payload, encoding="utf-8")
        (folder / "启动时宜Agent内容工厂.bat").write_text("@echo off\r\n", encoding="ascii")
        (folder / "安装到D盘.bat").write_text("@echo off\r\n", encoding="ascii")
        verifier = folder / "tools" / "verify_combined_portable.pyc"
        verifier.parent.mkdir(parents=True, exist_ok=True)
        verifier.write_text("# package identity marker\n", encoding="utf-8")

    @classmethod
    def _write_legacy_schema2_package(cls, folder: Path, version: str, payload: str) -> None:
        cls._write_package(folder, version, payload)
        compiled = folder / "tools" / "verify_combined_portable.pyc"
        compiled.unlink()
        (folder / "tools" / "verify_combined_portable.py").write_text(
            "# formal schema-2 package identity marker\n",
            encoding="utf-8",
        )

    def test_eligible_d_drive_is_the_default_target(self) -> None:
        status = assess_preferred_drive(
            exists=True,
            fixed_local=True,
            filesystem="NTFS",
            reparse=False,
            writable=True,
            free_bytes=MINIMUM_FREE_BYTES,
        )
        selection = select_install_target(
            status,
            {"LOCALAPPDATA": str(Path("C:/Users/备用用户/AppData/Local"))},
        )
        self.assertTrue(selection.uses_preferred_drive)
        self.assertEqual(
            Path("D:" + os.sep) / "时宜Agent内容工厂" / "App",
            selection.target,
        )
        self.assertEqual((), selection.preferred_drive_reasons)

    def test_missing_d_drive_falls_back_to_current_user_local_app_data(self) -> None:
        status = assess_preferred_drive(exists=False)
        local_app_data = Path("C:/Users/测试 用户/AppData/Local")
        selection = select_install_target(status, {"LOCALAPPDATA": str(local_app_data)})
        self.assertFalse(selection.uses_preferred_drive)
        self.assertEqual(
            local_app_data / "Programs" / "时宜Agent内容工厂" / "App",
            selection.target,
        )
        self.assertIn("D 盘不存在", selection.preferred_drive_reasons)

    def test_unwritable_or_low_space_d_drive_is_rejected_with_a_reason(self) -> None:
        for writable, free_bytes, expected in (
            (False, MINIMUM_FREE_BYTES, "不能写入"),
            (True, MINIMUM_FREE_BYTES - 1, "不足 2GB"),
        ):
            with self.subTest(writable=writable, free_bytes=free_bytes):
                status = assess_preferred_drive(
                    exists=True,
                    fixed_local=True,
                    filesystem="ntfs",
                    reparse=False,
                    writable=writable,
                    free_bytes=free_bytes,
                )
                self.assertFalse(status.eligible)
                self.assertTrue(any(expected in reason for reason in status.reasons), status.reasons)

    def test_existing_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-existing-") as temporary:
            root = Path(temporary)
            source = root / "源包"
            source.mkdir()
            (source / "payload.txt").write_text("source", encoding="utf-8")
            target = root / "目标" / "App"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("existing-success", encoding="utf-8")

            with self.assertRaisesRegex(InstallerError, "已有安装无法确认版本"):
                install_package(
                    source,
                    target,
                    verifier=lambda _: [],
                    free_bytes=lambda _: MINIMUM_FREE_BYTES,
                )

            self.assertEqual("existing-success", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_same_verified_version_is_an_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-same-version-") as temporary:
            root = Path(temporary)
            source = root / "源包"
            target = root / "目标" / "App"
            source.mkdir()
            target.mkdir(parents=True)
            manifest = b'{"schema_version":2,"source":{"repository_commit":"a"}}\n'
            (source / "PACKAGE-MANIFEST.json").write_bytes(manifest)
            (target / "PACKAGE-MANIFEST.json").write_bytes(manifest)
            sentinel = target / "keep.txt"
            sentinel.write_text("same-version", encoding="utf-8")

            installed = install_package(
                source,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )

            self.assertEqual(target, installed)
            self.assertEqual("same-version", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_verified_old_version_is_atomically_upgraded_with_one_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-upgrade-") as temporary:
            root = Path(temporary)
            source = root / "新版源包"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "new payload")
            self._write_package(target, "old", "old payload")

            installed = install_package(
                source,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )

            self.assertEqual(target, installed)
            self.assertEqual("new payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual("old payload", (previous / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".App-*-*")))

    def test_second_upgrade_rotates_to_exactly_one_previous_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-rotate-") as temporary:
            root = Path(temporary)
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            source_two = root / "第二版"
            source_three = root / "第三版"
            self._write_package(target, "one", "version one")
            self._write_package(source_two, "two", "version two")
            self._write_package(source_three, "three", "version three")

            install_package(
                source_two,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )
            install_package(
                source_three,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )

            self.assertEqual("version three", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual("version two", (previous / "payload.txt").read_text(encoding="utf-8"))
            self.assertFalse((target.parent / "App.previous.previous").exists())
            self.assertEqual([], list(target.parent.glob(".App-*-*")))

    def test_verified_legacy_schema2_previous_is_safely_rotated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-schema2-previous-") as temporary:
            root = Path(temporary)
            source = root / "客户净包新版"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "new customer payload")
            self._write_package(target, "current", "current customer payload")
            self._write_legacy_schema2_package(previous, "legacy", "legacy schema2 payload")

            installed = install_package(
                source,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )

            self.assertEqual(target, installed)
            self.assertEqual("new customer payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual("current customer payload", (previous / "payload.txt").read_text(encoding="utf-8"))
            self.assertFalse((previous / "tools" / "verify_combined_portable.py").exists())
            self.assertEqual([], list(target.parent.glob(".App-*-*")))

    def test_damaged_ordinary_app_is_repaired_without_becoming_the_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-repair-") as temporary:
            root = Path(temporary)
            source = root / "完整源包"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "repaired payload")
            self._write_package(target, "damaged", "damaged payload")
            self._write_package(previous, "known-good", "known good rollback")

            def verifier(folder: Path) -> list[str]:
                if folder == target:
                    return ["PACKAGE-MANIFEST.json 与实际文件集合不一致"]
                return []

            installed = install_package(
                source,
                target,
                verifier=verifier,
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
            )

            self.assertEqual(target, installed)
            self.assertEqual("repaired payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual("known good rollback", (previous / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".App-*-*")))

    def test_unknown_existing_folder_with_missing_or_garbage_manifest_is_never_touched(self) -> None:
        for manifest in (None, b"not-json\n"):
            with self.subTest(manifest=manifest):
                with tempfile.TemporaryDirectory(prefix="shiyi-installer-foreign-") as temporary:
                    root = Path(temporary)
                    source = root / "完整源包"
                    target = root / "客户目录" / "App"
                    self._write_package(source, "new", "new payload")
                    target.mkdir(parents=True)
                    sentinel = target / "用户自己的重要文件.txt"
                    sentinel.write_bytes(b"must remain byte-for-byte")
                    if manifest is not None:
                        (target / "PACKAGE-MANIFEST.json").write_bytes(manifest)

                    def verifier(folder: Path) -> list[str]:
                        return ["invalid existing folder"] if folder == target else []

                    with self.assertRaisesRegex(InstallerError, "不是可确认归属"):
                        install_package(
                            source,
                            target,
                            verifier=verifier,
                            free_bytes=lambda _: MINIMUM_FREE_BYTES,
                        )

                    self.assertEqual(b"must remain byte-for-byte", sentinel.read_bytes())
                    if manifest is not None:
                        self.assertEqual(manifest, (target / "PACKAGE-MANIFEST.json").read_bytes())
                    self.assertEqual([], list(target.parent.glob(".App-staging-*")))
                    self.assertEqual([], list(target.parent.glob(".App-displaced-*")))

    def test_publish_failure_restores_current_app_and_previous_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-rollback-") as temporary:
            root = Path(temporary)
            source = root / "新版源包"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "new payload")
            self._write_package(target, "current", "current payload")
            self._write_package(previous, "previous", "previous payload")
            publish_attempted = False

            def fail_publish(staging: Path, destination: Path) -> None:
                nonlocal publish_attempted
                if staging.name.startswith(".App-staging-") and destination == target:
                    publish_attempted = True
                    raise OSError("simulated publish failure")
                os.rename(staging, destination)

            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                install_package(
                    source,
                    target,
                    verifier=lambda _: [],
                    free_bytes=lambda _: MINIMUM_FREE_BYTES,
                    rename=fail_publish,
                )

            self.assertTrue(publish_attempted)
            self.assertEqual("current payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual("previous payload", (previous / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".App-*-*")))
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_unknown_previous_folder_is_never_rotated_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-foreign-previous-") as temporary:
            root = Path(temporary)
            source = root / "新版源包"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "new payload")
            self._write_package(target, "old", "old payload")
            previous.mkdir(parents=True)
            sentinel = previous / "用户自己的重要文件.txt"
            sentinel.write_bytes(b"must remain byte-for-byte")

            def verifier(folder: Path) -> list[str]:
                return ["foreign previous"] if folder == previous else []

            with self.assertRaisesRegex(InstallerError, "App.previous.*未移动、未覆盖、未删除"):
                install_package(
                    source,
                    target,
                    verifier=verifier,
                    free_bytes=lambda _: MINIMUM_FREE_BYTES,
                )

            self.assertEqual("old payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual(b"must remain byte-for-byte", sentinel.read_bytes())
            self.assertEqual([], list(target.parent.glob(".App-*-*")))
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_forged_legacy_marker_previous_with_foreign_identity_is_never_touched(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-forged-schema2-previous-") as temporary:
            root = Path(temporary)
            source = root / "新版源包"
            target = root / "客户目录" / "App"
            previous = root / "客户目录" / "App.previous"
            self._write_package(source, "new", "new payload")
            self._write_package(target, "old", "old payload")
            previous.mkdir(parents=True)
            (previous / "PACKAGE-MANIFEST.json").write_text(
                '{"schema_version":2,"product":"其他产品",'
                '"package_kind":"windows_x64_combined_portable",'
                '"source":{"repository_commit":"' + ("f" * 40) + '"}}\n',
                encoding="utf-8",
            )
            (previous / "启动时宜Agent内容工厂.bat").write_text("@echo off\r\n", encoding="ascii")
            (previous / "安装到D盘.bat").write_text("@echo off\r\n", encoding="ascii")
            legacy_verifier = previous / "tools" / "verify_combined_portable.py"
            legacy_verifier.parent.mkdir(parents=True)
            legacy_verifier.write_text("# forged marker\n", encoding="utf-8")
            sentinel = previous / "用户自己的重要文件.txt"
            sentinel.write_bytes(b"must remain byte-for-byte")

            with self.assertRaisesRegex(InstallerError, "App.previous.*未移动、未覆盖、未删除"):
                install_package(
                    source,
                    target,
                    verifier=lambda _: [],
                    free_bytes=lambda _: MINIMUM_FREE_BYTES,
                )

            self.assertEqual("old payload", (target / "payload.txt").read_text(encoding="utf-8"))
            self.assertEqual(b"must remain byte-for-byte", sentinel.read_bytes())
            self.assertEqual([], list(target.parent.glob(".App-*-*")))
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_manifest_failure_does_not_publish_or_leave_staging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-installer-manifest-") as temporary:
            root = Path(temporary)
            source = root / "完整源包"
            source.mkdir()
            (source / "payload.txt").write_text("payload", encoding="utf-8")
            target = root / "客户目录" / "App"
            calls = 0

            def verifier(_: Path) -> list[str]:
                nonlocal calls
                calls += 1
                return [] if calls == 1 else ["PACKAGE-MANIFEST.json 与实际文件集合不一致"]

            with self.assertRaisesRegex(InstallerError, "复制后的安装包完整性校验失败"):
                install_package(
                    source,
                    target,
                    verifier=verifier,
                    free_bytes=lambda _: MINIMUM_FREE_BYTES,
                )

            self.assertEqual(2, calls)
            self.assertFalse(target.exists())
            self.assertTrue(source.is_dir())
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))

    def test_chinese_spaces_and_ampersand_path_installs_by_sibling_atomic_rename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="时宜 安装测试 & ") as temporary:
            root = Path(temporary)
            source = root / "完整 解压包 & 原件"
            nested = source / "资料 与 程序"
            nested.mkdir(parents=True)
            (nested / "中文 文件.txt").write_text("不可变程序内容", encoding="utf-8")
            target = root / "客户 甲 & 乙" / "时宜Agent内容工厂" / "App"
            rename_pairs: list[tuple[Path, Path]] = []

            def atomic_rename(staging: Path, destination: Path) -> None:
                self.assertEqual(staging.parent, destination.parent)
                self.assertTrue(staging.name.startswith(".App-staging-"))
                rename_pairs.append((staging, destination))
                os.rename(staging, destination)

            installed = install_package(
                source,
                target,
                verifier=lambda _: [],
                free_bytes=lambda _: MINIMUM_FREE_BYTES,
                rename=atomic_rename,
            )

            self.assertEqual(target, installed)
            self.assertEqual("不可变程序内容", (target / "资料 与 程序" / "中文 文件.txt").read_text(encoding="utf-8"))
            self.assertEqual(1, len(rename_pairs))
            self.assertTrue(source.is_dir(), "installer must not delete the extracted source package")
            self.assertEqual([], list(target.parent.glob(".App-staging-*")))


if __name__ == "__main__":
    unittest.main()
