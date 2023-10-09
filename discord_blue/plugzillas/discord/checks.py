import logging
import discord
from discord import app_commands
from discord_blue.config import config

logger = logging.getLogger(__name__)


def has_employee_role():  # type: ignore[arg-type]
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member):
            result = any(role.name == config.discord.employee_role_name for role in interaction.user.roles)
            return result
        return False

    return app_commands.check(predicate)
