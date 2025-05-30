import json
import logging
from pathlib import Path

import aiofiles
import discord
from typing import Any, Awaitable, Callable

from discord_blue.classes.training import TrainingMessage, TrainingConversation
from discord_blue.config import config

logger = logging.getLogger(__name__)


async def fetch_conversation(channel: discord.TextChannel, message: discord.Message, context_size: int) -> TrainingConversation:
    conversation = TrainingConversation(
        channel=channel.name,
        target=TrainingMessage(
            user_id=message.author.id,
            username=str(message.author.global_name),
            message=message.content,
            message_id=message.id,
            timestamp=message.created_at.timestamp(),
        ),
    )

    async for context_message in channel.history(before=message, limit=context_size, oldest_first=False):
        if not context_message.content:
            continue
        conversation.context.insert(
            0,
            TrainingMessage(
                user_id=context_message.author.id,
                username=str(context_message.author.global_name),
                message=context_message.content,
                message_id=context_message.id,
                timestamp=context_message.created_at.timestamp(),
            ),
        )

    if message.reference and message.reference.message_id:
        try:
            replied_message = await channel.fetch_message(message.reference.message_id)
            conversation.replied = TrainingMessage(
                user_id=replied_message.author.id,
                username=str(replied_message.author.global_name),
                message=replied_message.content,
                message_id=replied_message.id,
                timestamp=replied_message.created_at.timestamp(),
            )
        except discord.NotFound:
            logger.warning(f"Message {message.reference.message_id} not found")

    return conversation


async def save_conversation(conversation: TrainingConversation, output_file_path: Path) -> None:
    try:
        async with aiofiles.open(output_file_path, "a") as file:
            await file.write(conversation.model_dump_json() + "\n")
    except IOError as e:
        logger.error(f"Could not write to file {output_file_path}: {e}")


async def convert_jsonl_to_json(jsonl_file_path: Path) -> None:
    jsons = jsonl_file_path.read_text().strip().split("\n")

    json_file_path = jsonl_file_path.with_suffix(".json")
    if json_file_path.exists():
        existing_json = json.loads(json_file_path.read_text())
        jsons.extend(existing_json)

    json_file_path.write_text(json.dumps(jsons, indent=4))

    logger.info(f"Converted {jsonl_file_path} to {json_file_path}")


async def connect_to_discord_and_run(function_to_run: Callable[..., Awaitable[None]], *args: Any, **kwargs: Any) -> None:
    client = discord.Client(intents=discord.Intents.all())

    @client.event
    async def on_ready() -> None:
        logger.info(f"{client.user} has connected to Discord!")
        await function_to_run(client, *args, **kwargs)
        await client.close()

    try:
        await client.start(config.discord.token)
    except (KeyboardInterrupt, discord.errors.DiscordException):
        await client.close()


def clear_training_data(output_path: Path) -> None:
    for file in output_path.glob("*_training_data.json*"):
        file.unlink()

    config.llm_training.channels.clear()
    config.save()


async def get_training_data(
    client: discord.Client, username: str, context_size: int, output_path: Path, reset_training_data: bool = False
) -> None:
    logger.info(f"Getting training data for {username} with context size {context_size}")

    if reset_training_data:
        clear_training_data(output_path)

    if username != "all":
        member = client.guilds[0].get_member_named(username)
        if not member:
            raise ValueError(f"Could not find user {username}")
        user_id = member.id
    else:
        user_id = None
    number_of_conversations = 0
    number_of_channels = 0

    for channel in client.get_all_channels():
        number_of_channels += 1
        logger.info(f"Checking channel #{number_of_channels} {channel.name} with ID: {channel.id}")
        if not isinstance(channel, discord.TextChannel):
            continue
        history_kwargs: dict[str, Any] = {"limit": None, "oldest_first": True}
        saved_channel = config.llm_training.get_channel(channel.id)
        if saved_channel:
            last_message_id = saved_channel.last_message_id
            history_kwargs["after"] = discord.Object(id=last_message_id)

        try:
            async for message in channel.history(**history_kwargs):
                if not message.content:
                    continue
                if user_id and message.author.id != user_id:
                    continue

                number_of_conversations += 1
                logger.info(
                    f"Fetching conversation #{number_of_conversations} for message {message.id} at {message.created_at}\n{message.author.global_name}: {message.content}"
                )
                conversation = await fetch_conversation(channel, message, context_size)
                if username == "all":
                    user_file_path = output_path / f"{message.author.name.replace('_', '').replace('.','')}_training_data.jsonl"
                else:
                    user_file_path = output_path / f"{username}_training_data.jsonl"
                await save_conversation(conversation, user_file_path)
                config.llm_training.add_channel(channel.id, channel.name, message.id)
                config.save()

        except discord.errors.Forbidden:
            logger.warning(f"Could not access channel {channel.id}")

    for file in output_path.glob("*_training_data.jsonl"):
        await convert_jsonl_to_json(file)
