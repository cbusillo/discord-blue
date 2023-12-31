import asyncio
import logging
import signal
from time import sleep

from discord.errors import PrivilegedIntentsRequired
from discord_blue.plugzillas.discord_plug import BlueBot
from discord_blue.config import config

DISCORD_TOKEN_TIMEOUT = 120
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def main() -> None:
    login_success = False
    for count in range(DISCORD_TOKEN_TIMEOUT):
        if login_success:
            break
        try:
            bot = BlueBot()
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda current_signal=sig: asyncio.create_task(bot.on_signal(current_signal)))
            loop.run_until_complete(bot.start(config.discord.token))
            login_success = True
        except PrivilegedIntentsRequired:
            logger.error("Privileged intents required")
            logger.error(
                "Please enable intents in the discord developer portal https://discord.com/api/oauth2/authorize?client_id=1160954317306613800&permissions=8&scope=bot"
            )
            sleep(5)


if __name__ == "__main__":
    main()
