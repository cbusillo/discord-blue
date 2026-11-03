from __future__ import annotations

import asyncio
import json
import logging
import re


logger = logging.getLogger(__name__)
READ_TIMEOUT_SECONDS = 30
SOURCE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9][0-9]*)(?:#issuecomment-[1-9][0-9]*)?"
)


async def read_item(command: tuple[str, ...], item: str) -> list[dict[str, object]]:
    process = await asyncio.create_subprocess_exec(
        *command,
        item,
        "--include-answers",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError(f"The catalog reader failed with exit {process.returncode}")
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("item") != item or not isinstance(result.get("questions"), list):
        raise ValueError("Invalid catalog reader result")
    return result["questions"]


async def read_resolutions(command: tuple[str, ...], sources: object, *, strict: bool = True) -> list[dict[str, str]]:
    if not command or not isinstance(sources, list):
        raise ValueError("Invalid question reader or sources")
    groups: dict[str, list[dict[str, str]]] = {}
    for source in sources:
        if not isinstance(source, dict) or not all(isinstance(value, str) for value in source.values()):
            raise ValueError("Invalid question source fields")
        match = SOURCE.fullmatch(source.get("url", ""))
        if match is None:
            raise ValueError("Invalid question source URL")
        groups.setdefault(f"{match[1]}#{match[2]}", []).append(source)
    resolved = []
    for item, group in groups.items():
        try:
            questions = await read_item(command, item)
            for source in group:
                matching = [
                    q
                    for q in questions
                    if isinstance(q, dict)
                    and q.get("number") == source.get("number")
                    and ("#issuecomment-" not in source["url"] or q.get("url") == source["url"])
                ]
                if not matching:
                    continue
                if len(matching) != 1:
                    raise ValueError("Question source is ambiguous")
                answers = matching[0].get("decision_comments")
                if not isinstance(answers, list):
                    raise ValueError("Catalog reader does not expose accepted answers")
                if answers:
                    answer = answers[-1]
                    if (
                        not isinstance(answer, dict)
                        or not isinstance(answer.get("body"), str)
                        or not isinstance(answer.get("url"), str)
                    ):
                        raise ValueError("Invalid accepted answer")
                    body = re.sub(r"<!--.*?-->", "", answer["body"], flags=re.DOTALL).strip()
                    resolved.append({**source, "answer": body, "decision_url": answer["url"]})
        except (OSError, ValueError, TimeoutError) as error:
            if strict:
                raise
            logger.warning("Question refresh unavailable (%s)", type(error).__name__)
    return resolved
