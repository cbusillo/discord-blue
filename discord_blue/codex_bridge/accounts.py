from __future__ import annotations

import tomllib
from pathlib import Path


def supervisor_homes(path: Path) -> list[Path]:
    """Read only OpenAI home mappings, without executing the capacity reader or opening credentials."""
    data = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    section = data.get("accounts")
    accounts = section.get("account") if isinstance(section, dict) else None
    if not isinstance(accounts, list):
        raise ValueError("Supervisor config needs [[accounts.account]] entries")
    homes: list[Path] = []
    for account in accounts:
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
