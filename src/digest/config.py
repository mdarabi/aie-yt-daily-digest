"""Configuration: .env loading and typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def project_root() -> Path:
    """The repo root: DIGEST_HOME env var, else the ancestor holding pyproject.toml."""
    if env := os.environ.get("DIGEST_HOME"):
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ.

    Later lines in the file override earlier ones (so appending works), but real
    environment variables always win over the file.
    """
    if not path.exists():
        return
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value.strip())


@dataclass
class Config:
    root: Path
    resend_api_key: str = ""
    email_from: str = "AIE Digest <digest@example.com>"
    email_to: str = ""  # required — set EMAIL_TO in .env
    channel_id: str = "UCLKPca3kwwd-B59HNr-_lvA"
    claude_model: str = "sonnet"
    claude_bin: str = ""
    claude_timeout: int = 600
    lookback_hours: int = 26
    max_catchup_hours: int = 168
    caption_grace_hours: int = 48
    send_empty_digest: bool = False
    error_emails: bool = True
    include_shorts: bool = False
    shorts_max_seconds: int = 75
    transcript_char_limit: int = 400_000
    seen_retention_days: int = 120
    deferred_retention_days: int = 7

    state_file: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.state_file = self.root / "state" / "state.json"
        self.log_file = self.root / "logs" / "digest.log"

    @property
    def uploads_playlist_id(self) -> str:
        """The channel's full uploads playlist (UC... -> UU...), which has no 15-item cap."""
        if self.channel_id.startswith("UC"):
            return "UU" + self.channel_id[2:]
        return self.channel_id

    @classmethod
    def load(cls) -> "Config":
        root = project_root()
        load_dotenv(root / ".env")
        env = os.environ
        return cls(
            root=root,
            resend_api_key=env.get("RESEND_API_KEY", "").strip(),
            email_from=env.get("EMAIL_FROM", cls.email_from).strip() or cls.email_from,
            email_to=env.get("EMAIL_TO", cls.email_to).strip() or cls.email_to,
            channel_id=env.get("CHANNEL_ID", cls.channel_id).strip() or cls.channel_id,
            claude_model=env.get("CLAUDE_MODEL", cls.claude_model).strip() or cls.claude_model,
            claude_bin=env.get("CLAUDE_BIN", "").strip(),
            claude_timeout=_int(env.get("CLAUDE_TIMEOUT"), cls.claude_timeout),
            lookback_hours=_int(env.get("LOOKBACK_HOURS"), cls.lookback_hours),
            max_catchup_hours=_int(env.get("MAX_CATCHUP_HOURS"), cls.max_catchup_hours),
            caption_grace_hours=_int(env.get("CAPTION_GRACE_HOURS"), cls.caption_grace_hours),
            send_empty_digest=_bool(env.get("SEND_EMPTY_DIGEST"), cls.send_empty_digest),
            error_emails=_bool(env.get("ERROR_EMAILS"), cls.error_emails),
            include_shorts=_bool(env.get("INCLUDE_SHORTS"), cls.include_shorts),
        )
