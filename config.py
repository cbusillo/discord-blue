import logging
import toml
from pathlib import Path

logger = logging.getLogger(__name__)


class Serializable:
    def to_dict(self) -> dict[str, any]:
        result = {}
        instance_keys = set(self.__dict__.keys())
        annotation_keys = set(self.__annotations__.keys()) if hasattr(self, '__annotations__') else set()
        all_keys = instance_keys | annotation_keys
        for key in all_keys:
            if not key.startswith("_"):
                value = getattr(self, key, None)
                if value == "from_terminal":
                    value = input(f"{key} not in configuration. Please enter a value: ")
                if isinstance(value, Serializable):
                    result[key] = value.to_dict()
                else:
                    result[key] = value
        return result

    def from_dict(self, data: dict) -> None:
        for key, type_hint in self.__annotations__.items():
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
        for key, value in self.__annotations__.items():
            if getattr(self, key, None) is None:
                logger.warning(f"Warning: Configuration value '{key}' is missing or None in {self.__class__.__name__}")


class DiscordConfig(Serializable):
    api_key: str = "from_terminal"
    timeout: int = 10


class PrintnodeConfig(Serializable):
    api_key: str = "from_terminal"
    port: int = 80


class Config(Serializable):
    _instance = None
    _filepath = Path.home() / ".config" / Path(__file__).parent.stem.replace("_", "-") / "config.toml"

    debug: bool = True

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)

            cls._instance.discord = DiscordConfig()
            cls._instance.printnode = PrintnodeConfig()

            cls._instance._filepath.parent.mkdir(parents=True, exist_ok=True)
            if cls._instance._filepath.exists():
                cls._instance.load()
            cls._instance.save()
        return cls._instance

    def save(self) -> None:
        try:
            with self._filepath.open("w") as f:
                data = self.to_dict()
                toml.dump(data, f)
        except (FileNotFoundError, OSError) as error:
            logger.exception(f"Error saving configuration: {str(error)}")

    def load(self) -> None:
        try:
            with self._filepath.open() as f:
                data = toml.load(f)
                for key, value in data.items():
                    if key.startswith("_"):  # Skip keys starting with an underscore
                        continue
                    attr = getattr(self, key, None)
                    if isinstance(attr, Serializable):
                        attr.from_dict(value)
                    else:
                        setattr(self, key, value)
        except (FileNotFoundError, OSError, toml.TomlDecodeError) as error:
            logger.exception(f"Error loading configuration: {str(error)}")

    def update_and_save(self, **kwargs) -> None:
        for key, value in kwargs.items():
            parts = key.split("__")
            if len(parts) > 1 and hasattr(self, parts[0]):
                obj = getattr(self, parts[0])
                setattr(obj, parts[1], value)
            else:
                setattr(self, key, value)
        self.save()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    config = Config()

    config.discord.timeout = 20
    config.save()
