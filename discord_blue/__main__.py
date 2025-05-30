import argparse
import asyncio
import logging
import signal
from argparse import Namespace
from pathlib import Path
from time import sleep

from nextcord.errors import PrivilegedIntentsRequired

from discord_blue.config import config
from discord_blue.plugs.discord_plug import BlueBot
from discord_blue.scripts.llm.model import generate_response
from discord_blue.scripts.llm.model_training import train_models
from discord_blue.scripts.llm.training_data import connect_to_discord_and_run, get_training_data

DISCORD_TOKEN_TIMEOUT = 120
logger = logging.getLogger(__name__)


def setup_logging(log_level: str) -> None:
    numeric_log_level = getattr(logging, log_level)
    if not isinstance(numeric_log_level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logging.basicConfig(level=numeric_log_level)


def parse_args() -> Namespace:
    def parse_path(path_string: str) -> Path:
        path = Path(path_string).expanduser().resolve()
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path

    parser = argparse.ArgumentParser(description="Discord bot")
    parser.add_argument("--get-training-data", action="store_true", help="Get training data")
    parser.add_argument("--username", type=str, help="Username or all")
    parser.add_argument("--context-size", type=int, default=5, help="Context size")
    parser.add_argument("--output-path", type=parse_path, help="Output file")
    parser.add_argument("--input-path", type=parse_path, help="Input file")
    parser.add_argument("--reset-training-data", action="store_true", help="Reset training data")
    parser.add_argument("--train-model", action="store_true", help="Train model")
    parser.add_argument("--model-name", type=str, help="Model name", default="meta-llama/Llama-3.2-1B")
    parser.add_argument("--generate-response", action="store_true", help="Generate response")
    parser.add_argument("--message", type=str, help="Message to generate response for")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Log level",
    )
    return parser.parse_args()


def start_bot() -> None:
    login_success = False
    for count in range(DISCORD_TOKEN_TIMEOUT):
        if login_success:
            break
        try:
            bot = BlueBot()
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig,
                    lambda current_signal=sig: asyncio.create_task(
                        bot.on_signal(current_signal)
                    ),
                )
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
    if args.get_training_data:
        if not args.username or not args.output_path:
            logger.error("Username or all is required along with output path for getting training data")
            return
        asyncio.run(
            connect_to_discord_and_run(
                get_training_data, args.username, args.context_size, args.output_path, args.reset_training_data
            )
        )
    elif args.train_model:
        if not args.username or not args.input_path:
            logger.error("Username or all along with input path is required for training model")
            return
        train_models(args.username, args.input_path, args.model_name)
    elif args.generate_response:
        if not args.username or not args.input_path or not args.message:
            logger.error("Username, input path, and message is required for generating response")
            return
        generate_response(args.username, args.input_path, args.message)
    else:
        start_bot()


if __name__ == "__main__":

    main()
