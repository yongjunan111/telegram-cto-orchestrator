"""Configuration loading for orchctl.

Config file: .orchestrator/config.yaml (repo-local)
Example:     .orchestrator/config.example.yaml

Missing config file is not an error — all values have sensible defaults.
"""
import os
import yaml

from . import storage


CONFIG_PATH = os.path.join(storage.ORCHESTRATOR_DIR, "config.yaml")


class ConfigError(Exception):
    pass


CONFIG_EXAMPLE_PATH = os.path.join(storage.ORCHESTRATOR_DIR, "config.example.yaml")

_DEFAULTS = {
    "worker": {
        "permissions_mode": "normal",  # normal | skip-permissions
        "claude_bin": "claude",
    },
    "dispatch": {
        "auto_launch_worker": True,
        "auto_register_peer": True,
    },
}


def load_config() -> dict:
    """Load config from .orchestrator/config.yaml, merged with defaults.

    Returns defaults if the file does not exist or is empty.
    Raises ConfigError if the file exists but is malformed or not a mapping.
    """
    config = _deep_copy_defaults()
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {CONFIG_PATH}: {e}")
        if raw is None:
            return config
        if not isinstance(raw, dict):
            raise ConfigError(f"{CONFIG_PATH} must be a YAML mapping, got {type(raw).__name__}")
        _deep_merge(config, raw)
    return config


def _deep_copy_defaults() -> dict:
    """Return a fresh deep copy of defaults."""
    import copy
    return copy.deepcopy(_DEFAULTS)


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in place. Nested dicts are merged recursively."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


