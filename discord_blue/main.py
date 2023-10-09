import logging
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.config import config

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def main() -> None:
    bot = BlueBot()
    bot.run(config.discord.token)


if __name__ == "__main__":
    main()
