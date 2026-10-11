import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.artifact_manifest import write_manifest
from scripts.build_publication import github_read, read_publication
from scripts.image_metadata import image_metadata


class BuildPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commit = "a" * 40
        self.published = image_metadata(self.commit, "CBusillo/Discord-Blue")
        self.image = self.published.image + "@sha256:" + "b" * 64
        self.build_run = {
            "id": 7,
            "run_attempt": 2,
            "path": ".github/workflows/build.yml",
            "event": "push",
            "head_branch": "main",
            "head_sha": self.commit,
            "repository": {"id": 101},
            "head_repository": {"id": 101},
            "status": "completed",
            "conclusion": "success",
        }
        self.artifact = {"id": 11, "name": "artifact-manifest-2", "expired": False}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact-manifest.json"
            write_manifest(
                source_commit=self.commit, repository="cbusillo/discord-blue", image_reference=self.image, destination=destination
            )
            self.manifest = json.loads(destination.read_text())
        self.reads: list[str] = []

    def read(self, path: str) -> bytes:
        self.reads.append(path)
        if "/workflows/" in path:
            return json.dumps({"workflow_runs": [self.build_run]}).encode()
        if "/runs/" in path:
            return json.dumps({"total_count": 1, "artifacts": [self.artifact]}).encode()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("artifact-manifest.json", json.dumps(self.manifest))
        return archive.getvalue()

    def publication(self) -> tuple[str, str]:
        return read_publication(
            repository="cbusillo/discord-blue", repository_id=101, source_commit=self.commit, read=self.read, timeout_seconds=0
        )

    def test_reads_exact_successful_run_and_attempt(self) -> None:
        self.assertEqual(self.publication(), (self.image, self.published.tag_sha))
        self.assertIn("/runs/7/artifacts", self.reads[1])
        self.assertTrue(self.reads[2].endswith("/artifacts/11/zip"))

    def test_failed_build_never_supplies_delivery(self) -> None:
        self.build_run["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            self.publication()
        self.assertEqual(len(self.reads), 1)

    def test_untrusted_or_wrong_source_runs_never_supply_delivery(self) -> None:
        for field, value in (
            ("event", "pull_request"),
            ("head_branch", "feature"),
            ("head_sha", "c" * 40),
            ("path", ".github/workflows/main.yml"),
            ("repository", {"id": 999}),
            ("head_repository", {"id": 999}),
        ):
            with self.subTest(field=field), patch.dict(self.build_run, {field: value}), self.assertRaises(TimeoutError):
                self.publication()

    def test_old_attempt_or_expired_manifest_is_refused(self) -> None:
        for mutation in ({"name": "artifact-manifest-1"}, {"expired": True}):
            with self.subTest(mutation=mutation), patch.dict(self.artifact, mutation), self.assertRaises(ValueError):
                self.publication()

    def test_source_or_image_mismatch_is_refused(self) -> None:
        for mutation in (
            {"source_commit": "c" * 40},
            {"image": {"repository": "ghcr.io/cbusillo/other", "digest": "sha256:" + "b" * 64}},
        ):
            with self.subTest(mutation=mutation), patch.dict(self.manifest, mutation), self.assertRaises(ValueError):
                self.publication()

    def test_waits_for_build_without_reading_artifacts_early(self) -> None:
        self.build_run["status"] = "in_progress"

        def finish_build(_seconds: float) -> None:
            self.build_run["status"] = "completed"

        with patch("scripts.build_publication.time.sleep", side_effect=finish_build):
            result = read_publication(
                repository="cbusillo/discord-blue", repository_id=101, source_commit=self.commit, read=self.read
            )
        self.assertEqual(result[0], self.image)
        self.assertEqual(len([path for path in self.reads if "/workflows/" in path]), 2)

    def test_transient_api_failure_retries_without_exposing_provider_output(self) -> None:
        failed = subprocess.CompletedProcess([], 1, b"", b"gh: temporary failure (HTTP 502)")
        succeeded = subprocess.CompletedProcess([], 0, b"publication", b"")
        with (
            patch("scripts.build_publication.subprocess.run", side_effect=[failed, succeeded]) as request,
            patch("scripts.build_publication.time.sleep"),
        ):
            self.assertEqual(github_read("repos/example/site"), b"publication")
        self.assertEqual(request.call_count, 2)

    def test_auth_refusal_is_not_retried_and_only_status_is_reported(self) -> None:
        failed = subprocess.CompletedProcess([], 1, b"", b"gh: private response body (HTTP 403)")
        with patch("scripts.build_publication.subprocess.run", return_value=failed) as request:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403$"):
                github_read("repos/example/site")
        self.assertEqual(request.call_count, 1)
