from __future__ import annotations

from typing import TYPE_CHECKING, cast

import discord

from discord_blue.doodads.agent_session.protocol import RemoteRequestUserInput
from discord_blue.plugs.discord_plug import BlueBot

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.interactions import RequestUserInputView


def _text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def questionnaire_fits_modal(request: RemoteRequestUserInput) -> bool:
    return (
        bool(request.questions)
        and sum(1 + bool(q.options and q.is_other) for q in request.questions) <= 5
        and all(
            not q.is_secret
            and _text_length(q.header or q.id) + (8 if q.options and q.is_other else 0) <= 45
            and _text_length(q.question) <= 100
            and (not q.options or 2 <= len(q.options) + q.is_other <= 10)
            and all(_text_length(o.label) <= 100 and _text_length(o.description) <= 100 for o in q.options)
            for q in request.questions
        )
    )


class AnswerTogetherModal(discord.ui.Modal):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(title="Answer together", timeout=600)
        self.parent_view = parent_view
        self.radios: dict[str, discord.ui.RadioGroup[AnswerTogetherModal]] = {}
        self.texts: dict[str, discord.ui.TextInput[AnswerTogetherModal]] = {}
        for question in parent_view.request.questions:
            header = question.header or question.id
            if question.options:
                options = [
                    discord.RadioGroupOption(label=o.label, value=str(index), description=o.description or None)
                    for index, o in enumerate(question.options)
                ]
                if question.is_other:
                    options.append(discord.RadioGroupOption(label="Other", value="other"))
                radio: discord.ui.RadioGroup[AnswerTogetherModal] = discord.ui.RadioGroup[AnswerTogetherModal](options=options)
                self.radios[question.id] = radio
                self.add_item(discord.ui.Label(text=header, description=question.question, component=radio))
            if not question.options or question.is_other:
                text: discord.ui.TextInput[AnswerTogetherModal] = discord.ui.TextInput[AnswerTogetherModal](
                    style=discord.TextStyle.paragraph,
                    required=not question.options,
                    max_length=1500,
                )
                self.texts[question.id] = text
                self.add_item(
                    discord.ui.Label(
                        text=f"{header} · Other" if question.options else header,
                        description="Fill only when choosing Other." if question.options else question.question,
                        component=text,
                    )
                )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.parent_view.interaction_check(interaction):
            return
        answers: dict[str, object] = {}
        for question in self.parent_view.request.questions:
            radio = self.radios.get(question.id)
            text = self.texts.get(question.id)
            value = radio.value if radio is not None else "other"
            if value == "other" and text is not None:
                answer = text.value.strip()
            elif value is not None and value.isdecimal() and int(value) < len(question.options):
                answer = question.options[int(value)].label
                if text is not None and text.value.strip():
                    await interaction.response.send_message(
                        "Choose Other to send your written answer. Use the picker to retry.", ephemeral=True
                    )
                    return
            else:
                answer = ""
            if not answer:
                await interaction.response.send_message(
                    "Please answer every question. Use Answer together or the picker to retry.", ephemeral=True
                )
                return
            answers[question.id] = {"answers": [answer]}
        request = self.parent_view.request
        await self.parent_view.interactions.handle_request_user_input_interaction(
            cast(discord.Interaction[BlueBot], interaction),
            self.parent_view.session_id,
            request.call_id,
            request.turn_id,
            {"answers": answers},
            session_epoch=request.session_epoch,
            message_id=self.parent_view.message_id,
        )


class AnswerTogetherButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(label="Answer together", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if await self.parent_view.interaction_check(interaction):
            await interaction.response.send_modal(AnswerTogetherModal(self.parent_view))
