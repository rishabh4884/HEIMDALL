import io
import json
import logging
import pickle
import tempfile
import unittest
from pathlib import Path

from heimdall.config import (
    ENV_DATA_SOURCE,
    ENV_LOG_LEVEL,
    ENV_MAX_RETRIES,
    ENV_REQUEST_TIMEOUT,
    ENV_SOURCE_CREDENTIAL,
    Secret,
    load_settings,
    parse_env_file,
)
from heimdall.contracts import ErrorCode, HeimdallError
from heimdall.logging_setup import configure_logging
from heimdall.serialization import dumps, to_jsonable

FAKE_SECRET = "test-only-not-a-real-credential-7f3a"


class SettingsTests(unittest.TestCase):
    def test_defaults_without_environment(self):
        settings = load_settings(environ={})
        self.assertIsNone(settings.data_source)
        self.assertIsNone(settings.source_credential)
        self.assertEqual(settings.log_level, "INFO")

    def test_empty_credential_is_treated_as_missing(self):
        self.assertIsNone(load_settings(environ={ENV_SOURCE_CREDENTIAL: ""}).source_credential)

    def test_unrelated_environment_variables_ignored(self):
        settings = load_settings(environ={"PATH": "/bin", "HOME": "/tmp"})
        self.assertIsNone(settings.data_source)

    def test_invalid_values_raise_config_invalid_naming_variable_only(self):
        cases = {
            ENV_LOG_LEVEL: "LOUD",
            ENV_REQUEST_TIMEOUT: "-5",
            ENV_MAX_RETRIES: "99",
            ENV_DATA_SOURCE: "bad source!",
        }
        for variable, value in cases.items():
            with self.subTest(variable=variable):
                with self.assertRaises(HeimdallError) as ctx:
                    load_settings(environ={variable: value})
                self.assertEqual(ctx.exception.code, ErrorCode.CONFIG_INVALID)
                self.assertIn(variable, ctx.exception.info.message)

    def test_non_numeric_timeout(self):
        with self.assertRaises(HeimdallError):
            load_settings(environ={ENV_REQUEST_TIMEOUT: "soon"})

    def test_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f'# comment\nexport {ENV_DATA_SOURCE}="from-file"\n{ENV_LOG_LEVEL}=debug\n')
            settings = load_settings(environ={ENV_DATA_SOURCE: "from-env"}, env_file=env_file)
            self.assertEqual(settings.data_source, "from-env")
            self.assertEqual(settings.log_level, "DEBUG")

    def test_malformed_env_file_does_not_echo_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f"{FAKE_SECRET}\n")
            with self.assertRaises(HeimdallError) as ctx:
                parse_env_file(env_file)
            self.assertNotIn(FAKE_SECRET, str(ctx.exception))
            self.assertNotIn(FAKE_SECRET, json.dumps(dict(ctx.exception.info.detail)))


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(environ={ENV_SOURCE_CREDENTIAL: FAKE_SECRET})

    def test_secret_never_appears_in_repr_str_or_summary(self):
        self.assertNotIn(FAKE_SECRET, repr(self.settings))
        self.assertNotIn(FAKE_SECRET, str(self.settings.source_credential))
        self.assertNotIn(FAKE_SECRET, json.dumps(self.settings.summary()))
        self.assertEqual(self.settings.source_credential.reveal(), FAKE_SECRET)

    def test_secret_cannot_be_serialized_or_pickled(self):
        with self.assertRaises(TypeError):
            to_jsonable(self.settings)
        with self.assertRaises(TypeError):
            pickle.dumps(Secret(FAKE_SECRET))

    def test_logging_redacts_secret(self):
        stream = io.StringIO()
        logger = configure_logging(self.settings, stream=stream)
        logger.info("token=%s", FAKE_SECRET)
        try:
            raise RuntimeError(f"failure containing {FAKE_SECRET}")
        except RuntimeError:
            logger.exception("request failed")
        output = stream.getvalue()
        self.assertNotIn(FAKE_SECRET, output)
        self.assertIn("***", output)
        logging.getLogger("heimdall").handlers.clear()


class SerializationTests(unittest.TestCase):
    def test_envelope_carries_schema_version(self):
        payload = json.loads(dumps({"a": 1}, request_id="req-1"))
        self.assertIn("schema_version", payload)
        self.assertEqual(payload["request_id"], "req-1")

    def test_non_finite_and_unknown_types_rejected(self):
        with self.assertRaises(ValueError):
            to_jsonable(float("nan"))
        with self.assertRaises(TypeError):
            to_jsonable(object())


if __name__ == "__main__":
    unittest.main()
