"""Write the product-owned generic-web publication manifest."""

import json
import os
import re
from pathlib import Path


def write_manifest(*, source_commit: str, repository: str, image_reference: str, destination: Path) -> None:
    """Refuse missing publication evidence before writing a manifest."""
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("Publication requires an exact source commit")
    image_repository = f"ghcr.io/{repository.lower()}"
    image, separator, digest = image_reference.partition("@")
    if not separator or image != image_repository or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("Publication requires the repository's immutable image digest")
    manifest = {
        "schema_version": 1,
        "kind": "generic-web",
        "source_commit": source_commit,
        "image": {"repository": image_repository, "digest": digest},
    }
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_manifest(
        source_commit=os.environ["SOURCE_COMMIT"],
        repository=os.environ["GITHUB_REPOSITORY"],
        image_reference=os.environ["IMAGE_REFERENCE"],
        destination=Path("artifact-manifest.json"),
    )
