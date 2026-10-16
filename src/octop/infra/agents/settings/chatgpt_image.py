"""Instance switches for image generation on the ChatGPT subscription."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from octop.infra.db.repos.settings import SettingsRepo

_KEY = "chatgpt_image_config"


@dataclass(frozen=True)
class ChatgptImageSettings:
    """Off by default; admin agents only; never spend purchased credits."""

    enabled: bool = False
    admin_only: bool = True
    allow_credits: bool = False


class ChatgptImageSettingsStore:
    def __init__(self, settings_repo: SettingsRepo) -> None:
        self._settings = settings_repo

    def load(self) -> ChatgptImageSettings:
        raw = self._settings.get(_KEY)
        if not raw:
            return ChatgptImageSettings()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ChatgptImageSettings()
        if not isinstance(data, dict):
            return ChatgptImageSettings()
        defaults = ChatgptImageSettings()
        return ChatgptImageSettings(
            enabled=_flag(data.get("enabled"), defaults.enabled),
            admin_only=_flag(data.get("admin_only"), defaults.admin_only),
            allow_credits=_flag(data.get("allow_credits"), defaults.allow_credits),
        )

    def save(self, value: ChatgptImageSettings) -> ChatgptImageSettings:
        self._settings.set(_KEY, json.dumps(asdict(value)))
        return value


def _flag(raw: object, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


__all__ = ["ChatgptImageSettings", "ChatgptImageSettingsStore"]
