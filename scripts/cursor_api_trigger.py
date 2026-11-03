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

"""Launch and list Cursor cloud agents from the public Cloud Agents API.

Verified against the public docs (the v0 surface is legacy and is not called
here):

  https://cursor.com/docs/cloud-agent/api/endpoints

  POST https://api.cursor.com/v1/agents
  GET  https://api.cursor.com/v1/agents
  GET  https://api.cursor.com/v1/agents/{id}
  GET  https://api.cursor.com/v1/agents/{id}/runs/{runId}
  Auth: HTTP Basic, API key as the username and an empty password
        (``Authorization: Bearer`` is also accepted; this script uses Basic,
        matching the curl example ``-u YOUR_API_KEY:``).
  Create body: {"prompt": {"text": "..."},
                "repos": [{"url": "https://...", "startingRef": "<branch or sha>"}],
                "autoCreatePR": false,
                "workOnCurrentBranch": false}

List responses carry identity fields only (id, name, status, url, latestRunId).
Repository, branch, and pull request URL come from the agent and its latest run.

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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API_ORIGIN = "https://api.cursor.com"
API_URL = API_ORIGIN + "/v1/agents"
_OWNER_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_LIST_LIMIT = 100


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


def redact(text: str, secret: str | None) -> str:
    """Drop a secret from text that might be stored or returned."""
    if not text or not secret:
        return text
    return text.replace(secret, "[redacted]")


def repo_key(value: str) -> str:
    """Compare repository URLs that differ by scheme, www, or a trailing .git."""
    text = (value or "").strip().lower().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith("www."):
        text = text[4:]
    return text


def _basic_auth(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def fetch_json(
    method: str,
    path: str,
    api_key: str,
    query: dict | None = None,
    body: dict | None = None,
    timeout: float = 20.0,
) -> dict:
    """Call the Cloud Agents API. The key is sent as Basic auth and never placed in the URL."""
    if not api_key:
        raise CursorApiError("CURSOR_API_KEY is not set")
    if not path.startswith("/"):
        raise CursorApiError("path must start with /")
    url = API_ORIGIN + path
    if query:
        cleaned = {key: value for key, value in query.items() if value is not None}
        if cleaned:
            url = url + "?" + urllib.parse.urlencode(cleaned)
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", _basic_auth(api_key))
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "kafka-fork-grok-bot/1.0")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:4000]
        message = redact(f"Cursor API returned HTTP {exc.code}: {detail}", api_key)
        raise CursorApiError(message, status=exc.code) from exc
    except urllib.error.URLError as exc:
        reason = redact(str(exc.reason), api_key)
        raise CursorApiError(f"Cursor API request failed: {reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CursorApiError("Cursor API returned a body that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise CursorApiError("Cursor API returned a JSON value that is not an object")
    return parsed


def launch_agent(payload: dict, api_key: str, timeout: float = 60.0) -> dict:
    return fetch_json("POST", "/v1/agents", api_key, body=payload, timeout=timeout)


def _unwrap_agent(payload: dict) -> dict:
    wrapped = payload.get("agent")
    if isinstance(wrapped, dict) and "id" not in payload:
        return wrapped
    return payload


def _matching_repo(agent: dict, wanted: str) -> dict | None:
    repos = agent.get("repos")
    if not isinstance(repos, list):
        return None
    for repo in repos:
        if isinstance(repo, dict) and repo_key(str(repo.get("url") or "")) == wanted:
            return repo
    return None


def _branch_for_repo(run: dict | None, wanted: str) -> tuple[str | None, str | None, str | None]:
    """Return run status, pushed branch, and pull request URL for the repository."""
    if not isinstance(run, dict):
        return None, None, None
    status = run.get("status") if isinstance(run.get("status"), str) else None
    git = run.get("git") if isinstance(run.get("git"), dict) else {}
    branches = git.get("branches") if isinstance(git.get("branches"), list) else []
    chosen = None
    for entry in branches:
        if isinstance(entry, dict) and repo_key(str(entry.get("repoUrl") or "")) == wanted:
            chosen = entry
            break
    if chosen is None:
        for entry in branches:
            if isinstance(entry, dict):
                chosen = entry
                break
    if chosen is None:
        return status, None, None
    branch = chosen.get("branch") if isinstance(chosen.get("branch"), str) else None
    pr_url = chosen.get("prUrl") if isinstance(chosen.get("prUrl"), str) else None
    return status, branch, pr_url


def summarize_agent(agent: dict, run: dict | None, wanted: str) -> dict:
    matched = _matching_repo(agent, wanted)
    first = matched
    if first is None:
        repos = agent.get("repos") if isinstance(agent.get("repos"), list) else []
        first = repos[0] if repos and isinstance(repos[0], dict) else {}
    run_status, branch, pr_url = _branch_for_repo(run, wanted)
    return {
        "agent_id": agent.get("id"),
        "name": agent.get("name") if isinstance(agent.get("name"), str) else "",
        "status": agent.get("status") if isinstance(agent.get("status"), str) else "",
        "url": agent.get("url") if isinstance(agent.get("url"), str) else None,
        "repo": first.get("url") if isinstance(first.get("url"), str) else "",
        "ref": first.get("startingRef") if isinstance(first.get("startingRef"), str) else "",
        "branch": branch,
        "pr_url": pr_url,
        "run_status": run_status,
        "created_at": agent.get("createdAt") if isinstance(agent.get("createdAt"), str) else "",
        "latest_run_id": agent.get("latestRunId") if isinstance(agent.get("latestRunId"), str) else None,
    }


def list_repo_agents(
    api_key: str,
    repo: str,
    known_agent_ids: list[str] | None = None,
    *,
    limit: int = _LIST_LIMIT,
    fetch=None,
    workers: int = 8,
) -> list[dict]:
    """Agents on ``repo``, newest first.

    ``GET /v1/agents`` does not include repository, branch, or pull request URL.
    Each id is loaded with ``GET /v1/agents/{id}`` and, when present, its
    ``latestRunId`` via ``GET /v1/agents/{id}/runs/{runId}``. Agents recorded
    locally are fetched even when they are older than the list page.
    """
    if not api_key:
        raise CursorApiError("CURSOR_API_KEY is not set")
    transport = fetch or fetch_json
    wanted = repo_key(repo)
    page_limit = min(max(int(limit), 1), _LIST_LIMIT)
    page = transport("GET", "/v1/agents", api_key, {"limit": str(page_limit)})
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise CursorApiError("Cursor API list agents returned an unexpected body")

    ordered: list[str] = []
    seen: set[str] = set()

    def add_id(agent_id: object) -> None:
        if isinstance(agent_id, str) and agent_id and agent_id not in seen:
            seen.add(agent_id)
            ordered.append(agent_id)

    for item in items:
        if isinstance(item, dict):
            add_id(item.get("id"))
    for agent_id in known_agent_ids or []:
        add_id(agent_id)

    def load_detail(agent_id: str) -> dict | CursorApiError | None:
        try:
            payload = transport("GET", "/v1/agents/" + urllib.parse.quote(agent_id, safe=""), api_key)
        except CursorApiError as exc:
            return exc
        if not isinstance(payload, dict):
            return CursorApiError("Cursor API agent detail was not an object")
        agent = _unwrap_agent(payload)
        if not isinstance(agent.get("id"), str):
            agent = dict(agent)
            agent["id"] = agent_id
        if _matching_repo(agent, wanted) is None:
            return None
        return agent

    worker_count = 1 if workers <= 1 or len(ordered) <= 1 else min(workers, len(ordered))
    if worker_count == 1:
        details = [load_detail(agent_id) for agent_id in ordered]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            details = list(pool.map(load_detail, ordered))

    errors = [item for item in details if isinstance(item, CursorApiError)]
    matched = [agent for agent in details if isinstance(agent, dict)]
    if ordered and not matched and len(errors) == len(ordered):
        failure = errors[0]
        raise CursorApiError(redact(str(failure), api_key), status=failure.status)

    def load_run(agent: dict) -> tuple[dict, dict | None]:
        run_id = agent.get("latestRunId")
        agent_id = agent.get("id")
        if not isinstance(run_id, str) or not run_id or not isinstance(agent_id, str):
            return agent, None
        path = "/v1/agents/" + urllib.parse.quote(agent_id, safe="") + "/runs/" + urllib.parse.quote(run_id, safe="")
        try:
            payload = transport("GET", path, api_key)
        except CursorApiError:
            return agent, None
        return agent, payload if isinstance(payload, dict) else None

    if worker_count == 1 or len(matched) <= 1:
        pairs = [load_run(agent) for agent in matched]
    else:
        with ThreadPoolExecutor(max_workers=min(worker_count, len(matched))) as pool:
            pairs = list(pool.map(load_run, matched))
    return [summarize_agent(agent, run, wanted) for agent, run in pairs]


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
