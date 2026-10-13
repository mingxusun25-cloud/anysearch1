"""anysearch1 的单元测试与 CLI 冒烟测试（仅依赖标准库 unittest）。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anysearch  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "anysearch.py"


class PatternTests(unittest.TestCase):
    def test_literal_pattern_escapes_metacharacters(self):
        pattern = anysearch.build_pattern("a.b(")
        self.assertIsNotNone(pattern.search("a.b("))
        self.assertIsNone(pattern.search("axb("))

    def test_ignore_case(self):
        pattern = anysearch.build_pattern("hello", ignore_case=True)
        self.assertIsNotNone(pattern.search("HELLO world"))

    def test_regex_mode(self):
        pattern = anysearch.build_pattern(r"\d{3}", regex=True)
        self.assertIsNotNone(pattern.search("code 404"))
        self.assertIsNone(pattern.search("code 40"))

    def test_invalid_regex_raises_value_error(self):
        with self.assertRaises(ValueError):
            anysearch.build_pattern("(unclosed", regex=True)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "node_modules").mkdir()
        (self.root / "src" / "app.py").write_text(
            "import os\n\n\ndef main():\n    print('hello')\n    return 0\n",
            encoding="utf-8",
        )
        (self.root / "src" / "notes.md").write_text("HELLO notes\nnothing here\n", encoding="utf-8")
        (self.root / "node_modules" / "dep.js").write_text("hello from dep\n", encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"hello\x00binary")

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_matches_and_skips_default_excludes(self):
        report = anysearch.search("hello", self.root)
        paths = {m.path for m in report.matches}
        self.assertEqual(paths, {str(Path("src") / "app.py")})
        self.assertNotIn("node_modules", " ".join(paths))

    def test_binary_file_is_skipped(self):
        report = anysearch.search("hello", self.root, excludes=[])
        self.assertEqual(report.files_skipped, 1)
        self.assertNotIn("blob.bin", {m.path for m in report.matches})

    def test_ignore_case_matches_markdown(self):
        report = anysearch.search("hello", self.root, ignore_case=True)
        self.assertEqual({m.path for m in report.matches}, {
            str(Path("src") / "app.py"),
            str(Path("src") / "notes.md"),
        })

    def test_include_glob_filters_files(self):
        report = anysearch.search("hello", self.root, includes=["*.md"], ignore_case=True)
        self.assertEqual([m.path for m in report.matches], [str(Path("src") / "notes.md")])

    def test_context_lines(self):
        report = anysearch.search("print", self.root, context=2)
        self.assertEqual(len(report.matches), 1)
        match = report.matches[0]
        self.assertEqual(match.line, 5)
        self.assertEqual([line.strip() for line in match.before], ["", "def main():"])
        self.assertEqual(match.after, ["    return 0"])

    def test_limit_truncates(self):
        report = anysearch.search("e", self.root, limit=2)
        self.assertTrue(report.truncated)
        self.assertEqual(len(report.matches), 2)

    def test_line_numbers_are_one_based(self):
        report = anysearch.search("import os", self.root)
        self.assertEqual(report.matches[0].line, 1)

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            anysearch.search("x", self.root / "nope")

    def test_stats_and_report_dict(self):
        report = anysearch.search("hello", self.root)
        payload = report.to_dict()
        self.assertEqual(payload["stats"]["matched_files"], 1)
        self.assertEqual(payload["stats"]["files_scanned"], 2)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exit_code_zero_when_found(self):
        proc = self.run_cli("alpha", str(self.root))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("a.txt:1: alpha", proc.stdout)

    def test_exit_code_one_when_not_found(self):
        proc = self.run_cli("zzz-not-there", str(self.root))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.strip(), "")

    def test_json_output_is_parseable(self):
        proc = self.run_cli("beta", str(self.root), "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["stats"]["matches"], 1)
        self.assertEqual(payload["matches"][0]["line"], 2)

    def test_bad_path_exits_two(self):
        proc = self.run_cli("x", str(self.root / "missing"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("anysearch:", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
