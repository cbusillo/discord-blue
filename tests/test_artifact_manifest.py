import json
import tempfile
import unittest
from pathlib import Path

from scripts.artifact_manifest import write_manifest
from scripts.image_metadata import image_metadata


class ArtifactManifestTests(unittest.TestCase):
    def test_published_digest_is_bound_to_source_and_repository(self) -> None:
        commit = "a" * 40
        digest = "sha256:" + "b" * 64
        published = image_metadata(commit, "CBusillo/Discord-Blue")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "manifest.json"
            write_manifest(
                source_commit=commit,
                repository="CBusillo/Discord-Blue",
                image_reference=f"{published.image}@{digest}",
                destination=destination,
            )
            payload = json.loads(destination.read_text())
        self.assertEqual(payload["source_commit"], commit)
        self.assertEqual(payload["image"]["repository"], published.image)
        self.assertEqual(payload["image"]["digest"], digest)
        self.assertEqual(set(payload), {"schema_version", "kind", "source_commit", "image"})

    def test_failed_or_unbound_publication_writes_no_manifest(self) -> None:
        commit = "a" * 40
        digest = "sha256:" + "b" * 64
        for source, image in (
            (commit, ""),
            (commit, "ghcr.io/cbusillo/discord-blue:main"),
            (commit, "ghcr.io/cbusillo/discord-blue@sha256:short"),
            (commit, f"ghcr.io/cbusillo/other@{digest}"),
            ("main", f"ghcr.io/cbusillo/discord-blue@{digest}"),
        ):
            with self.subTest(source=source, image=image), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "manifest.json"
                with self.assertRaises(ValueError):
                    write_manifest(
                        source_commit=source,
                        repository="cbusillo/discord-blue",
                        image_reference=image,
                        destination=destination,
                    )
                self.assertFalse(destination.exists())
