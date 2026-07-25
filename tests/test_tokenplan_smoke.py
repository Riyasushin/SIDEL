from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path("scripts/test_tokenplan.py")


def load_script_module():
    spec = importlib.util.spec_from_file_location("test_tokenplan", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TokenPlanSmokeTest(unittest.TestCase):
    def test_load_config_reads_env_file_without_overriding_environment(self) -> None:
        module = load_script_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DEEPSEEK_BASE_URL=https://example.test/v1\n"
                "DEEPSEEK_API_KEY=file-key\n"
                "DEEPSEEK_MODEL=file-model\n",
                encoding="utf-8",
            )
            old_key = os.environ.get("DEEPSEEK_API_KEY")
            os.environ["DEEPSEEK_API_KEY"] = "environment-key"
            try:
                config = module.load_config(path)
            finally:
                if old_key is None:
                    del os.environ["DEEPSEEK_API_KEY"]
                else:
                    os.environ["DEEPSEEK_API_KEY"] = old_key

        self.assertEqual(config.base_url, "https://example.test/v1")
        self.assertEqual(config.api_key, "environment-key")
        self.assertEqual(config.model, "file-model")

    def test_summarize_error_never_includes_authorization_value(self) -> None:
        module = load_script_module()
        summary = module.summarize_error(
            '{"error":{"message":"Invalid API key secret-token-value","code":"invalid_api_key"}}'
        )
        self.assertEqual(summary, "code=invalid_api_key; message=Invalid API key [redacted]")


if __name__ == "__main__":
    unittest.main()
