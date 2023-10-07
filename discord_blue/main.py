import logging
from plugzillas.discord_plug import BlueBot
from config import Config

config = Config()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def main() -> None:
    bot = BlueBot()
    bot.run(config.discord.token)


if __name__ == '__main__':
    main()
