from __future__ import annotations

import asyncio
import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any

READ_TIMEOUT_SECONDS = 30


def catalog_resolutions(reader: Path, sources: list[dict[str, str]]) -> list[dict[str, str]]:
    reader = reader.resolve()
    sys.path[:0] = [str(reader.parent), str(reader.parents[2] / "github/scripts")]
    oq = runpy.run_path(str(reader))
    discussions = {}
    resolved = []
    for source in sources:
        match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)(?:#issuecomment-\d+)?", source["url"])
        if match is None:
            raise ValueError("Invalid question source")
        owner, repo, number = match.groups()
        item = f"{owner}/{repo}#{number}"
        if item not in discussions:
            comments = oq["fetch"](item)
            questions = oq["questions"](comments, owner, oq["default_decision_authors"]())
            discussions[item] = (comments, questions)
        comments, questions = discussions[item]
        matching = [q for q in questions if oq["question_records"].heading_id(q["question"]) == source["number"]]
        if len(matching) != 1:
            raise ValueError("Question source is missing or ambiguous")
        question = matching[0]
        if not question["answers"]:
            continue
        answer_url = question["answers"][-1]
        answer = next(comment["body"] for comment in comments if comment["url"] == answer_url)
        answer = answer.split("\n\n", 1)[0].strip()
        answer = oq["DECISION_HEADING"].sub("", answer.lstrip("*"), count=1)
        answer = re.sub(r"^Q[1-9][0-9]*[\s:*,.=—-]*", "", answer).strip("* ")
        if answer:
            resolved.append({**source, "answer": answer, "decision_url": answer_url})
    return resolved


async def read_resolutions(reader: Path, sources: object) -> list[dict[str, str]]:
    if not isinstance(sources, list):
        raise ValueError("Invalid question sources")
    if not all(isinstance(source, dict) and all(isinstance(value, str) for value in source.values()) for source in sources):
        raise ValueError("Invalid question source fields")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "discord_blue.question_resolutions",
        str(reader),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            output, _ = await process.communicate(json.dumps(sources).encode())
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError("The catalog question reader could not verify the discussion")
    result: Any = json.loads(output)
    if not isinstance(result, list) or not all(
        isinstance(row, dict) and all(isinstance(value, str) for value in row.values()) for row in result
    ):
        raise ValueError("Invalid question reader response")
    return result


if __name__ == "__main__":
    print(json.dumps(catalog_resolutions(Path(sys.argv[1]), json.load(sys.stdin))))
