import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.image_metadata import image_metadata


class ImageMetadataTests(unittest.TestCase):
    def test_build_outputs_supply_the_shared_publication_identity(self) -> None:
        for repository, source in (("CBusillo/Discord-Blue", "a" * 40), ("another-owner/another-product", "c" * 40)):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "outputs"
                output.write_text("existing=value\n")
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.image_metadata"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "GITHUB_REPOSITORY": repository, "GITHUB_SHA": source, "GITHUB_OUTPUT": str(output)},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                published = image_metadata(source, repository)
                self.assertEqual(values["existing"], "value")
                self.assertEqual(values["image"], published.image)
                self.assertEqual(values["tag_sha"], published.tag_sha)
                self.assertEqual(values["tag_main"], published.tag_main)
                self.assertEqual(values["cache"], published.cache)
                self.assertEqual(result.stdout, "")

    def test_non_commit_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            image_metadata("main", "cbusillo/discord-blue")
