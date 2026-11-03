import re
from types import SimpleNamespace
from typing import Any

DECISION_HEADING = re.compile(r"^Director decision Q\d+: ")
question_records = SimpleNamespace(heading_id=lambda text: text)


def default_decision_authors() -> list[str]:
    return ["trusted-recorder"]


def fetch(item: str) -> list[dict[str, str]]:
    if item.endswith("#999"):
        raise ValueError("discussion unavailable")
    return [
        {
            "url": "https://github.com/cbusillo/example/issues/1#issuecomment-2",
            "body": "Director decision Q1218: Keep both.\n\nQuestion: source",
        }
    ]


def questions(comments: list[dict[str, str]], owner: str, authors: list[str]) -> list[dict[str, Any]]:
    if owner != "cbusillo" or authors != default_decision_authors():
        raise ValueError("reader identity was not supplied")
    return [{"question": "Q1218", "answers": [comments[0]["url"]]}, {"question": "Q1219", "answers": []}]
