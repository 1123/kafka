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

"""Launch a Cursor cloud agent from the public Cloud Agents API.

Verified 2026-10-25 against the public docs (the v0 surface is legacy and is
not called here):

  https://cursor.com/docs/cloud-agent/api/endpoints
  https://cursor.com/docs/cloud-agent/api/overview
  https://cursor.com/docs/background-agent/api/overview  (same v1 overview)

  POST https://api.cursor.com/v1/agents
  Auth: HTTP Basic, API key as the username and an empty password
        (``Authorization: Bearer`` is also accepted; this script uses Basic,
        matching the curl example ``-u YOUR_API_KEY:``).
  Body: {"prompt": {"text": "..."},
         "repos": [{"url": "https://...", "startingRef": "<branch or sha>"}],
         "autoCreatePR": false,
         "workOnCurrentBranch": false}

The API is in public beta and may change. The key is read from the
CURSOR_API_KEY environment variable and is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_URL = "https://api.cursor.com/v1/agents"
_OWNER_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


class CursorApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalize_repo(repo: str) -> str:
    """Accept an https URL or a GitHub ``owner/name`` slug."""
    value = repo.strip()
    if value.endswith(".git"):
        value = value[: -len(".git")]
    if value.startswith("https://") or value.startswith("http://"):
        return value
    if _OWNER_REPO.fullmatch(value):
        return f"https://github.com/{value}"
    raise CursorApiError("repo must be an http(s) URL or a GitHub owner/name slug")


def build_payload(
    repo: str,
    ref: str,
    prompt: str,
    name: str | None = None,
    auto_create_pr: bool = False,
    work_on_current_branch: bool = False,
) -> dict:
    if not prompt.strip():
        raise CursorApiError("prompt must not be empty")
    if not ref.strip():
        raise CursorApiError("ref must not be empty")
    if name is not None and len(name) > 100:
        raise CursorApiError("name must be at most 100 characters")
    payload: dict = {
        "prompt": {"text": prompt},
        "repos": [{"url": normalize_repo(repo), "startingRef": ref}],
        # Sent explicitly so a future default cannot open a pull request or
        # push onto the starting ref. Both fields are optional in the docs.
        "autoCreatePR": auto_create_pr,
        "workOnCurrentBranch": work_on_current_branch,
    }
    if name:
        payload["name"] = name
    return payload


def launch_agent(payload: dict, api_key: str, timeout: float = 60.0) -> dict:
    if not api_key:
        raise CursorApiError("CURSOR_API_KEY is not set")
    body = json.dumps(payload).encode("utf-8")
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(API_URL, data=body, method="POST")
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "kafka-fork-grok-bot/1.0")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:4000]
        raise CursorApiError(f"Cursor API returned HTTP {exc.code}: {detail}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise CursorApiError(f"Cursor API request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CursorApiError("Cursor API returned a body that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise CursorApiError("Cursor API returned a JSON value that is not an object")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a Cursor cloud agent (POST /v1/agents) for a repository."
    )
    parser.add_argument("--repo", required=True, help="https URL or GitHub owner/name")
    parser.add_argument("--ref", required=True, help="Branch name or commit SHA (repos[].startingRef)")
    parser.add_argument("--prompt", required=True, help="Instruction text sent as prompt.text")
    parser.add_argument("--name", help="Optional display name, at most 100 characters")
    parser.add_argument(
        "--auto-create-pr",
        action="store_true",
        help="Set autoCreatePR. Off by default so this script does not open a pull request.",
    )
    parser.add_argument(
        "--work-on-current-branch",
        action="store_true",
        help="Push onto --ref. Off by default; Cursor otherwise uses a new cursor/ branch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON body and do not call the API. Does not need CURSOR_API_KEY.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = build_payload(
            repo=args.repo,
            ref=args.ref,
            prompt=args.prompt,
            name=args.name,
            auto_create_pr=args.auto_create_pr,
            work_on_current_branch=args.work_on_current_branch,
        )
        if args.dry_run:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
            print("dry-run: no request sent", file=sys.stderr)
            return 0
        result = launch_agent(payload, os.environ.get("CURSOR_API_KEY", "").strip())
    except CursorApiError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if exc.status is None else 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    if agent.get("id") or agent.get("url"):
        print(f"agent {agent.get('id', '')} {agent.get('url', '')}".rstrip(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
