#!/usr/bin/env python3
"""Tests for region_check.py. Stdlib only.

Run from the repository root:

    python3 .github/profile-check/test_region_check.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import region_check  # noqa: E402  (imports after the helper's directory is on sys.path)

FIXTURES = HERE / "fixtures"
REPOSITORY = HERE.parent.parent
CHECKER = HERE / "region_check.py"

SOURCE_B = "0123456789abcdef0123456789abcdef01234567"

# Every structurally invalid head fixture and the fault it must produce.
HEAD_REJECTS = {
    "absent-identity.md": "absent",
    "absent-links.md": "absent",
    "duplicated-identity.md": "duplicated",
    "nested.md": "nested",
    "orphan-end.md": "malformed",
    "never-ends.md": "malformed",
    "source-missing.md": "malformed",
    "source-short.md": "malformed",
    "source-uppercase.md": "malformed",
    "source-mismatch.md": "malformed",
    "stray-mention.md": "malformed",
    "unknown-name.md": "malformed",
}


def fixture(*parts):
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def write(path, content):
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


class StructuralTests(unittest.TestCase):
    """Rules that run on every pull request."""

    def test_valid_fixture_passes(self):
        findings, parsed = region_check.check_readme(fixture("head", "valid.md"))
        self.assertEqual(findings, [])
        self.assertEqual(parsed[1]["identity"]["source"], SOURCE_B)
        self.assertEqual(len(parsed[1]), 2)

    def test_repository_readme_passes(self):
        text = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        findings, _ = region_check.check_readme(text)
        self.assertEqual(findings, [])

    def test_rejects_each_invalid_head_fixture(self):
        for name, fault in HEAD_REJECTS.items():
            with self.subTest(fixture=name):
                findings, parsed = region_check.check_readme(fixture("head", name))
                self.assertIsNone(parsed)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].fault, fault)
                self.assertTrue(findings[0].detail)

    def test_rejects_carriage_returns(self):
        text = fixture("head", "valid.md").replace("\n", "\r\n")
        findings, _ = region_check.check_readme(text)
        self.assertEqual([finding.fault for finding in findings], ["malformed"])


class PublisherTests(unittest.TestCase):
    """Rules that apply only to the publisher's automation branch."""

    def setUp(self):
        self.base = fixture("base.md")

    def test_accepts_region_and_stamp_update(self):
        head = fixture("publisher", "accept.md")
        self.assertEqual(region_check.check_publisher(self.base, head, ["README.md"]), [])

    def test_accepts_stamp_only_update(self):
        head = fixture("publisher", "stamps-only.md")
        self.assertEqual(region_check.check_publisher(self.base, head, ["README.md"]), [])

    def test_rejects_unexpected_file(self):
        head = fixture("publisher", "accept.md")
        findings = region_check.check_publisher(self.base, head, ["README.md", "notes.md"])
        self.assertEqual([finding.fault for finding in findings], ["out-of-bounds"])
        self.assertIn("notes.md", findings[0].detail)

    def test_rejects_readme_untouched_when_another_file_changes(self):
        head = fixture("publisher", "accept.md")
        findings = region_check.check_publisher(self.base, head, ["notes.md"])
        self.assertEqual([finding.fault for finding in findings], ["out-of-bounds"])

    def test_rejects_edit_outside_the_regions(self):
        head = fixture("publisher", "outside-edit.md")
        findings = region_check.check_publisher(self.base, head, ["README.md"])
        self.assertEqual([finding.fault for finding in findings], ["out-of-bounds"])

    def test_rejects_reordered_regions(self):
        head = fixture("publisher", "reordered.md")
        findings = region_check.check_publisher(self.base, head, ["README.md"])
        self.assertEqual([finding.fault for finding in findings], ["out-of-bounds"])

    def test_rejects_malformed_provenance_at_head(self):
        head = fixture("head", "source-mismatch.md")
        findings = region_check.check_publisher(self.base, head, ["README.md"])
        self.assertEqual([finding.fault for finding in findings], ["malformed"])

    def test_rejects_invalid_base(self):
        base = fixture("head", "absent-identity.md")
        head = fixture("publisher", "accept.md")
        findings = region_check.check_publisher(base, head, ["README.md"])
        self.assertEqual([finding.fault for finding in findings], ["malformed"])
        self.assertIn("base", findings[0].detail)


GIT_FLAGS = [
    "-c",
    "user.name=Region Check Tests",
    "-c",
    "user.email=tests@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return subprocess.run(
        ["git", *GIT_FLAGS, *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class CliTests(unittest.TestCase):
    """End-to-end runs of the helper against throwaway git repositories."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def build(self, base_text, head_text, branch, extra=None):
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo / "README.md", base_text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        base = git(self.repo, "rev-parse", "HEAD").strip()

        git(self.repo, "switch", "-q", "-c", branch)
        write(self.repo / "README.md", head_text)
        for path, text in (extra or {}).items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            write(target, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "head")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        return base, head

    def run_checker(self, *args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_accepts_publisher_region_update(self):
        base, head = self.build(
            fixture("base.md"), fixture("publisher", "accept.md"), "lane-hub/profile"
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "lane-hub/profile")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("publisher pull request: yes", result.stdout)
        self.assertIn("✓ regions check passed", result.stdout)

    def test_rejects_unexpected_publisher_file(self):
        base, head = self.build(
            fixture("base.md"),
            fixture("publisher", "accept.md"),
            "lane-hub/profile",
            extra={"notes.md": "scratch\n"},
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "lane-hub/profile")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("out-of-bounds", result.stdout)
        self.assertIn("notes.md", result.stdout)

    def test_rejects_publisher_edit_outside_the_regions(self):
        base, head = self.build(
            fixture("base.md"), fixture("publisher", "outside-edit.md"), "lane-hub/profile"
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "lane-hub/profile")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("out-of-bounds", result.stdout)

    def test_rejects_publisher_malformed_provenance(self):
        base, head = self.build(
            fixture("base.md"), fixture("head", "source-mismatch.md"), "lane-hub/profile"
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "lane-hub/profile")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("malformed", result.stdout)

    def test_owner_prose_pull_request_passes(self):
        base, head = self.build(
            fixture("base.md"), fixture("publisher", "outside-edit.md"), "topic/prose"
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "topic/prose")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("publisher pull request: no", result.stdout)
        self.assertIn("✓ regions check passed", result.stdout)

    def test_owner_malformed_markers_still_fail(self):
        base, head = self.build(
            fixture("base.md"), fixture("head", "duplicated-identity.md"), "topic/prose"
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "topic/prose")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("duplicated", result.stdout)

    def test_rejects_non_utf8_readme(self):
        base, head = self.build(
            fixture("base.md"),
            b"<!-- lane-hub:begin identity source=0123456789abcdef0123456789abcdef01234567 -->\n\xff\xfe\n",
            "lane-hub/profile",
        )
        result = self.run_checker("--base-ref", base, "--head-ref", head, "--branch", "lane-hub/profile")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not UTF-8", result.stdout)

    def test_unknown_head_commit_fails_closed(self):
        base, _ = self.build(
            fixture("base.md"), fixture("publisher", "accept.md"), "lane-hub/profile"
        )
        result = self.run_checker(
            "--base-ref", base, "--head-ref", "0" * 40, "--branch", "lane-hub/profile"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("malformed", result.stdout)

    def test_missing_arguments_is_misuse(self):
        result = self.run_checker()
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)