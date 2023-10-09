import logging
import toml
from typing import Any
from pathlib import Path

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
                else:
                    result[key] = value
        return result

    def from_dict(self, data: dict) -> None:
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


class DiscordConfig(Serializable):
    token: str = "from_terminal"
    guild_id: int = 0
    bot_channel_id: int = 0
    employee_role_name: str = ""
    loaded_doodads: list[str] = []


class PrintnodeConfig(Serializable):
    api_key: str = "from_terminal"
    front_printer_id: int = 0


class AssetLabelPrinterConfig(Serializable):
    schools: dict[str, str] = {}
    printers: dict[str, int] = {}


class Config(Serializable):
    _instance = None

    def __init__(self) -> None:
        self._filepath = Path.home() / ".config" / Path(__file__).parent.stem.replace("_", "-") / "config.toml"
        if not self.filepath.exists():
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self.filepath.touch()

        self.debug = True

        self.discord = DiscordConfig()
        self.printnode = PrintnodeConfig()
        self.asset_label_printer = AssetLabelPrinterConfig()

        self.load()  # Load config during instance creation

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
                    if key.startswith("_"):  # Skip keys starting with an underscore
                        continue
                    attr = getattr(self, key, None)
                    if isinstance(attr, Serializable):
                        attr.from_dict(value)
                    else:
                        setattr(self, key, value)
        except (FileNotFoundError, OSError, toml.TomlDecodeError):
            logger.exception(f"Error loading configuration")
            exit(1)
        self.save()

    def save(self) -> None:
        self.gather_missing_data(self)
        data = self.to_dict()
        try:
            with self.filepath.open("w") as file:
                toml.dump(data, file)
        except (FileNotFoundError, OSError) as error:
            logger.exception(f"Error saving configuration: {str(error)}")

    def update_and_save(self, **kwargs) -> None:
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
                    Config.gather_missing_data(value, full_key_name)  # Recursive call


config = Config()

if __name__ == "__main__":
    config = Config.get_instance()
    print(config.discord.token)
    # config.discord.api_key = "new_api_key"
    # update_and_save(discord__timeout=200, printnode__port=8080)
