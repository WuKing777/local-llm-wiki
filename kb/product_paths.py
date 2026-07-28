import os
from pathlib import Path


APP_DIR_NAME = "LocalExobrain"
REGISTRY_FILE_NAME = "profiles.json"


def _env_value(name: str, env: dict[str, str] | None) -> str | None:
    source = os.environ if env is None else env
    value = source.get(name)
    return value if value else None


def _base_path(env_name: str, env: dict[str, str] | None) -> Path:
    value = _env_value(env_name, env)
    if value is None:
        return Path.home()
    return Path(value).expanduser()


def default_config_dir(env: dict[str, str] | None = None) -> Path:
    return _base_path("APPDATA", env) / APP_DIR_NAME


def default_cache_dir(env: dict[str, str] | None = None) -> Path:
    return _base_path("LOCALAPPDATA", env) / APP_DIR_NAME / "cache"


def default_log_dir(env: dict[str, str] | None = None) -> Path:
    return _base_path("LOCALAPPDATA", env) / APP_DIR_NAME / "logs"


def registry_path(
    config_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    if config_dir is None:
        return default_config_dir(env) / REGISTRY_FILE_NAME
    return Path(config_dir).expanduser() / APP_DIR_NAME / REGISTRY_FILE_NAME


def canonical_root(path: str | Path) -> Path:
    if isinstance(path, str) and not path.strip():
        raise RuntimeError("profile root must be non-empty")

    raw_path = Path(path)
    if any(part == ".." for part in raw_path.parts):
        raise RuntimeError("profile root must not contain path traversal")
    if not raw_path.is_absolute():
        raise RuntimeError("profile root must be absolute")
    return raw_path.resolve()
