from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

HOME_VARIABLES = {"anthropic": ("claude", "CLAUDE_CONFIG_DIR"), "openai": ("codex", "CODEX_HOME")}


@dataclass(frozen=True, slots=True)
class LaunchAccount:
    name: str
    harness: str
    home: Path


def account_entries(path: Path) -> list[object]:
    data = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    section = data.get("accounts")
    accounts = section.get("account") if isinstance(section, dict) else None
    if not isinstance(accounts, list):
        raise ValueError("Supervisor config needs [[accounts.account]] entries")
    return accounts


def supervisor_homes(path: Path) -> list[Path]:
    """Read only OpenAI home mappings, without executing the capacity reader or opening credentials."""
    homes: list[Path] = []
    for account in account_entries(path):
        if not isinstance(account, dict):
            raise ValueError("Supervisor account entry must be a table")
        if account.get("provider") != "openai":
            continue
        env = account.get("env")
        value = env.get("CODEX_HOME") if isinstance(env, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Each OpenAI Supervisor account must set env.CODEX_HOME")
        home = Path(value).expanduser()
        if not home.is_absolute():
            raise ValueError("Supervisor CODEX_HOME must be absolute or start with ~/")
        homes.append(home.resolve())
    if not homes:
        raise ValueError("Supervisor config has no OpenAI accounts")
    return list(dict.fromkeys(homes))


def launch_account(entry: object) -> LaunchAccount | None:
    if not isinstance(entry, dict):
        return None
    provider = entry.get("provider")
    harness_variable = HOME_VARIABLES.get(provider) if isinstance(provider, str) else None
    name, env = entry.get("name"), entry.get("env")
    if harness_variable is None or not isinstance(name, str) or not name.strip() or not isinstance(env, dict):
        return None
    harness, variable = harness_variable
    value = env.get(variable)
    if not isinstance(value, str) or not value.strip():
        return None
    home = Path(value).expanduser()
    return LaunchAccount(name, harness, home.resolve()) if home.is_absolute() else None


def launch_accounts(path: Path) -> dict[str, LaunchAccount]:
    accounts: dict[str, LaunchAccount] = {}
    for entry in account_entries(path):
        if (account := launch_account(entry)) is not None:
            accounts.setdefault(account.name, account)
    return accounts
