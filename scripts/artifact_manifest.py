"""Write the product-owned generic-web publication manifest."""

import json
import os
import re
from pathlib import Path

from scripts.image_metadata import image_metadata


TESTING_STATE_COPY_COMMAND = ("/app/.venv/bin/python", "-m", "discord_blue.testing_state_copy")


def write_manifest(*, source_commit: str, repository: str, image_reference: str, destination: Path) -> None:
    """Refuse missing publication evidence before writing a manifest."""
    image_repository, digest = publication_identity(source_commit, repository, image_reference)
    manifest = {
        "schema_version": 1,
        "kind": "generic-web",
        "source_commit": source_commit,
        "image": {"repository": image_repository, "digest": digest},
        "testing_state_copy": {"command": TESTING_STATE_COPY_COMMAND},
    }
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def publication_identity(source_commit: str, repository: str, image_reference: str) -> tuple[str, str]:
    """Validate the identity shared by publication and legacy delivery."""
    image_repository = image_metadata(source_commit, repository).image
    image, separator, digest = image_reference.partition("@")
    if not separator or image != image_repository or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("Publication requires the repository's immutable image digest")
    return image_repository, digest


if __name__ == "__main__":
    write_manifest(
        source_commit=os.environ["SOURCE_COMMIT"],
        repository=os.environ["GITHUB_REPOSITORY"],
        image_reference=os.environ["IMAGE_REFERENCE"],
        destination=Path("artifact-manifest.json"),
    )
