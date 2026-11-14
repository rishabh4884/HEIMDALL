"""Configuration loaded from environment variables and an optional ``.env`` file.

Rules:
- Credentials are never hardcoded. They come from the environment or a local
  ``.env`` file, which is excluded from version control.
- Secrets are wrapped in ``Secret``; its ``repr``/``str`` never reveal the value
  and it cannot be serialized.
- Error messages name the offending variable, never its value.

The variable names for the real data source are PROVISIONAL. They are generic
until the authorized access method has been inspected.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .contracts.outcomes import ErrorCode, HeimdallError

ENV_DATA_SOURCE = "HEIMDALL_DATA_SOURCE"
ENV_SOURCE_CREDENTIAL = "HEIMDALL_SOURCE_CREDENTIAL"
ENV_CACHE_DIR = "HEIMDALL_CACHE_DIR"
ENV_LOG_LEVEL = "HEIMDALL_LOG_LEVEL"
ENV_REQUEST_TIMEOUT = "HEIMDALL_REQUEST_TIMEOUT_S"
ENV_MAX_RETRIES = "HEIMDALL_MAX_RETRIES"
ENV_STATE_DIR = "HEIMDALL_STATE_DIR"
ENV_LOCAL_FITS_DIR = "HEIMDALL_LOCAL_FITS_DIR"
ENV_OFFLINE = "HEIMDALL_OFFLINE"

KNOWN_VARIABLES = (
    ENV_DATA_SOURCE,
    ENV_SOURCE_CREDENTIAL,
    ENV_CACHE_DIR,
    ENV_LOG_LEVEL,
    ENV_REQUEST_TIMEOUT,
    ENV_MAX_RETRIES,
    ENV_STATE_DIR,
    ENV_LOCAL_FITS_DIR,
    ENV_OFFLINE,
)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Operational defaults only. They carry no scientific meaning.
DEFAULT_CACHE_DIR = ".heimdall/cache"
DEFAULT_STATE_DIR = ".heimdall/state"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_REQUEST_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 3
MAX_RETRIES_LIMIT = 10


class Secret:
    """Holds a secret string without ever exposing it by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or value == "":
            raise ValueError("a Secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        """Return the secret. Call only where the credential is actually sent."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self):  # prevent pickling the raw value
        raise TypeError("Secret values cannot be pickled")


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    data_source: str | None
    source_credential: Secret | None
    cache_dir: Path
    log_level: str
    request_timeout_s: float
    max_retries: int
    # Investigation runs and validation records (git-ignored by default).
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    # Directory scanned by the local-fits adapter.
    local_fits_dir: Path | None = None
    # Offline: never contact a network service; use cached responses and products only.
    offline: bool = False

    def secret_values(self) -> tuple[str, ...]:
        """Raw secret strings, for log redaction only."""
        return (self.source_credential.reveal(),) if self.source_credential else ()

    def summary(self) -> dict[str, str]:
        """A display-safe summary that never contains secret values."""
        return {
            "data_source": self.data_source or "(not configured)",
            "source_credential": "set" if self.source_credential else "not set",
            "cache_dir": str(self.cache_dir),
            "log_level": self.log_level,
            "request_timeout_s": str(self.request_timeout_s),
            "max_retries": str(self.max_retries),
            "state_dir": str(self.state_dir),
            "local_fits_dir": str(self.local_fits_dir) if self.local_fits_dir else "(not set)",
            "offline": str(self.offline).lower(),
        }


def _config_error(variable: str, problem: str) -> HeimdallError:
    return HeimdallError.of(ErrorCode.CONFIG_INVALID, f"{variable}: {problem}", variable=variable)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``.env`` file: ``KEY=VALUE`` lines, ``#`` comments, optional quotes."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HeimdallError.of(ErrorCode.CONFIG_INVALID, f"cannot read env file: {exc.strerror}", path=str(path)) from None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            # The line may contain a secret, so report only its number.
            raise HeimdallError.of(ErrorCode.CONFIG_INVALID, f"malformed line {number} in env file", path=str(path))
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """Build settings from an env file (if given) overridden by the environment."""
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_env_file(env_file))
    source = os.environ if environ is None else environ
    merged.update({k: v for k, v in source.items() if k in KNOWN_VARIABLES})

    def get(name: str) -> str | None:
        value = merged.get(name)
        return value.strip() if value is not None and value.strip() != "" else None

    data_source = get(ENV_DATA_SOURCE)
    if data_source is not None and not data_source.replace("-", "").replace("_", "").isalnum():
        raise _config_error(ENV_DATA_SOURCE, "must contain only letters, digits, '-' and '_'")

    credential_raw = merged.get(ENV_SOURCE_CREDENTIAL)
    credential = Secret(credential_raw) if credential_raw else None

    log_level = (get(ENV_LOG_LEVEL) or DEFAULT_LOG_LEVEL).upper()
    if log_level not in LOG_LEVELS:
        raise _config_error(ENV_LOG_LEVEL, f"must be one of {', '.join(LOG_LEVELS)}")

    timeout_raw = get(ENV_REQUEST_TIMEOUT)
    try:
        timeout = float(timeout_raw) if timeout_raw is not None else DEFAULT_REQUEST_TIMEOUT_S
    except ValueError:
        raise _config_error(ENV_REQUEST_TIMEOUT, "must be a number of seconds") from None
    if not timeout > 0 or timeout == float("inf"):
        raise _config_error(ENV_REQUEST_TIMEOUT, "must be a positive, finite number of seconds")

    retries_raw = get(ENV_MAX_RETRIES)
    try:
        retries = int(retries_raw) if retries_raw is not None else DEFAULT_MAX_RETRIES
    except ValueError:
        raise _config_error(ENV_MAX_RETRIES, "must be an integer") from None
    if not 0 <= retries <= MAX_RETRIES_LIMIT:
        raise _config_error(ENV_MAX_RETRIES, f"must be between 0 and {MAX_RETRIES_LIMIT}")

    return Settings(
        data_source=data_source,
        source_credential=credential,
        cache_dir=Path(get(ENV_CACHE_DIR) or DEFAULT_CACHE_DIR),
        log_level=log_level,
        request_timeout_s=timeout,
        max_retries=retries,
        state_dir=Path(get(ENV_STATE_DIR) or DEFAULT_STATE_DIR),
        local_fits_dir=Path(get(ENV_LOCAL_FITS_DIR)) if get(ENV_LOCAL_FITS_DIR) else None,
        offline=_flag(get(ENV_OFFLINE)),
    )


def _flag(value: str | None) -> bool:
    if value is None:
        return False
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise _config_error(ENV_OFFLINE, "must be 1/0, true/false, yes/no or on/off")
