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

State is in memory and is gone when the process exits. Slack requests are
rejected unless SLACK_SIGNING_SECRET is set. Launching an agent shells out to
scripts/cursor_api_trigger.py, which reads CURSOR_API_KEY from the environment.
Neither secret is written to logs or API responses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
TRIGGER = ROOT / "scripts" / "cursor_api_trigger.py"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

SLACK_MAX_SKEW_SECONDS = 5 * 60
MAX_BODY_BYTES = 1_000_000
MAX_PROMPT_CHARS = 20_000

app = FastAPI(title="Grok Bot", version="1.0.0")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def default_repo() -> str:
    return _env("GROK_DEFAULT_REPO", "https://github.com/1123/kafka")


def default_ref() -> str:
    return _env("GROK_DEFAULT_REF", "trunk")


@dataclass
class Approval:
    id: str
    title: str
    prompt: str
    repo: str
    ref: str
    source: str
    status: str
    note: str
    mock: bool
    created_at: str
    decided_at: str | None = None


@dataclass
class AgentRun:
    id: str
    approval_id: str | None
    repo: str
    ref: str
    prompt: str
    status: str
    agent_id: str | None
    url: str | None
    error: str | None
    mock: bool
    created_at: str


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_approval = 1003
        self._next_agent = 1002
        self.approvals: dict[str, Approval] = {}
        self.agents: dict[str, AgentRun] = {}
        self._seed()

    def _seed(self) -> None:
        self.approvals["apr-1001"] = Approval(
            id="apr-1001",
            title="Share-group lag metric",
            prompt="Sketch a broker metric for share-group lag. Do not change public APIs.",
            repo=default_repo(),
            ref=default_ref(),
            source="mock",
            status="pending",
            note="Seeded pending row. Nothing has been sent to Slack or Cursor.",
            mock=True,
            created_at=_now(),
        )
        self.approvals["apr-1002"] = Approval(
            id="apr-1002",
            title="Edit generated MetadataResponse.java",
            prompt="",
            repo=default_repo(),
            ref=default_ref(),
            source="mock",
            status="rejected",
            note="Rejected by the guardrail: generated sources are regenerated, not edited.",
            mock=True,
            created_at=_now(),
            decided_at=_now(),
        )
        self.agents["agent-1001"] = AgentRun(
            id="agent-1001",
            approval_id=None,
            repo=default_repo(),
            ref=default_ref(),
            prompt="Sample row only.",
            status="not_sent",
            agent_id=None,
            url=None,
            error=None,
            mock=True,
            created_at=_now(),
        )

    def snapshot(self) -> dict:
        with self._lock:
            approvals = [asdict(item) for item in reversed(list(self.approvals.values()))]
            agents = [asdict(item) for item in reversed(list(self.agents.values()))]
        return {
            "approvals": approvals,
            "agents": agents,
            "defaults": {"repo": default_repo(), "ref": default_ref()},
            "cursor_api_key_configured": bool(_env("CURSOR_API_KEY")),
            "slack_signing_configured": bool(_env("SLACK_SIGNING_SECRET")),
        }

    def add_approval(self, title: str, prompt: str, repo: str, ref: str, source: str) -> Approval:
        with self._lock:
            approval_id = f"apr-{self._next_approval}"
            self._next_approval += 1
            approval = Approval(
                id=approval_id,
                title=title.strip() or "Untitled request",
                prompt=prompt.strip(),
                repo=repo.strip() or default_repo(),
                ref=ref.strip() or default_ref(),
                source=source,
                status="pending",
                note="",
                mock=False,
                created_at=_now(),
            )
            self.approvals[approval_id] = approval
            return approval

    def decide(self, approval_id: str, status: str, note: str) -> Approval:
        with self._lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            if approval.status != "pending":
                raise ValueError(approval.status)
            approval.status = status
            approval.note = note
            approval.decided_at = _now()
            return approval

    def add_agent(self, run: AgentRun) -> AgentRun:
        with self._lock:
            self.agents[run.id] = run
            return run

    def next_agent_id(self) -> str:
        with self._lock:
            agent_id = f"agent-{self._next_agent}"
            self._next_agent += 1
            return agent_id

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            return self.approvals.get(approval_id)


STORE = Store()


class ApprovalIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    repo: str = ""
    ref: str = ""
    source: str = "dashboard"


class AgentIn(BaseModel):
    repo: str = ""
    ref: str = ""
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    name: str | None = Field(default=None, max_length=100)
    approval_id: str | None = None


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


def launch_via_script(repo: str, ref: str, prompt: str, name: str | None, approval_id: str | None) -> AgentRun:
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
        run = AgentRun(
            id=agent_id,
            approval_id=approval_id,
            repo=repo,
            ref=ref,
            prompt=prompt,
            status="error",
            agent_id=None,
            url=None,
            error=str(exc)[:2000],
            mock=False,
            created_at=_now(),
        )
        return STORE.add_agent(run)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "cursor_api_trigger.py failed").strip()
        run = AgentRun(
            id=agent_id,
            approval_id=approval_id,
            repo=repo,
            ref=ref,
            prompt=prompt,
            status="error",
            agent_id=None,
            url=None,
            error=detail[:2000],
            mock=False,
            created_at=_now(),
        )
        return STORE.add_agent(run)

    remote_agent_id = None
    url = None
    try:
        payload = json.loads(completed.stdout)
        agent = payload.get("agent") if isinstance(payload, dict) else None
        if isinstance(agent, dict):
            remote_agent_id = agent.get("id")
            url = agent.get("url")
    except json.JSONDecodeError:
        run = AgentRun(
            id=agent_id,
            approval_id=approval_id,
            repo=repo,
            ref=ref,
            prompt=prompt,
            status="error",
            agent_id=None,
            url=None,
            error="cursor_api_trigger.py returned a non-JSON body",
            mock=False,
            created_at=_now(),
        )
        return STORE.add_agent(run)

    run = AgentRun(
        id=agent_id,
        approval_id=approval_id,
        repo=repo,
        ref=ref,
        prompt=prompt,
        status="launched",
        agent_id=remote_agent_id,
        url=url,
        error=None,
        mock=False,
        created_at=_now(),
    )
    return STORE.add_agent(run)


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
    return STORE.snapshot()


@app.post("/api/approvals")
def create_approval(body: ApprovalIn) -> dict:
    approval = STORE.add_approval(body.title, body.prompt, body.repo, body.ref, body.source)
    return asdict(approval)


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, launch: bool = False) -> dict:
    try:
        approval = STORE.decide(approval_id, "approved", "Approved from the dashboard.")
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"approval is already {exc}") from None
    result: dict = {"approval": asdict(approval), "launch": None}
    if launch:
        if not approval.prompt:
            result["launch"] = {"ok": False, "error": "approval has no prompt to launch"}
            return result
        run = launch_via_script(approval.repo, approval.ref, approval.prompt, approval.title[:100], approval.id)
        result["launch"] = {"ok": run.status == "launched", "agent": asdict(run)}
    return result


@app.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str) -> dict:
    try:
        approval = STORE.decide(approval_id, "rejected", "Rejected from the dashboard.")
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"approval is already {exc}") from None
    return asdict(approval)


@app.post("/api/agents")
def trigger_agent(body: AgentIn) -> dict:
    repo = body.repo.strip() or default_repo()
    ref = body.ref.strip() or default_ref()
    if body.approval_id is not None:
        approval = STORE.get(body.approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if approval.status != "approved":
            raise HTTPException(status_code=409, detail="approval must be approved before launch")
    run = launch_via_script(repo, ref, body.prompt, body.name, body.approval_id)
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
            approval = STORE.add_approval(
                title="Slack mention",
                prompt=text[:MAX_PROMPT_CHARS],
                repo=default_repo(),
                ref=default_ref(),
                source="slack",
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
    user = first("user_name").strip()
    text = first("text").strip()[:MAX_PROMPT_CHARS]
    title = f"{command} from {user}".strip() if user else command
    approval = STORE.add_approval(
        title=title[:200],
        prompt=text,
        repo=default_repo(),
        ref=default_ref(),
        source="slack",
    )
    return {
        "response_type": "ephemeral",
        "text": f"Queued {approval.id}. Approve it on the Grok Bot dashboard before an agent starts.",
    }
