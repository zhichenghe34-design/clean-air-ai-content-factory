from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import app
from core.orchestrator import ConflictError, JobStore, WorkflowError, local_fallback_plan
from tools import build_public_evidence as evidence_builder
from tools.verify_public_evidence import verify_archive


class PublicEvidenceExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JobStore(self.root)
        self.old_runtime = app.RUNTIME_DIR
        self.old_store = app.job_store
        app.RUNTIME_DIR = self.root
        app.job_store = self.store
        with app.public_evidence_export_locks_guard:
            app.public_evidence_export_locks.clear()
        self.ffprobe = self.root / "ffprobe.exe"
        self.ffprobe.write_bytes(b"fixture")
        self.environment = mock.patch.dict(os.environ, {"FFPROBE_PATH": str(self.ffprobe)}, clear=False)
        self.environment.start()
        self.job_id, self.run_id, self.source_manifest = self._publish_current_success()

    def tearDown(self) -> None:
        self.environment.stop()
        app.RUNTIME_DIR = self.old_runtime
        app.job_store = self.old_store
        with app.public_evidence_export_locks_guard:
            app.public_evidence_export_locks.clear()
        self.temporary.cleanup()

    def _publish_current_success(self) -> tuple[str, str, dict[str, object]]:
        job = self.store.create(local_fallback_plan("生成一条公开证据测试视频", []))
        raw, folder = self.store._load_v2(job["id"])
        run_id = "20260810-120000-abcdef12"
        source_manifest: dict[str, object] = {
            "schema_version": 2,
            "job_id": job["id"],
            "run_id": run_id,
            "stage": "render",
            "status": "complete",
            "finished_at": "2026-08-10T12:01:00+08:00",
            "artifacts": [],
        }
        source = folder / "runs" / run_id / "artifacts"
        source.mkdir(parents=True)
        manifest_path = source / "manifest.json"
        manifest_path.write_text(json.dumps(source_manifest, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        raw["runs"] = [{
            "run_id": run_id,
            "stage": "render",
            "status": "complete",
            "manifest_sha256": manifest_sha256,
        }]
        raw["current_run_id"] = run_id
        raw["status"] = "complete"
        self.store._write(folder / "job.json", raw)
        return str(job["id"]), run_id, source_manifest

    def test_source_resolver_accepts_only_current_complete_run_and_exact_manifest(self) -> None:
        resolved = self.store.resolve_public_evidence_source(self.job_id)
        self.assertEqual(resolved["run_id"], self.run_id)
        self.assertEqual(resolved["source"].name, "artifacts")

        raw, folder = self.store._load_v2(self.job_id)
        raw["runs"].append({"run_id": "failed-run", "stage": "render", "status": "failed"})
        raw["current_run_id"] = "failed-run"
        self.store._write(folder / "job.json", raw)
        with self.assertRaises(FileNotFoundError):
            self.store.resolve_public_evidence_source(self.job_id)

        raw["current_run_id"] = "../outside"
        self.store._write(folder / "job.json", raw)
        with self.assertRaises(FileNotFoundError):
            self.store.resolve_public_evidence_source(self.job_id)

        raw["current_run_id"] = self.run_id
        self.store._write(folder / "job.json", raw)
        manifest_path = folder / "runs" / self.run_id / "artifacts" / "manifest.json"
        manifest_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.store.resolve_public_evidence_source(self.job_id)

    def test_export_is_deterministic_atomic_replay_safe_and_not_artifact_traversal(self) -> None:
        source_sha256 = self.store.resolve_public_evidence_source(self.job_id)["source_manifest_sha256"]
        public_manifest = {
            **self.source_manifest,
            "public_package": True,
            "source_manifest_sha256": source_sha256,
        }
        manifest_bytes = (json.dumps(public_manifest, ensure_ascii=False) + "\n").encode("utf-8")
        public_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        build_calls: list[Path] = []

        def fake_build(_source, output, archive, *, ffprobe_path):
            self.assertEqual(Path(ffprobe_path), self.ffprobe.resolve())
            Path(output).mkdir(parents=True)
            Path(output, "manifest.json").write_bytes(manifest_bytes)
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.json", manifest_bytes)
            build_calls.append(Path(archive))
            archive_sha256 = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
            return {
                "public_manifest_sha256": public_manifest_sha256,
                "archive_sha256": archive_sha256,
                "archive_size": Path(archive).stat().st_size,
            }

        verified = ([], {"evidence_contract": "motion_v0.3"}, public_manifest)
        with mock.patch.object(app, "build_public_evidence", side_effect=fake_build), mock.patch.object(
            app, "verify_archive", return_value=verified
        ):
            first = app.prepare_public_evidence_export(self.job_id)
            second = app.prepare_public_evidence_export(self.job_id)
            Path(first["path"]).write_bytes(b"corrupt-cache")
            rebuilt = app.prepare_public_evidence_export(self.job_id)

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertFalse(rebuilt["replayed"])
        self.assertEqual(len(build_calls), 2)
        self.assertEqual(first["filename"], rebuilt["filename"])
        self.assertRegex(str(first["filename"]), r"^shiyi-public-evidence-[A-Za-z0-9-]+\.zip$")
        archive_path = Path(rebuilt["path"])
        self.assertTrue(archive_path.is_relative_to(self.root / "exports"))
        metadata_path = archive_path.with_name(archive_path.name + ".json")
        metadata_text = metadata_path.read_text(encoding="ascii")
        self.assertNotIn(str(self.root), metadata_text)
        self.assertNotIn("FFPROBE_PATH", metadata_text)
        self.assertNotIn("path", json.loads(metadata_text))
        with self.assertRaises(WorkflowError):
            self.store.resolve_artifact(self.job_id, archive_path.name)

    def test_ui_exposes_only_the_server_owned_current_export_route(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("下载公开证据包", script)
        self.assertIn("/public-evidence.zip", script)
        self.assertNotIn("public-evidence.zip?", script)

    def test_packaged_export_requires_the_exact_bundled_ffprobe(self) -> None:
        package_root = self.root / "package"
        bundled = package_root / "runtime" / "ffmpeg" / "ffprobe.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"bundled")
        (package_root / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(app, "APP_DIR", package_root):
            with self.assertRaises(ConflictError):
                app._configured_public_evidence_ffprobe()
            with mock.patch.dict(os.environ, {"FFPROBE_PATH": str(bundled)}, clear=False):
                self.assertEqual(app._configured_public_evidence_ffprobe(), bundled.resolve())

    def test_archive_verifier_rejects_nested_or_unrecognized_entries_before_extracting(self) -> None:
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../manifest.json", b"{}")
        errors, media, manifest = verify_archive(archive, ffprobe_path=self.ffprobe)
        self.assertTrue(any("不安全路径" in error for error in errors), errors)
        self.assertEqual(media, {})
        self.assertEqual(manifest, {})

    def test_reusable_builder_is_byte_deterministic_for_one_immutable_run(self) -> None:
        source = self.root / "builder-source"
        source.mkdir()
        source_manifest = {
            "schema_version": 2,
            "job_id": "job-deterministic",
            "run_id": "run-deterministic",
            "stage": "render",
            "status": "complete",
            "finished_at": "2026-08-10T12:34:56+08:00",
            "budget": {"attempted": 0, "limit": 7},
            "artifacts": [],
        }
        (source / "manifest.json").write_text(
            json.dumps(source_manifest, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        json_payloads = {
            "research.json": {},
            "insight.json": {},
            "script_variants.json": {},
            "approved_script.json": {"script": "测试脚本"},
            "review.json": {},
            "motion_plan.json": {},
            "run_report.json": {},
            "approvals.json": {"research": {}, "compliance": {}},
        }
        for name, payload in json_payloads.items():
            (source / name).write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        (source / "voice.wav").write_bytes(b"voice")
        (source / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n测试脚本\n", encoding="utf-8")
        (source / "final.mp4").write_bytes(b"video")
        media = {
            "duration_seconds": 52.0,
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
        }
        patches = (
            mock.patch.object(evidence_builder, "_engine_contract", return_value=("legacy_v2", [])),
            mock.patch.object(evidence_builder, "approval_validation_line", return_value=("审批身份有效", [])),
            mock.patch.object(evidence_builder, "script_edit_validation_line", return_value=("", [])),
            mock.patch.object(evidence_builder, "scan_text", return_value=[]),
            mock.patch.object(evidence_builder, "probe_video", return_value=media),
            mock.patch.object(evidence_builder, "validate_srt", return_value=[]),
            mock.patch.object(evidence_builder, "verify", return_value=([], media)),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        first = evidence_builder.build_public_evidence(
            source, self.root / "public-one", self.root / "public-one.zip", ffprobe_path=self.ffprobe
        )
        second = evidence_builder.build_public_evidence(
            source, self.root / "public-two", self.root / "public-two.zip", ffprobe_path=self.ffprobe
        )
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        self.assertEqual(
            (self.root / "public-one.zip").read_bytes(),
            (self.root / "public-two.zip").read_bytes(),
        )
        manifest = json.loads((self.root / "public-one" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["packaged_at"], source_manifest["finished_at"])


if __name__ == "__main__":
    unittest.main()
