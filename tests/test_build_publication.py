import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.artifact_manifest import write_manifest
from scripts.build_publication import read_publication


class BuildPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commit = "a" * 40
        self.image = "ghcr.io/cbusillo/discord-blue@sha256:" + "b" * 64
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
        self.assertEqual(self.publication(), (self.image, f"ghcr.io/cbusillo/discord-blue:sha-{self.commit}"))
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
