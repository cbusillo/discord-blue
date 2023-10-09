import logging
from time import sleep

from discord.errors import PrivilegedIntentsRequired
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.config import config

DISCORD_TOKEN_TIMEOUT = 120
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def main() -> None:
    for count in range(DISCORD_TOKEN_TIMEOUT):
        try:
            bot = BlueBot()
            bot.run(config.discord.token)
        except PrivilegedIntentsRequired:
            logger.error("Privileged intents required")
            logger.error(
                "Please enable intents in the discord developer portal https://discord.com/api/oauth2/authorize?client_id=1160954317306613800&permissions=8&scope=bot"
            )
            sleep(5)


if __name__ == "__main__":
    main()
