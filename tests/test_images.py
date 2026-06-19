from __future__ import annotations

import unittest
from unittest.mock import patch

from swebench_container.images import candidate_swebench_images, default_swebench_image


class SwebenchImageTest(unittest.TestCase):
    def test_candidates_prefer_official_owner_repo_separator(self) -> None:
        candidates = candidate_swebench_images("sqlfluff__sqlfluff-2419")
        self.assertEqual(
            candidates[0],
            "swebench/sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419",
        )
        self.assertIn("swebench/sweb.eval.x86_64.sqlfluff_sqlfluff-2419", candidates)

    def test_default_image_uses_first_existing_candidate(self) -> None:
        existing = "swebench/sweb.eval.x86_64.sqlfluff_sqlfluff-2419"

        with patch("swebench_container.images.image_exists", side_effect=lambda image: image == existing):
            self.assertEqual(default_swebench_image("sqlfluff__sqlfluff-2419"), existing)


if __name__ == "__main__":
    unittest.main()
