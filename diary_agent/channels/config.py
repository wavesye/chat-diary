"""Channel switches live in JSON; credentials only come from the environment."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


CHANNEL_NAMES = ("telegram", "wechat", "qq", "line", "whatsapp")
ENV_FIELDS = {
    "telegram": ("bot_token", "allowed_user_id", "message_debounce_seconds"),
    "wechat": ("app_id", "app_secret", "token", "encoding_aes_key"),
    "qq": ("app_id", "app_secret"),
    "line": ("channel_access_token", "channel_secret"),
    "whatsapp": ("access_token", "phone_number_id", "app_secret", "verify_token", "api_version"),
}
REQUIRED_FIELDS = {
    "telegram": ("bot_token",),
    "wechat": ("app_id", "app_secret", "token"),
    "qq": ("app_id", "app_secret"),
    "line": ENV_FIELDS["line"],
    "whatsapp": ENV_FIELDS["whatsapp"],
}


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    enabled: bool
    credentials: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ChannelsConfig:
    channels: dict[str, ChannelConfig]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ChannelsConfig":
        env = os.environ if environ is None else environ
        configured = {}
        config_path = env.get("CHANNELS_CONFIG", "").strip()
        if config_path:
            try:
                document = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("CHANNELS_CONFIG must point to a readable JSON config") from error
            if not isinstance(document, dict) or set(document) != {"channels"}:
                raise ValueError("Channel config must contain only a channels object")
            configured = document["channels"]
            if not isinstance(configured, dict) or set(configured) - set(CHANNEL_NAMES):
                raise ValueError("Channel config contains an invalid or unsupported channel")

        channels = {}
        for name in CHANNEL_NAMES:
            options = configured.get(name, {})
            if not isinstance(options, dict) or set(options) - {"enabled"}:
                raise ValueError(f"channels.{name} accepts only enabled; put credentials in environment variables")
            enabled = options.get("enabled", name == "telegram")
            if not isinstance(enabled, bool):
                raise ValueError(f"channels.{name}.enabled must be a JSON boolean")
            switch = f"CHANNEL_{name.upper()}_ENABLED"
            if switch in env:
                value = env[switch].strip().lower()
                if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                    raise ValueError(f"{switch} must be true or false")
                enabled = value in {"true", "1", "yes", "on"}
            credentials = {
                key: env.get(f"{name.upper()}_{key.upper()}", "").strip()
                for key in ENV_FIELDS[name]
            }
            if name == "telegram" and not credentials["message_debounce_seconds"]:
                credentials["message_debounce_seconds"] = "3"
            if enabled:
                missing = [f"{name.upper()}_{key.upper()}" for key in REQUIRED_FIELDS[name]
                           if not credentials[key]]
                if missing:
                    raise ValueError("Missing channel environment variables: " + ", ".join(missing))
                if name == "telegram":
                    raw_user = credentials["allowed_user_id"]
                    if raw_user and not re.fullmatch(r"[1-9][0-9]*", raw_user):
                        raise ValueError("TELEGRAM_ALLOWED_USER_ID must be a positive integer")
                    try:
                        delay = float(credentials["message_debounce_seconds"])
                    except ValueError as error:
                        raise ValueError("TELEGRAM_MESSAGE_DEBOUNCE_SECONDS must be between 0 and 30") from error
                    if not math.isfinite(delay) or not 0 <= delay <= 30:
                        raise ValueError("TELEGRAM_MESSAGE_DEBOUNCE_SECONDS must be between 0 and 30")
                if name == "whatsapp":
                    if not re.fullmatch(r"v[0-9]+\.[0-9]+", credentials["api_version"]):
                        raise ValueError("WHATSAPP_API_VERSION must use the form vNN.N")
                    if not credentials["phone_number_id"].isdigit():
                        raise ValueError("WHATSAPP_PHONE_NUMBER_ID must contain digits only")
            channels[name] = ChannelConfig(name, enabled, credentials)
        return cls(channels)
