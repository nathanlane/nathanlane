#!/usr/bin/env python3
"""Target-owned validation for automated GitHub-profile updates (issue-128).

``README.md`` of ``nathanlane/nathanlane`` carries two Lane Hub-managed regions
wrapped in ``<!-- lane-hub:begin <name> source=<commit> -->`` and
``<!-- lane-hub:end <name> -->`` marker pairs. Lane Hub owns the region
content; its publisher (#129) replaces both regions and stamps the same new
source revision on both begin markers, touching nothing else.

Every pull request to ``main`` must leave exactly one ``identity`` region and
one ``links`` region with well-formed, unnested markers, and both begin markers
must record the same full 40-character source revision. Pull requests from the
publisher's automation branch must additionally change only ``README.md`` and
only the two managed regions and their begin markers.

This script only reads: it parses the README as text and reads git objects. It
never executes or evaluates repository content.

Usage:
  region_check.py --base-ref <commit> --head-ref <commit> --branch <branch>
                  [--publisher-branch lane-hub/profile] [--readme README.md]
                  [--repo .]

Exit status: 0 the pull request passes, 1 it fails, 2 the arguments are unusable.
"""

import argparse
import re
import subprocess
import sys

# The marker grammar mirrors Lane Hub scripts/canonical/regions.mjs (ADR 0004,
# ADR 0011). It is re-implemented here: the consumer owns this check and does
# not depend on Lane Hub code. A marker is one whole line; any line mentioning
# the marker namespace must be an exact marker.
BEGIN = re.compile(r"^<!-- lane-hub:begin ([a-z]+)(?: source=([0-9a-f]{40}))? -->$")
END = re.compile(r"^<!-- lane-hub:end ([a-z]+) -->$")
MENTION = re.compile(r"lane-hub:", re.IGNORECASE)
REGIONS = ("identity", "links")

DEFAULT_PUBLISHER_BRANCH = "lane-hub/profile"
DEFAULT_README = "README.md"


class Finding:
    """One reason a pull request fails, with the fault class and its detail."""

    def __init__(self, fault, detail):
        self.fault = fault
        self.detail = detail

    def __str__(self):
        return f"{self.fault}: {self.detail}"


class RegionFault(Exception):
    """A README whose regions cannot be read exactly."""

    def __init__(self, fault, detail):
        super().__init__(detail)
        self.fault = fault
        self.detail = detail


class GitError(Exception):
    """A git command that could not answer the question this check asks."""


def parse_readme(text):
    """Split ``text`` into lines and its regions, or raise RegionFault.

    Returns ``(lines, regions)`` where ``regions`` maps a region name to
    ``{"name", "source", "begin", "end"}`` with line indices. Mirrors Lane Hub:
    absent, duplicated, nested and malformed markers are faults, and every
    begin marker must record a source revision.
    """
    if "\r" in text:
        raise RegionFault(
            "malformed",
            "the file contains a carriage return; markers are LF-terminated lines",
        )

    lines = text.split("\n")
    regions = {}
    open_region = None

    for index, line in enumerate(lines):
        if not MENTION.search(line):
            continue

        at = f"line {index + 1}"
        begin = BEGIN.match(line)
        end = END.match(line)
        marker = begin or end
        if not marker:
            raise RegionFault("malformed", f"{at} is not an exact marker: {line!r}")

        name = marker.group(1)
        if name not in REGIONS:
            raise RegionFault(
                "malformed",
                f"{at} names {name}, which is not a region ({', '.join(REGIONS)})",
            )

        if begin:
            if open_region is not None:
                raise RegionFault(
                    "nested",
                    f"{at} begins {name} inside {open_region['name']}, "
                    f"open since line {open_region['begin'] + 1}",
                )
            if name in regions:
                raise RegionFault("duplicated", f"{at} begins {name} a second time")
            if begin.group(2) is None:
                raise RegionFault("malformed", f"{at} records no source revision")
            open_region = {"name": name, "source": begin.group(2), "begin": index}
            continue

        if open_region is None:
            raise RegionFault("malformed", f"{at} ends {name}, which is not open")
        if open_region["name"] != name:
            raise RegionFault(
                "malformed",
                f"{at} ends {name} while {open_region['name']} is open",
            )
        open_region["end"] = index
        regions[name] = open_region
        open_region = None

    if open_region is not None:
        raise RegionFault(
            "malformed",
            f"{open_region['name']}, begun at line {open_region['begin'] + 1}, never ends",
        )
    for name in REGIONS:
        if name not in regions:
            raise RegionFault("absent", f"there is no {name} region")

    return lines, regions


def check_readme(text):
    """Every-pull-request rules: well-formed regions and one shared revision.

    Returns ``(findings, parsed)``; ``parsed`` is ``None`` when a finding exists.
    """
    try:
        lines, regions = parse_readme(text)
    except RegionFault as fault:
        return [Finding(fault.fault, fault.detail)], None

    revisions = {region["source"] for region in regions.values()}
    if len(revisions) != 1:
        return [
            Finding(
                "malformed",
                f"identity records source {regions['identity']['source']} while "
                f"links records source {regions['links']['source']}; both begin "
                "markers must name the same Lane Hub commit",
            )
        ], None
    return [], (lines, regions)


def skeleton(lines, regions):
    """The lines that a publisher update may not touch.

    Region contents are dropped and begin markers are reduced to their name, so
    that a region replacement and a new source stamp on either begin marker
    leave the result unchanged while anything else outside the two regions does
    not.
    """
    inside = set()
    for region in regions.values():
        inside.update(range(region["begin"] + 1, region["end"]))

    kept = []
    for index, line in enumerate(lines):
        if index in inside:
            continue
        begin = BEGIN.match(line)
        if begin:
            line = f"<!-- lane-hub:begin {begin.group(1)} -->"
        kept.append(line)
    return kept


def difference(before, after):
    """Describe the first line that differs between two line lists."""
    for index, (left, right) in enumerate(zip(before, after)):
        if left != right:
            return f"the first changed line outside them is {left!r} -> {right!r}"
    if len(before) != len(after):
        return f"the patch adds or removes {abs(len(after) - len(before))} line(s) outside them"
    return "the lines outside them differ"


def check_publisher(base_text, head_text, changed, readme_name=DEFAULT_README):
    """Publisher-only rules: README.md alone, and only its two managed regions.

    ``changed`` is the list of files the pull request changes relative to its
    merge base.
    """
    if base_text is None:
        return [
            Finding(
                "malformed",
                f"the base commit has no {readme_name}; cannot judge a publisher patch",
            )
        ]

    base_findings, base = check_readme(base_text)
    if base_findings:
        first = base_findings[0]
        return [
            Finding(
                "malformed",
                f"the base {readme_name} is already invalid ({first}); "
                "refusing to judge the publisher patch",
            )
        ]

    disallowed = sorted({path for path in changed if path != readme_name})
    if disallowed:
        return [
            Finding(
                "out-of-bounds",
                f"a publisher update may change only {readme_name}; this patch "
                f"also changes: {', '.join(disallowed)}",
            )
        ]

    head_findings, head = check_readme(head_text)
    if head_findings:
        return head_findings

    before = skeleton(base[0], base[1])
    after = skeleton(head[0], head[1])
    if before != after:
        return [
            Finding(
                "out-of-bounds",
                "a publisher update may change only the two managed regions and "
                "their begin markers; " + difference(before, after),
            )
        ]
    return []


def git(repo, *args):
    """Run a read-only git command in ``repo`` and return its stdout."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def require_commit(repo, ref):
    """Fail closed when ``ref`` is not a commit this checkout holds."""
    result = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise GitError(f"{ref} is not a commit in this checkout")


def resolve_merge_base(repo, base_ref, head_ref):
    output = git(repo, "merge-base", base_ref, head_ref).strip()
    if not output:
        raise GitError(f"{base_ref} and {head_ref} share no merge base")
    return output.splitlines()[0]


def changed_paths(repo, merge_base, head_ref):
    output = git(
        repo, "diff", "--name-only", "--no-renames", "--no-ext-diff", merge_base, head_ref
    )
    return [line for line in output.splitlines() if line]


def file_at(repo, ref, path):
    """The UTF-8 text of ``path`` at ``ref``, or None when absent."""
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:{path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise GitError(f"{path} at {ref} is not UTF-8 text")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="region_check.py",
        description="Check the Lane Hub regions of a profile pull request.",
    )
    parser.add_argument("--base-ref", required=True, help="base commit of the pull request")
    parser.add_argument("--head-ref", required=True, help="head commit of the pull request")
    parser.add_argument("--branch", required=True, help="head branch of the pull request")
    parser.add_argument(
        "--publisher-branch",
        default=DEFAULT_PUBLISHER_BRANCH,
        help="the publisher's stable automation branch",
    )
    parser.add_argument("--readme", default=DEFAULT_README, help="README path in the repository")
    parser.add_argument("--repo", default=".", help="repository to read from")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    publisher = args.branch == args.publisher_branch

    print(f"regions check: {args.readme}")
    print(f"  base {args.base_ref}")
    print(f"  head {args.head_ref} (branch {args.branch!r})")
    print(f"  publisher pull request: {'yes' if publisher else 'no'}")

    try:
        require_commit(args.repo, args.base_ref)
        require_commit(args.repo, args.head_ref)
        merge_base = resolve_merge_base(args.repo, args.base_ref, args.head_ref)
    except GitError as error:
        print(f"✗ malformed: {error}")
        return 1

    paths = []
    try:
        if publisher:
            paths = changed_paths(args.repo, merge_base, args.head_ref)
        head_text = file_at(args.repo, args.head_ref, args.readme)
    except GitError as error:
        print(f"✗ malformed: {error}")
        return 1

    if publisher:
        print(f"  changed files: {', '.join(paths) if paths else '(none)'}")

    if head_text is None:
        findings = [
            Finding("absent", f"{args.readme} is absent from the head commit {args.head_ref}")
        ]
        parsed = None
    else:
        findings, parsed = check_readme(head_text)

    if not findings and publisher:
        try:
            base_text = file_at(args.repo, merge_base, args.readme)
        except GitError as error:
            print(f"✗ malformed: {error}")
            return 1
        findings = check_publisher(base_text, head_text, paths, args.readme)

    for finding in findings:
        print(f"✗ {finding}")

    if findings:
        print(f"✗ regions check failed with {len(findings)} finding(s)")
        return 1

    print(f"✓ one identity and one links region, both stamped {parsed[1]['identity']['source']}")
    if publisher:
        print("✓ the patch changes only README.md, only the two managed regions and their begin markers")
    print("✓ regions check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())