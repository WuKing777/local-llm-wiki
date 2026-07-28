import io
import importlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _product_modules():
    try:
        product_paths = importlib.import_module("kb.product_paths")
        profile_registry = importlib.import_module("kb.profile_registry")
    except ModuleNotFoundError as exc:
        raise AssertionError(f"required module missing: {exc.name}") from None
    return product_paths, profile_registry


class ProfileRegistryTests(unittest.TestCase):
    def test_registry_path_uses_explicit_appdata_base_and_env_default(self):
        product_paths, _ = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)

            self.assertEqual(
                product_paths.registry_path(config_dir=base),
                base / "LocalExobrain" / "profiles.json",
            )
            self.assertEqual(
                product_paths.registry_path(env={"APPDATA": str(base)}),
                base / "LocalExobrain" / "profiles.json",
            )

    def test_add_profile_writes_minimal_utf8_registry_and_updates_same_root(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            registry = base / "LocalExobrain" / "profiles.json"
            root = base / "vault"
            first = profile_registry.add_or_update_profile(
                registry, name="Beta", root=root, kind="personal_exobrain"
            )
            second = profile_registry.add_or_update_profile(
                registry, name="Alpha", root=root, kind="workspace"
            )

            self.assertTrue(registry.is_file())
            self.assertFalse(root.exists(), "registry helpers must not create roots")
            data = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(len(data["profiles"]), 1)
            profile = data["profiles"][0]
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(profile["name"], "Alpha")
            self.assertEqual(profile["kind"], "workspace")
            self.assertEqual(profile["root"], str(root.resolve()))
            self.assertEqual(
                sorted(profile),
                [
                    "created_at",
                    "id",
                    "kind",
                    "last_health_at",
                    "last_health_status",
                    "name",
                    "root",
                ],
            )

    def test_secret_extra_fields_are_rejected_without_leaving_registry_file(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            root = Path(temp) / "vault"

            with self.assertRaisesRegex(RuntimeError, "secret field is not allowed"):
                profile_registry.add_or_update_profile(
                    registry,
                    name="Unsafe",
                    root=root,
                    kind="personal_exobrain",
                    extra={"api_key": "sentinel-secret-value"},
                )

            self.assertFalse(registry.exists())

    def test_rejects_named_password_token_and_refresh_token_fields(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            root = Path(temp) / "vault"

            for field in ("password", "token", "refresh_token"):
                with self.subTest(field=field):
                    with self.assertRaisesRegex(
                        RuntimeError, "secret field is not allowed"
                    ):
                        profile_registry.add_or_update_profile(
                            registry,
                            name="Unsafe",
                            root=root,
                            kind="personal_exobrain",
                            extra={field: "not-persisted"},
                        )
                    self.assertFalse(registry.exists())

    def test_save_profiles_rejects_secret_values_and_invalid_roots_without_file(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            base = Path(temp)

            secret_registry = {
                "profiles": [
                    {
                        "id": "profile-a",
                        "name": "sentinel-secret-value",
                        "root": str((base / "vault").resolve()),
                        "kind": "personal_exobrain",
                        "created_at": "2026-07-03T00:00:00+08:00",
                        "last_health_status": "unknown",
                        "last_health_at": None,
                    }
                ]
            }
            with self.assertRaisesRegex(RuntimeError, "secret field is not allowed"):
                profile_registry.save_profiles(registry, secret_registry)
            self.assertFalse(registry.exists())

            invalid_root_registry = {
                "profiles": [
                    {
                        "id": "profile-b",
                        "name": "Safe",
                        "root": "relative-vault",
                        "kind": "personal_exobrain",
                        "created_at": "2026-07-03T00:00:00+08:00",
                        "last_health_status": "unknown",
                        "last_health_at": None,
                    }
                ]
            }
            with self.assertRaises(RuntimeError):
                profile_registry.save_profiles(registry, invalid_root_registry)
            self.assertFalse(registry.exists())

    def test_registry_json_never_contains_secret_shapes_or_forbidden_words(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            root = Path(temp) / "vault"
            profile_registry.add_or_update_profile(
                registry, name="Safe", root=root, kind="personal_exobrain"
            )
            text = registry.read_text(encoding="utf-8").casefold()

            self.assertNotIn("sk-", text)
            self.assertNotIn("token", text)
            self.assertNotIn("password", text)
            self.assertNotIn("sentinel-secret-value", text)

    def test_rejects_unsafe_roots_and_current_repo_root_unless_explicitly_allowed(self):
        _, profile_registry = _product_modules()
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            cases = ("", "relative-vault", Path(temp) / ".." / "elsewhere")

            for root in cases:
                with self.subTest(root=str(root)):
                    with self.assertRaises(RuntimeError):
                        profile_registry.add_or_update_profile(
                            registry,
                            name="Unsafe",
                            root=root,
                            kind="personal_exobrain",
                        )

            with self.assertRaisesRegex(RuntimeError, "product repository"):
                profile_registry.add_or_update_profile(
                    registry,
                    name="Repo",
                    root=repo_root,
                    kind="personal_exobrain",
                )

            allowed = profile_registry.add_or_update_profile(
                registry,
                name="Repo",
                root=repo_root,
                kind="test",
                _allow_product_repo_root_for_test=True,
            )
            self.assertEqual(allowed["root"], str(repo_root))

    def test_list_profiles_returns_profiles_sorted_by_name_then_id(self):
        _, profile_registry = _product_modules()

        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "LocalExobrain" / "profiles.json"
            base = Path(temp)
            profile_registry.save_profiles(
                registry,
                {
                    "profiles": [
                        {
                            "id": "profile-c",
                            "name": "Beta",
                            "root": str((base / "c").resolve()),
                            "kind": "personal_exobrain",
                            "created_at": "2026-07-03T00:00:00+08:00",
                            "last_health_status": "unknown",
                            "last_health_at": None,
                        },
                        {
                            "id": "profile-b",
                            "name": "Alpha",
                            "root": str((base / "b").resolve()),
                            "kind": "personal_exobrain",
                            "created_at": "2026-07-03T00:00:00+08:00",
                            "last_health_status": "unknown",
                            "last_health_at": None,
                        },
                        {
                            "id": "profile-a",
                            "name": "Alpha",
                            "root": str((base / "a").resolve()),
                            "kind": "personal_exobrain",
                            "created_at": "2026-07-03T00:00:00+08:00",
                            "last_health_status": "unknown",
                            "last_health_at": None,
                        },
                    ]
                },
            )

            self.assertEqual(
                [profile["id"] for profile in profile_registry.list_profiles(registry)],
                ["profile-a", "profile-b", "profile-c"],
            )

    def test_cli_profile_add_and_list_use_config_override_without_creating_root(self):
        _product_modules()
        from kb.cli import main

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "vault"
            output = io.StringIO()

            with patch.dict(os.environ, {"APPDATA": str(base)}, clear=False):
                with redirect_stdout(output):
                    add_code = main(
                        [
                            "profile-add",
                            "--config-dir",
                            str(base),
                            "--name",
                            "Vault",
                            "--root",
                            str(root),
                            "--kind",
                            "personal_exobrain",
                        ]
                    )
                with redirect_stdout(output):
                    list_code = main(["profile-list", "--config-dir", str(base)])

            self.assertEqual(add_code, 0)
            self.assertEqual(list_code, 0)
            self.assertFalse(root.exists(), "profile-add must not create roots")
            self.assertIn("Vault", output.getvalue())
            self.assertIn(str(root.resolve()), output.getvalue())


if __name__ == "__main__":
    unittest.main()
