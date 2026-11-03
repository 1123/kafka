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

"""Grok Bot: a small PM / QA / DevOps front door for this Kafka fork.

Run from this directory:

    uvicorn app:app --host 127.0.0.1 --port 8000

Approvals and launch records are stored in a local SQLite file (GROK_DB_PATH,
default grok-bot/grok-bot.sqlite) and survive a restart. Slack requests are
rejected unless SLACK_SIGNING_SECRET is set. Launching an agent shells out to
scripts/cursor_api_trigger.py, which reads CURSOR_API_KEY from the environment.
The dashboard also lists cloud agents for the default repo with
GET /v1/agents, GET /v1/agents/{id}, and GET /v1/agents/{id}/runs/{runId}.
Neither secret is written to logs or API responses. There are no seeded rows.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from store import AgentRun, Store, default_db_path

ROOT = Path(__file__).resolve().parent.parent
TRIGGER = ROOT / "scripts" / "cursor_api_trigger.py"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

SLACK_MAX_SKEW_SECONDS = 5 * 60
MAX_BODY_BYTES = 1_000_000
MAX_PROMPT_CHARS = 20_000
CURSOR_LIST_TTL_SECONDS = 20.0


def _load_cursor_api():
    spec = importlib.util.spec_from_file_location("cursor_api_trigger", TRIGGER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {TRIGGER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cursor_api_trigger"] = module
    spec.loader.exec_module(module)
    return module


CURSOR_API = _load_cursor_api()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def redact_secrets(text: str) -> str:
    redacted = text or ""
    for name in ("CURSOR_API_KEY", "SLACK_SIGNING_SECRET"):
        secret = _env(name)
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def default_repo() -> str:
    return _env("GROK_DEFAULT_REPO", "https://github.com/1123/kafka")


def default_ref() -> str:
    return _env("GROK_DEFAULT_REF", "trunk")


def resolve_approver(supplied: str) -> str:
    name = (supplied or "").strip() or _env("GROK_APPROVER")
    if not name:
        raise HTTPException(
            status_code=400,
            detail="approver name is required (dashboard field or GROK_APPROVER)",
        )
    return name[:200]


def resolve_note(supplied: str, default: str) -> str:
    text = (supplied or "").strip()
    return (text or default)[:2000]


def resolve_requester(supplied: str) -> str:
    name = (supplied or "").strip() or _env("GROK_APPROVER") or "dashboard"
    return name[:200]


def load_remote_agents(repo: str, api_key: str) -> list[dict]:
    known = [run.agent_id for run in STORE.list_agents() if run.agent_id]
    return CURSOR_API.list_repo_agents(api_key, repo, known)


def merge_agent_views(local_runs: list[AgentRun], remote: list[dict]) -> list[dict]:
    """Overlay live Cursor status, branch, and pull request URL onto local launches."""
    remote_by_id: dict[str, dict] = {}
    for item in remote:
        agent_id = item.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            remote_by_id[agent_id] = item
    seen: set[str] = set()
    merged: list[dict] = []
    for run in local_runs:
        row = asdict(run)
        remote_item = remote_by_id.get(run.agent_id) if run.agent_id else None
        if remote_item and run.agent_id:
            seen.add(run.agent_id)
            if remote_item.get("status"):
                row["status"] = remote_item["status"]
            if remote_item.get("url"):
                row["url"] = remote_item["url"]
            row["branch"] = remote_item.get("branch")
            row["pr_url"] = remote_item.get("pr_url")
            row["run_status"] = remote_item.get("run_status")
            row["name"] = remote_item.get("name") or ""
            row["origin"] = "merged"
        else:
            row["branch"] = None
            row["pr_url"] = None
            row["run_status"] = None
            row["name"] = ""
            row["origin"] = "local"
        merged.append(row)
    for item in remote:
        agent_id = item.get("agent_id")
        if not isinstance(agent_id, str) or agent_id in seen:
            continue
        merged.append(
            {
                "id": agent_id,
                "approval_id": None,
                "repo": item.get("repo") or "",
                "ref": item.get("ref") or "",
                "prompt": item.get("name") or "",
                "status": item.get("status") or "",
                "agent_id": agent_id,
                "url": item.get("url"),
                "error": None,
                "created_at": item.get("created_at") or "",
                "branch": item.get("branch"),
                "pr_url": item.get("pr_url"),
                "run_status": item.get("run_status"),
                "name": item.get("name") or "",
                "origin": "cursor",
            }
        )
    merged.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return merged


class AgentDirectory:
    """Refresh the Cursor list in the background so /api/status stays local and fast."""

    def __init__(self, loader, ttl_seconds: float = CURSOR_LIST_TTL_SECONDS) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._fetched_at = 0.0
        self._refreshing = False
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def maybe_refresh(self, repo: str) -> None:
        if not _env("CURSOR_API_KEY"):
            return
        with self._lock:
            if self._refreshing:
                return
            if self._fetched_at and (time.monotonic() - self._fetched_at) < self._ttl:
                return
            self._refreshing = True
        threading.Thread(
            target=self._refresh_thread,
            args=(repo,),
            name="cursor-agent-refresh",
            daemon=True,
        ).start()

    def _refresh_thread(self, repo: str) -> None:
        try:
            self.refresh(repo)
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self, repo: str) -> None:
        api_key = _env("CURSOR_API_KEY")
        if not api_key:
            with self._lock:
                self._error = None
                self._fetched_at = time.monotonic()
            return
        try:
            agents = self._loader(repo, api_key)
            STORE.replace_cursor_cache(CURSOR_API.repo_key(repo), agents)
        except Exception as exc:
            with self._lock:
                self._error = redact_secrets(str(exc))[:500]
                self._fetched_at = time.monotonic()
            return
        with self._lock:
            self._error = None
            self._fetched_at = time.monotonic()


STORE = Store(default_db_path())
DIRECTORY = AgentDirectory(load_remote_agents)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    DIRECTORY.maybe_refresh(default_repo())
    yield


app = FastAPI(title="Grok Bot", version="1.0.0", lifespan=_lifespan)


class ApprovalIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    repo: str = ""
    ref: str = ""
    source: str = "dashboard"
    requester: str = Field(default="", max_length=200)


class DecisionIn(BaseModel):
    approver: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)


class AgentIn(BaseModel):
    approval_id: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=100)


def verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Slack v0 HMAC. https://api.slack.com/authentication/verifying-requests-from-slack"""
    if not timestamp or not signature or not secret:
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs(int(datetime.now(timezone.utc).timestamp()) - stamp) > SLACK_MAX_SKEW_SECONDS:
        return False
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)


def launch_via_script(
    repo: str,
    ref: str,
    prompt: str,
    name: str | None,
    approval_id: str | None,
    actor: str,
) -> AgentRun:
    agent_id = STORE.next_agent_id()
    command = [
        sys.executable,
        str(TRIGGER),
        "--repo",
        repo,
        "--ref",
        ref,
        "--prompt",
        prompt,
    ]
    if name:
        command.extend(["--name", name])

    def remember(status: str, remote_agent_id: str | None, url: str | None, error: str | None) -> AgentRun:
        run = AgentRun(
            id=agent_id,
            approval_id=approval_id,
            repo=repo,
            ref=ref,
            prompt=prompt,
            status=status,
            agent_id=remote_agent_id,
            url=url,
            error=redact_secrets(error)[:2000] if error else None,
            created_at=_now(),
        )
        return STORE.add_agent(run, actor)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return remember("error", None, None, str(exc))

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "cursor_api_trigger.py failed").strip()
        return remember("error", None, None, detail)

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return remember("error", None, None, "cursor_api_trigger.py returned a non-JSON body")

    remote_agent_id = None
    url = None
    agent = payload.get("agent") if isinstance(payload, dict) else None
    if isinstance(agent, dict):
        remote_agent_id = agent.get("id")
        url = agent.get("url")
    return remember("launched", remote_agent_id, url, None)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "default_repo": default_repo(),
            "default_ref": default_ref(),
        },
    )


@app.get("/api/status")
def status() -> dict:
    repo = default_repo()
    DIRECTORY.maybe_refresh(repo)
    key = CURSOR_API.repo_key(repo)
    return {
        "approvals": [asdict(item) for item in STORE.list_approvals()],
        "agents": merge_agent_views(STORE.list_agents(), STORE.list_cursor_cache(key)),
        "events": STORE.list_events(40),
        "defaults": {"repo": repo, "ref": default_ref()},
        "cursor_api_key_configured": bool(_env("CURSOR_API_KEY")),
        "slack_signing_configured": bool(_env("SLACK_SIGNING_SECRET")),
        "db_path": str(STORE.path),
        "cursor_agents_error": DIRECTORY.error,
        "cursor_agents_fetched_at": STORE.cursor_cache_fetched_at(key),
    }


@app.post("/api/approvals")
def create_approval(body: ApprovalIn) -> dict:
    approval = STORE.add_approval(
        body.title,
        body.prompt,
        body.repo.strip() or default_repo(),
        body.ref.strip() or default_ref(),
        (body.source or "dashboard").strip() or "dashboard",
        resolve_requester(body.requester),
    )
    return asdict(approval)


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, launch: bool = False, body: DecisionIn | None = None) -> dict:
    payload = body or DecisionIn()
    approver = resolve_approver(payload.approver)
    note = resolve_note(payload.note, "Approved from the dashboard.")
    try:
        approval = STORE.decide(approval_id, "approved", note, approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"approval is already {exc}") from None
    except PermissionError:
        raise HTTPException(
            status_code=400,
            detail="approver name is required (dashboard field or GROK_APPROVER)",
        ) from None
    result: dict = {"approval": asdict(approval), "launch": None}
    if launch:
        if not approval.prompt:
            result["launch"] = {"ok": False, "error": "approval has no prompt to launch"}
            return result
        run = launch_via_script(
            approval.repo,
            approval.ref,
            approval.prompt,
            approval.title[:100],
            approval.id,
            approver,
        )
        result["launch"] = {"ok": run.status == "launched", "agent": asdict(run)}
    return result


@app.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str, body: DecisionIn | None = None) -> dict:
    payload = body or DecisionIn()
    approver = resolve_approver(payload.approver)
    note = resolve_note(payload.note, "Rejected from the dashboard.")
    try:
        approval = STORE.decide(approval_id, "rejected", note, approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"approval is already {exc}") from None
    except PermissionError:
        raise HTTPException(
            status_code=400,
            detail="approver name is required (dashboard field or GROK_APPROVER)",
        ) from None
    return asdict(approval)


@app.post("/api/agents")
def trigger_agent(body: AgentIn) -> dict:
    approval = STORE.get(body.approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.status != "approved":
        raise HTTPException(status_code=409, detail="approval must be approved before launch")
    if not approval.prompt:
        raise HTTPException(status_code=409, detail="approval has no prompt to launch")
    name = (body.name or approval.title)[:100]
    actor = approval.approver or _env("GROK_APPROVER") or "dashboard"
    run = launch_via_script(approval.repo, approval.ref, approval.prompt, name, approval.id, actor)
    if run.status != "launched":
        raise HTTPException(status_code=502, detail={"agent": asdict(run)})
    return {"ok": True, "agent": asdict(run)}


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    """Slack Events API and slash-command webhook. Signature is required."""
    secret = _env("SLACK_SIGNING_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="SLACK_SIGNING_SECRET is not set")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    if not verify_slack_signature(body, timestamp, signature, secret):
        raise HTTPException(status_code=401, detail="invalid slack signature")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return _handle_slack_json(body)
    if content_type.startswith("application/x-www-form-urlencoded"):
        return _handle_slack_command(body)
    raise HTTPException(status_code=415, detail="unsupported content type")


def _handle_slack_json(body: bytes) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a json object")
    kind = payload.get("type")
    if kind == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(status_code=400, detail="missing challenge")
        return {"challenge": challenge}
    if kind == "event_callback":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        if event.get("type") == "app_mention":
            text = re.sub(r"<@[A-Z0-9]+>", "", str(event.get("text") or "")).strip()
            user_id = str(event.get("user") or "").strip()
            approval = STORE.add_approval(
                title="Slack mention",
                prompt=text[:MAX_PROMPT_CHARS],
                repo=default_repo(),
                ref=default_ref(),
                source="slack",
                requester=user_id or "slack",
            )
            return {"ok": True, "approval_id": approval.id}
        return {"ok": True}
    raise HTTPException(status_code=400, detail="unsupported slack event")


def _handle_slack_command(body: bytes) -> dict:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)

    def first(key: str) -> str:
        values = parsed.get(key) or [""]
        return values[0]

    command = first("command").strip() or "/grok"
    user_name = first("user_name").strip()
    user_id = first("user_id").strip()
    text = first("text").strip()[:MAX_PROMPT_CHARS]
    title = f"{command} from {user_name}".strip() if user_name else command
    approval = STORE.add_approval(
        title=title[:200],
        prompt=text,
        repo=default_repo(),
        ref=default_ref(),
        source="slack",
        requester=user_id or user_name or "slack",
    )
    return {
        "response_type": "ephemeral",
        "text": f"Queued {approval.id}. Approve it on the Grok Bot dashboard before an agent starts.",
    }
