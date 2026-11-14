"""Architectural boundaries, secret hygiene and the command-line entry point."""

import ast
import contextlib
import io
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from heimdall.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "heimdall"


def heimdall_imports(path: Path) -> set[str]:
    """Absolute module names of heimdall-internal imports in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package_parts = path.relative_to(ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("heimdall"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                module = ".".join(base + ((node.module,) if node.module else ()))
                found.add(module)
            elif node.module and node.module.startswith("heimdall"):
                found.add(node.module)
    return found


class ImportBoundaryTests(unittest.TestCase):
    def test_contracts_depend_only_on_contracts(self):
        for path in (PACKAGE / "contracts").glob("*.py"):
            for module in heimdall_imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(module.startswith("heimdall.contracts"), module)

    def test_data_access_does_not_import_pipeline_or_presentation(self):
        for path in (PACKAGE / "data_access").glob("*.py"):
            for module in heimdall_imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertFalse(module.startswith(("heimdall.pipeline", "heimdall.serialization")), module)

    def test_package_never_imports_test_fixtures(self):
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn("tests.fixtures", text)
                self.assertNotIn("SyntheticFixtureSource", text)


class SecretHygieneTests(unittest.TestCase):
    def test_gitignore_excludes_env_files_and_keeps_example(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", lines)
        self.assertIn("!.env.example", lines)
        self.assertIn(".DS_Store", lines)  # pre-existing entry preserved

    def test_env_example_holds_placeholders_only(self):
        values = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        self.assertIn("HEIMDALL_SOURCE_CREDENTIAL", values)
        self.assertEqual(values["HEIMDALL_SOURCE_CREDENTIAL"], "")

    def test_no_credential_like_literals_in_package(self):
        pattern = re.compile(r"(api[_-]?key|token|password|secret)\s*=\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE)
        for path in PACKAGE.rglob("*.py"):
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))


class CliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HEIMDALL_")}
        clean.update(env or {})
        with mock.patch.dict(os.environ, clean, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("heimdall.__main__.Path.is_file", return_value=False):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        code, out, _ = self.run_cli("version")
        self.assertEqual(code, 0)
        self.assertIn("schema", out)

    def test_check_unconfigured_is_not_an_error(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("SOURCE_NOT_CONFIGURED", out)

    def test_check_never_prints_credential(self):
        secret = "test-only-cli-credential-91c2"
        code, out, err = self.run_cli("check", env={"HEIMDALL_SOURCE_CREDENTIAL": secret})
        self.assertNotIn(secret, out + err)
        self.assertIn("source_credential: set", out)

    def test_check_invalid_config_returns_error_code(self):
        code, _, err = self.run_cli("check", env={"HEIMDALL_LOG_LEVEL": "LOUD"})
        self.assertEqual(code, 2)
        self.assertIn("CONFIG_INVALID", err)

    def test_unknown_command(self):
        code, _, err = self.run_cli("launch")
        self.assertEqual(code, 2)
        self.assertIn("usage", err)


if __name__ == "__main__":
    unittest.main()
