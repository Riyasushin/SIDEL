from __future__ import annotations

import unittest
from pathlib import Path


class ProviderHttpxTest(unittest.TestCase):
    def test_upstream_clients_do_not_inherit_host_proxy_env(self) -> None:
        text = Path("router/providers/openai_compatible.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("trust_env=False"), 4)
        self.assertNotIn("httpx.AsyncClient(timeout=self.timeout_seconds)", text)
        self.assertNotIn("httpx.AsyncClient(timeout=None)", text)


if __name__ == "__main__":
    unittest.main()
