#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Git range helpers shared by the pre-review and boundary-check scripts."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MARKER_RE = re.compile(r"boundary-allow:\s*([a-z0-9-]+)")
DIFF_HEADER_RE = re.compile(
    r"^diff --git (?:\"a/(.+)\"|a/(.+)) (?:\"b/(.+)\"|b/(.+))$"
)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class FileDiff:
    status: str
    path: str
    old_path: str | None = None
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    binary: bool = False

    def added_text(self) -> str:
        return "\n".join(line for _, line in self.added)


def run_git(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def rev_exists(rev: str) -> bool:
    probe = run_git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"])
    return probe.returncode == 0


def ensure_rev(rev: str) -> None:
    if rev_exists(rev):
        return
    remote_branch = rev.removeprefix("origin/")
    fetched = run_git(["fetch", "--no-tags", "origin", remote_branch])
    if fetched.returncode != 0 or not rev_exists(rev):
        detail = (fetched.stderr or fetched.stdout or "").strip()
        raise SystemExit(detail or f"cannot resolve {rev}")


def merge_base_with_trunk() -> str:
    for candidate in ("origin/trunk", "trunk"):
        if not rev_exists(candidate):
            ensure_rev(candidate if candidate.startswith("origin/") else "origin/trunk")
        if rev_exists(candidate):
            merged = run_git(["merge-base", "HEAD", candidate])
            if merged.returncode == 0 and merged.stdout.strip():
                return merged.stdout.strip()
    raise SystemExit("cannot find a merge-base with origin/trunk or trunk")


def resolve_range(base_arg: str | None, head_arg: str | None) -> tuple[str, str, bool]:
    """Return (base, head, include_worktree).

    Local runs diff the worktree, including uncommitted edits, against the
    merge-base with trunk. CI passes EVENT_NAME plus BASE_SHA / HEAD_SHA or
    BASE_REF, matching .github/workflows/agent-guardrails.yml.
    """
    if base_arg or head_arg:
        base = base_arg or merge_base_with_trunk()
        ensure_rev(base)
        if head_arg in (None, "", "WORKTREE"):
            return base, "HEAD", True
        ensure_rev(head_arg)
        return base, head_arg, False

    event = os.environ.get("EVENT_NAME", "")
    base_sha = os.environ.get("BASE_SHA", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    if event == "pull_request" and base_sha and head_sha:
        ensure_rev(base_sha)
        ensure_rev(head_sha)
        return base_sha, head_sha, False
    if event == "pull_request":
        return "HEAD^1", "HEAD", False
    if event:
        ref = os.environ.get("BASE_REF") or "trunk"
        ensure_rev(f"origin/{ref}")
        return f"origin/{ref}", "HEAD", False
    return merge_base_with_trunk(), "HEAD", True


def parse_markers(text: str) -> set[str]:
    return set(MARKER_RE.findall(text))


def _parse_name_status(text: str) -> dict[str, tuple[str, str | None]]:
    found: dict[str, tuple[str, str | None]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            found[parts[2]] = (status[0], parts[1])
        elif len(parts) >= 2:
            found[parts[1]] = (status[0], None)
    return found


def _parse_unified(text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    new_line = 0
    old_line = 0
    for line in text.splitlines():
        header = DIFF_HEADER_RE.match(line)
        if header:
            new_path = header.group(3) or header.group(4)
            old_path = header.group(1) or header.group(2)
            current = FileDiff(status="M", path=new_path, old_path=old_path if old_path != new_path else None)
            files[new_path] = current
            continue
        if current is None:
            continue
        if line.startswith("Binary files "):
            current.binary = True
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("index ") or line.startswith("new file") or line.startswith("deleted file") or line.startswith("similarity ") or line.startswith("rename "):
            continue
        hunk = HUNK_RE.match(line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue
        if line.startswith("+"):
            current.added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            current.removed.append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("\\"):
            continue
    return files


def _read_untracked(path: str) -> FileDiff:
    file_path = ROOT / path
    diff = FileDiff(status="A", path=path)
    if not file_path.is_file():
        return diff
    data = file_path.read_bytes()
    if b"\0" in data[:8192]:
        diff.binary = True
        return diff
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        diff.added.append((number, line))
    return diff


def load_files(base: str, head: str, include_worktree: bool) -> list[FileDiff]:
    if include_worktree:
        name_status = run_git(["diff", "--name-status", "--find-renames", base])
        unified = run_git(["diff", "--unified=0", "--find-renames", base])
    else:
        name_status = run_git(["diff", "--name-status", "--find-renames", base, head])
        unified = run_git(["diff", "--unified=0", "--find-renames", base, head])
    if name_status.returncode != 0:
        raise SystemExit(name_status.stderr.strip() or "git diff --name-status failed")
    if unified.returncode != 0:
        raise SystemExit(unified.stderr.strip() or "git diff failed")

    statuses = _parse_name_status(name_status.stdout)
    parsed = _parse_unified(unified.stdout)
    ordered: list[FileDiff] = []
    seen: set[str] = set()
    for path, (status, old_path) in statuses.items():
        diff = parsed.get(path, FileDiff(status=status, path=path, old_path=old_path))
        diff.status = status
        if old_path:
            diff.old_path = old_path
        ordered.append(diff)
        seen.add(path)
    for path, diff in parsed.items():
        if path not in seen:
            ordered.append(diff)

    if include_worktree:
        untracked = run_git(["ls-files", "--others", "--exclude-standard"])
        if untracked.returncode != 0:
            raise SystemExit(untracked.stderr.strip() or "git ls-files failed")
        for path in untracked.stdout.splitlines():
            if path and path not in seen:
                ordered.append(_read_untracked(path))
    return ordered


def commit_message_text(base: str, head: str) -> str:
    log = run_git(["log", "--format=%B%x1e", f"{base}..{head}"])
    if log.returncode != 0:
        return ""
    return log.stdout


def read_blob(head: str, path: str, include_worktree: bool) -> str | None:
    if include_worktree:
        file_path = ROOT / path
        if not file_path.is_file():
            return None
        data = file_path.read_bytes()
        if b"\0" in data[:8192]:
            return None
        return data.decode("utf-8", errors="replace")
    shown = run_git(["show", f"{head}:{path}"])
    if shown.returncode != 0:
        return None
    if "\0" in shown.stdout[:8192]:
        return None
    return shown.stdout


def short_rev(rev: str) -> str:
    shown = run_git(["rev-parse", "--short", rev])
    if shown.returncode != 0:
        return rev
    return shown.stdout.strip()
