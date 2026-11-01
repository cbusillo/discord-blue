"""Wait for the canonical push Build and read its exact publication attempt."""

import io
import json
import os
import re
import subprocess
import time
import zipfile
from collections.abc import Callable
from urllib.parse import urlencode

from scripts.artifact_manifest import publication_identity
from scripts.image_metadata import image_metadata


def read_publication(
    *, repository: str, repository_id: int, source_commit: str, read: Callable[[str], bytes], timeout_seconds: float = 1800
) -> tuple[str, str]:
    """Only a successful same-repository main push supplies delivery outputs."""
    query = urlencode({"event": "push", "branch": "main", "head_sha": source_commit, "per_page": 100})
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = json.loads(read(f"repos/{repository}/actions/workflows/build.yml/runs?{query}"))
        runs = [
            run
            for run in payload["workflow_runs"]
            if run.get("path") == ".github/workflows/build.yml"
            and run.get("event") == "push"
            and run.get("head_branch") == "main"
            and run.get("head_sha") == source_commit
            and run.get("repository", {}).get("id") == repository_id
            and run.get("head_repository", {}).get("id") == repository_id
        ]
        run = max(runs, key=lambda candidate: candidate["id"], default=None)
        if run is not None and run.get("status") == "completed":
            if run.get("conclusion") != "success":
                raise ValueError("Canonical Build did not succeed")
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Canonical Build did not complete within the delivery budget")
        time.sleep(min(15, max(0, deadline - time.monotonic())))

    artifacts = json.loads(read(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100"))
    if artifacts["total_count"] > len(artifacts["artifacts"]):
        raise ValueError("Build artifact inventory is incomplete")
    matches = [artifact for artifact in artifacts["artifacts"] if artifact["name"] == f"artifact-manifest-{run['run_attempt']}"]
    if len(matches) != 1 or matches[0].get("expired", True):
        raise ValueError("Build has no unique current-attempt manifest")
    archive = read(f"repos/{repository}/actions/artifacts/{matches[0]['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        files = [entry for entry in bundle.infolist() if not entry.is_dir()]
        if len(files) != 1 or files[0].filename != "artifact-manifest.json":
            raise ValueError("Build artifact must contain one manifest")
        manifest = json.loads(bundle.read(files[0]))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "generic-web"
        or manifest.get("source_commit") != source_commit
        or not isinstance(manifest.get("image"), dict)
        or set(manifest["image"]) != {"repository", "digest"}
    ):
        raise ValueError("Build manifest does not bind the requested source")
    image_reference = f"{manifest['image']['repository']}@{manifest['image']['digest']}"
    publication_identity(source_commit, repository, image_reference)
    return image_reference, image_metadata(source_commit, repository).tag_sha


def github_read(path: str) -> bytes:
    """Use the runner's built-in gh client and scoped token without echoing it."""
    for attempt in range(3):
        try:
            result = subprocess.run(["gh", "api", path], capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            reason = "timeout"
            retryable = True
        else:
            if result.returncode == 0:
                return result.stdout
            match = re.search(rb"HTTP (\d{3})", result.stderr)
            status = int(match[1]) if match else None
            reason = f"HTTP {status}" if status else "gh request failure"
            retryable = status in {429, 500, 502, 503, 504} or b"secondary rate limit" in result.stderr.lower()
        if not retryable or attempt == 2:
            raise RuntimeError(f"GitHub publication read failed: {reason}")
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    published_image_reference, published_deploy_reference = read_publication(
        repository=os.environ["GITHUB_REPOSITORY"],
        repository_id=int(os.environ["GITHUB_REPOSITORY_ID"]),
        source_commit=os.environ["GITHUB_SHA"],
        read=github_read,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"image_reference={published_image_reference}\ndeploy_reference={published_deploy_reference}\n")
