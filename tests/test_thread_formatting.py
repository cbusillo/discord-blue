from __future__ import annotations

import time
import unittest

from discord_blue.doodads.agent_session.formatting import LEGACY_ASSISTANT_LABEL
from discord_blue.doodads.agent_session.formatting import USER_MESSAGE_PREFIX
from discord_blue.doodads.agent_session.formatting import WAITING_FOR_DIRECTION
from discord_blue.doodads.agent_session.formatting import convert_markdown_tables
from discord_blue.doodads.agent_session.formatting import format_user_message
from discord_blue.doodads.agent_session.formatting import is_assistant_message
from discord_blue.doodads.agent_session.formatting import mark_assistant_message


class AssistantMarkerTests(unittest.TestCase):
    def test_marked_messages_are_recognised_without_any_visible_label(self) -> None:
        message = mark_assistant_message("Done.")

        self.assertTrue(is_assistant_message(message))
        self.assertEqual("".join(char for char in message if char.isprintable()), "Done.")

    def test_messages_posted_with_the_old_label_are_still_recognised(self) -> None:
        self.assertTrue(is_assistant_message(f"{LEGACY_ASSISTANT_LABEL}\nLast useful answer"))

    def test_other_bot_messages_are_not_assistant_messages(self) -> None:
        for content in (
            format_user_message("Assistant said **Assistant**"),
            WAITING_FOR_DIRECTION,
            "​",
            "Plain notice",
        ):
            with self.subTest(content=content):
                self.assertFalse(is_assistant_message(content))


class UserMessageTests(unittest.TestCase):
    def test_multi_line_prompt_stays_in_one_quote_block(self) -> None:
        formatted = format_user_message("  First line\nSecond line\n\nThird  ")

        first_line, *rest = formatted.split("\n")
        self.assertTrue(first_line.startswith(">>> "))
        self.assertIn(USER_MESSAGE_PREFIX, first_line)
        self.assertTrue(first_line.endswith("First line"))
        self.assertEqual(rest, ["Second line", "", "Third"])


class MarkdownTableTests(unittest.TestCase):
    def test_table_rows_become_bullets_with_header_value_pairs(self) -> None:
        text = "\n".join(
            [
                "Findings:",
                "",
                "| Field | Verdict | Action |",
                "| :--- | --- | ---: |",
                "| **`homepage_url`** | **Mistake, and already duplicated.** | Drop it |",
                "| `tagline` | Fine | |",
                "",
                "After the table.",
            ]
        )

        self.assertEqual(
            convert_markdown_tables(text),
            "\n".join(
                [
                    "Findings:",
                    "",
                    "- **`homepage_url`** — Verdict: **Mistake, and already duplicated.** · Action: Drop it",
                    "- **`tagline`** — Verdict: Fine",
                    "",
                    "After the table.",
                ]
            ),
        )

    def test_two_column_table_uses_a_compact_form(self) -> None:
        text = "Name | Value\n--- | ---\nalpha | one \\| two\nbeta | 2"

        self.assertEqual(convert_markdown_tables(text), "- **alpha**: one | two\n- **beta**: 2")

    def test_tables_inside_code_fences_are_untouched(self) -> None:
        fenced = "\n".join(
            [
                "```markdown",
                "| a | b |",
                "| - | - |",
                "| 1 | 2 |",
                "```",
                "~~~~",
                "| c | d |",
                "|---|---|",
                "~~~~",
            ]
        )
        text = f"{fenced}\n\n| x | y |\n|---|---|\n| 3 | 4 |"

        self.assertEqual(convert_markdown_tables(text), f"{fenced}\n\n- **3**: 4")

    def test_other_markdown_is_unchanged(self) -> None:
        text = "\n".join(
            [
                "# Heading",
                "Some text with a | pipe that is not a table.",
                "Title",
                "---",
                "- a list | item",
                "> quote",
                "`inline | code`",
                "    | indented | code |",
                "    | --- | --- |",
                "| a | b | c |",
                "| --- | --- |",
            ]
        )

        self.assertEqual(convert_markdown_tables(text), text)

    def test_large_input_converts_in_linear_time(self) -> None:
        small = self.convert_duration(1_000)
        large = self.convert_duration(20_000)

        self.assertLess(large, 2.0)
        self.assertLess(large, max(small, 0.001) * 20 * 5)

    def test_adversarial_lines_convert_quickly(self) -> None:
        for text in ("|" * 200_000, "|" + " " * 200_000 + "|\n|" + "-" * 200_000 + "|", "\\" * 200_000 + "|\n| - |"):
            with self.subTest(length=len(text)):
                started = time.perf_counter()
                convert_markdown_tables(text)
                self.assertLess(time.perf_counter() - started, 2.0)

    def convert_duration(self, rows: int) -> float:
        table = ["| Name | Kind | Note |", "|---|---|---|"]
        table.extend(f"| row{index} | kind | {'x' * 40} \\| y |" for index in range(rows))
        text = "\n".join(["```", "| fenced | table |", "```", *table, "", "Done."])
        started = time.perf_counter()
        converted = convert_markdown_tables(text)
        duration = time.perf_counter() - started
        self.assertEqual(converted.count("\n- **row"), rows)
        return duration


if __name__ == "__main__":
    unittest.main()
