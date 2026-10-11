"""Own the image names used by Build publication and its consumers."""

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageMetadata:
    image: str
    tag_sha: str
    tag_main: str
    cache: str


def image_metadata(source_commit: str, repository: str) -> ImageMetadata:
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("Publication requires an exact source commit")
    image = f"ghcr.io/{repository.lower()}"
    return ImageMetadata(image=image, tag_sha=f"{image}:sha-{source_commit}", tag_main=f"{image}:main", cache=f"{image}:buildcache")


if __name__ == "__main__":
    metadata = image_metadata(os.environ["GITHUB_SHA"], os.environ["GITHUB_REPOSITORY"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name in ("image", "tag_sha", "tag_main", "cache"):
            output.write(f"{name}={getattr(metadata, name)}\n")
