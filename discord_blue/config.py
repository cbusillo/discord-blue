import logging
from pathlib import Path
from typing import Any

import toml

logger = logging.getLogger(__name__)


class Serializable:
    def to_dict(self) -> dict[str, Any] | Any:
        result = {}
        all_keys = Config.get_all_keys(self)
        for key in all_keys:
            if not key.startswith("_"):
                value = getattr(self, key, None)
                if isinstance(value, Serializable):
                    result[key] = value.to_dict()
                elif isinstance(value, dict):
                    result[key] = {k: v.to_dict() if isinstance(v, Serializable) else v for k, v in value.items()}
                else:
                    result[key] = value
        return result

    def from_dict(self, data: dict[str, Any]) -> None:
        for key, type_hint in getattr(self, "__annotations__", {}).items():
            value = data.get(key, getattr(self, key, None))

            try:
                existing_attr = getattr(self, key)
            except AttributeError:
                logger.warning(f"{key} not in {self.__class__.__name__}. Skipping...")
                continue

            if isinstance(existing_attr, Serializable):
                if not isinstance(value, dict):
                    logger.warning(f"Expected dict for {key} in {self.__class__.__name__}, got {type(value)}. Skipping...")
                    continue
                existing_attr.from_dict(value)
            else:
                setattr(self, key, value)

        self.validate()

    def validate(self) -> None:
        for key, value in getattr(self, "__annotations__", {}).items():
            if getattr(self, key, None) is None:
                logger.warning(f"Warning: Configuration value '{key}' is missing or None in {self.__class__.__name__}")


class ChannelConfig(Serializable):
    def __init__(self, name: str, last_message_id: int = 0) -> None:
        self.name = name
        self.last_message_id = last_message_id


class LLMTrainingConfig(Serializable):
    channels: dict[str, ChannelConfig] = {}

    def add_channel(self, channel_id: int, name: str, last_message_id: int) -> None:
        self.channels[str(channel_id)] = ChannelConfig(name, last_message_id)

    def get_channel(self, channel_id: int) -> ChannelConfig | None:
        return self.channels.get(str(channel_id))


class DiscordConfig(Serializable):
    token: str = "from_terminal"
    guild_id: int = 0
    bot_channel_id: int = 0
    employee_role_name: str = ""
    loaded_doodads: list[str] = []


class HuggingFaceConfig(Serializable):
    token: str = "from_terminal"


class Config(Serializable):
    _instance = None

    debug: bool = False

    def __init__(self) -> None:
        self._filepath = Path.home() / ".config" / Path(__file__).parent.stem.replace("_", "-") / "config.toml"
        if not self.filepath.exists():
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self.filepath.touch()

        self.discord = DiscordConfig()
        self.llm_training = LLMTrainingConfig()
        self.hugging_face = HuggingFaceConfig()

        self.load()

    @classmethod
    def get_instance(cls) -> "Config":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def filepath(self) -> Path:
        return self._filepath

    def load(self) -> None:
        try:
            with self.filepath.open() as file:
                data = toml.load(file)
                for key, value in data.items():
                    if key.startswith("_"):
                        continue
                    attr = getattr(self, key, None)
                    if isinstance(attr, Serializable):
                        attr.from_dict(value)
                    else:
                        setattr(self, key, value)
        except (FileNotFoundError, OSError, toml.TomlDecodeError):
            logger.exception("Error loading configuration")
            exit(1)
        self.save()

    def save(self) -> None:
        self.gather_missing_data(self)
        data = self.to_dict()
        try:
            toml_data = toml.dumps(data)
            self.filepath.write_text(toml_data)
        except KeyError as key_error:
            logger.error(f"KeyError when saving configuration: {key_error}")
            logger.debug(f"Configuration data: {data}")
        except (FileNotFoundError, OSError) as error:
            logger.exception(f"Error saving configuration: {str(error)}")

    def update_and_save(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            parts = key.split("__")
            if len(parts) > 1 and hasattr(self, parts[0]):
                obj = getattr(self, parts[0])
                setattr(obj, parts[1], value)
            else:
                setattr(self, key, value)
        self.save()

    @staticmethod
    def get_all_keys(instance: object) -> set[str]:
        instance_keys = set(instance.__dict__.keys())
        annotation_keys = set(instance.__annotations__.keys()) if hasattr(instance, "__annotations__") else set()
        return instance_keys | annotation_keys

    @staticmethod
    def gather_missing_data(instance: Serializable, parent_name: str = "") -> None:
        all_keys = Config.get_all_keys(instance)
        for key in all_keys:
            if not key.startswith("_"):
                value = getattr(instance, key, None)
                full_key_name = f"{parent_name}.{key}" if parent_name else key
                if value == "from_terminal":
                    new_value = input(f"{full_key_name} not in configuration. Please enter a value: ")
                    setattr(instance, key, new_value)
                elif isinstance(value, Serializable):
                    Config.gather_missing_data(value, full_key_name)


config = Config()

if __name__ == "__main__":
    config = Config.get_instance()
    print(config.discord.token)
