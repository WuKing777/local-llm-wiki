import json
import os
import re
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .product_paths import canonical_root


PROFILE_FIELDS = (
    "id",
    "name",
    "root",
    "kind",
    "created_at",
    "last_health_status",
    "last_health_at",
)
SECRET_FIELD_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|credential|authorization|bearer|private[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|credential|bearer)\b)",
    re.IGNORECASE,
)


def _raise_secret_field() -> None:
    raise RuntimeError("secret field is not allowed")


def _check_no_secret_shape(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_FIELD_RE.search(str(key)):
                _raise_secret_field()
            _check_no_secret_shape(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _check_no_secret_shape(item)
        return
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        _raise_secret_field()


def _product_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _root_key(root: str | Path) -> str:
    return os.path.normcase(str(canonical_root(root)))


def _profile_id(root: Path) -> str:
    digest = sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:12]
    return f"profile-{digest}"


def _minimal_profile(
    profile: dict[str, Any], *, allow_product_repo_root_for_test: bool = False
) -> dict[str, Any]:
    _check_no_secret_shape(
        {key: value for key, value in profile.items() if key not in PROFILE_FIELDS}
    )
    profile_root = _validated_profile_root(
        profile.get("root", ""), allow_product_repo_root_for_test
    )
    minimal = {
        "id": str(profile.get("id", "")),
        "name": str(profile.get("name", "")),
        "root": str(profile_root),
        "kind": str(profile.get("kind", "")),
        "created_at": profile.get("created_at") or _now_iso(),
        "last_health_status": profile.get("last_health_status") or "unknown",
        "last_health_at": profile.get("last_health_at"),
    }
    _check_no_secret_shape(minimal)
    return minimal


def _normalize_registry(
    registry: dict[str, Any], *, allow_product_repo_root_for_test: bool = False
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(registry, dict):
        raise RuntimeError("profile registry must be an object")

    _check_no_secret_shape(
        {key: value for key, value in registry.items() if key != "profiles"}
    )
    profiles = registry.get("profiles", [])
    if not isinstance(profiles, list):
        raise RuntimeError("profile registry profiles must be a list")

    normalized: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise RuntimeError("profile entry must be an object")
        normalized.append(
            _minimal_profile(
                profile,
                allow_product_repo_root_for_test=allow_product_repo_root_for_test,
            )
        )
    return {"profiles": normalized}


def load_profiles(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    registry_path = Path(path)
    if not registry_path.exists():
        return {"profiles": []}
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read profile registry: {registry_path}") from exc
    return _normalize_registry(data)


def _save_profiles(
    path: str | Path,
    registry: dict[str, Any],
    *,
    allow_product_repo_root_for_test: bool = False,
) -> None:
    registry_path = Path(path)
    normalized = _normalize_registry(
        registry, allow_product_repo_root_for_test=allow_product_repo_root_for_test
    )
    content = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{registry_path.name}.",
        suffix=".tmp",
        dir=registry_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            temp_file.write(content)
        os.replace(temp_path, registry_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def save_profiles(path: str | Path, registry: dict[str, Any]) -> None:
    _save_profiles(path, registry)


def _validated_profile_root(root: str | Path, allow_product_repo_root: bool) -> Path:
    profile_root = canonical_root(root)
    if (
        os.path.normcase(str(profile_root))
        == os.path.normcase(str(_product_repo_root()))
        and not allow_product_repo_root
    ):
        raise RuntimeError("profile root must not be the product repository root")
    return profile_root


def add_or_update_profile(
    path: str | Path,
    *,
    name: str,
    root: str | Path,
    kind: str,
    extra: dict[str, Any] | None = None,
    _allow_product_repo_root_for_test: bool = False,
) -> dict[str, Any]:
    _check_no_secret_shape(extra or {})
    profile_root = _validated_profile_root(root, _allow_product_repo_root_for_test)
    registry = load_profiles(path)

    existing: dict[str, Any] | None = None
    profile_root_key = _root_key(profile_root)
    for profile in registry["profiles"]:
        if _root_key(profile["root"]) == profile_root_key:
            existing = profile
            break

    if existing is None:
        existing = {
            "id": _profile_id(profile_root),
            "created_at": _now_iso(),
            "last_health_status": "unknown",
            "last_health_at": None,
        }
        registry["profiles"].append(existing)

    existing.update(
        {
            "name": str(name),
            "root": str(profile_root),
            "kind": str(kind),
            "last_health_status": existing.get("last_health_status") or "unknown",
            "last_health_at": existing.get("last_health_at"),
        }
    )
    _save_profiles(
        path,
        registry,
        allow_product_repo_root_for_test=_allow_product_repo_root_for_test,
    )
    return dict(existing)


def list_profiles(path: str | Path) -> list[dict[str, Any]]:
    registry = load_profiles(path)
    return sorted(
        (dict(profile) for profile in registry["profiles"]),
        key=lambda profile: (profile["name"], profile["id"]),
    )
