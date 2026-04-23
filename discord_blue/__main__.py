import argparse
import asyncio
import logging
import signal
from argparse import Namespace
from collections.abc import Callable
from time import sleep

from discord.errors import PrivilegedIntentsRequired

from discord_blue.config import config
from discord_blue.plugs.discord_plug import BlueBot

DISCORD_TOKEN_TIMEOUT = 120
logger = logging.getLogger(__name__)
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def track_background_task(task: asyncio.Task[None]) -> None:
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


def create_signal_handler(bot: BlueBot) -> Callable[[signal.Signals], None]:
    def handler(current_signal: signal.Signals) -> None:
        loop = asyncio.get_running_loop()
        track_background_task(loop.create_task(bot.on_signal(current_signal)))

    return handler


def setup_logging(log_level: str) -> None:
    numeric_log_level = getattr(logging, log_level)
    if not isinstance(numeric_log_level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logging.basicConfig(level=numeric_log_level)


# noinspection Annotator
def parse_args() -> Namespace:
    parser = argparse.ArgumentParser(description="Discord bot")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Log level",
    )
    return parser.parse_args()


def start_bot() -> None:
    login_success = False
    for _count in range(DISCORD_TOKEN_TIMEOUT):
        if login_success:
            break
        try:
            bot = BlueBot()
            loop = asyncio.get_event_loop()
            handler = create_signal_handler(bot)
            for signal_to_add in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signal_to_add, handler, signal_to_add)
            loop.run_until_complete(bot.start(config.discord.token))
            login_success = True
        except PrivilegedIntentsRequired:
            logger.error("Privileged intents required")
            logger.error(
                "Please enable intents in the discord developer portal https://discord.com/api/oauth2/authorize?client_id=1160954317306613800&permissions=8&scope=bot"
            )
            sleep(5)


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    start_bot()


if __name__ == "__main__":
    main()
