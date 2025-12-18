from __future__ import annotations

import re
import unittest
from pathlib import Path


class WebBundleTests(unittest.TestCase):
    def test_web_main_loads_all_streamvis_modules(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        main_js = repo_root / "web" / "main.js"
        self.assertTrue(main_js.exists(), "web/main.js is missing")

        src = main_js.read_text(encoding="utf-8")
        m = re.search(r"const\s+streamvisFiles\s*=\s*\[(.*?)\];", src, re.DOTALL)
        self.assertIsNotNone(m, "streamvisFiles array not found in web/main.js")
        block = m.group(1) if m is not None else ""

        listed = set(re.findall(r"\"([^\"]+\.py)\"", block))
        self.assertTrue(listed, "No python files listed in streamvisFiles array")

        # Expected: all python sources under the streamvis/ package.
        pkg_files = sorted((repo_root / "streamvis").rglob("*.py"))
        expected = {"../" + p.relative_to(repo_root).as_posix() for p in pkg_files}

        missing = sorted(expected - listed)
        extra = sorted(listed - expected)
        self.assertEqual(missing, [], f"Missing python files in streamvisFiles: {missing}")
        self.assertEqual(extra, [], f"Unexpected python files in streamvisFiles: {extra}")

        # Sanity: ensure the listed files exist in the repo (normalize ../ prefix).
        for entry in listed:
            rel = entry[3:] if entry.startswith("../") else entry
            self.assertTrue((repo_root / rel).exists(), f"Listed file does not exist: {entry}")


if __name__ == "__main__":
    unittest.main()
