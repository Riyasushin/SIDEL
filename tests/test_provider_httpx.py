from __future__ import annotations

import unittest
from pathlib import Path


class ProviderHttpxTest(unittest.TestCase):
    def test_upstream_clients_do_not_inherit_host_proxy_env(self) -> None:
        text = Path("router/providers/openai_compatible.py").read_text(encoding="utf-8")
        # Chat and Anthropic non-streaming requests share the retry helper; the
        # two streaming paths keep their own explicitly isolated clients.
        self.assertGreaterEqual(text.count("trust_env=False"), 3)
        self.assertNotIn("httpx.AsyncClient(timeout=self.timeout_seconds)", text)
        self.assertNotIn("httpx.AsyncClient(timeout=None)", text)
        self.assertIn("return status_code in {429, 500, 502, 503, 504}", text)
        self.assertIn('max_attempts or "unlimited"', text)


if __name__ == "__main__":
    unittest.main()
