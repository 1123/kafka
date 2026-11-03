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

"""Persistence, approval, and Cursor list tests for the Grok Bot front door.

Run from grok-bot/ with the app's virtualenv:

    python -m unittest test_app.py
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.environ["GROK_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="grok-bot-import-")) / "import.sqlite")
os.environ.pop("CURSOR_API_KEY", None)
os.environ.pop("SLACK_SIGNING_SECRET", None)
os.environ.pop("GROK_APPROVER", None)

import app as app_module
import cursor_api_trigger
import store as store_module
from fastapi import HTTPException

try:
    from fastapi.testclient import TestClient
except Exception:  # httpx is optional; the route tests skip without it
    TestClient = None

FAKE_KEY = "test-cursor-key"
REPO = "https://github.com/1123/kafka"


class BotCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "bot.sqlite"
        self._previous_store = app_module.STORE
        self._previous_directory = app_module.DIRECTORY
        app_module.STORE = store_module.Store(self.path)
        app_module.DIRECTORY = app_module.AgentDirectory(lambda repo, api_key: [])
        app_module.DIRECTORY._fetched_at = time.monotonic()
        app_module.DIRECTORY._ttl = 10**9
        os.environ.pop("GROK_APPROVER", None)
        os.environ.pop("CURSOR_API_KEY", None)
        os.environ.pop("SLACK_SIGNING_SECRET", None)

    def tearDown(self) -> None:
        app_module.STORE.close()
        app_module.STORE = self._previous_store
        app_module.DIRECTORY = self._previous_directory
        self._tmp.cleanup()
        os.environ.pop("GROK_APPROVER", None)
        os.environ.pop("CURSOR_API_KEY", None)
        os.environ.pop("SLACK_SIGNING_SECRET", None)

    def queue(self, title: str = "KIP-994", prompt: str = "Describe the share-group lag work.", requester: str = "Ada") -> store_module.Approval:
        return app_module.create_approval(
            app_module.ApprovalIn(title=title, prompt=prompt, repo=REPO, ref="trunk", requester=requester)
        )


class PersistenceTests(BotCase):
    def test_fresh_store_has_no_seeded_rows(self) -> None:
        snapshot = app_module.status()
        self.assertEqual(snapshot["approvals"], [])
        self.assertEqual(snapshot["agents"], [])
        self.assertEqual(snapshot["events"], [])
        text = Path(app_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("apr-1001", text)
        self.assertNotIn("Share-group lag", text)

    def test_approval_survives_reopen_and_records_who(self) -> None:
        created = self.queue()
        self.assertEqual(created["requester"], "Ada")
        self.assertEqual(created["status"], "pending")
        decided = app_module.approve(
            created["id"],
            body=app_module.DecisionIn(approver="Grace", note="Ship the metric sketch."),
        )
        approval = decided["approval"]
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["approver"], "Grace")
        self.assertEqual(approval["note"], "Ship the metric sketch.")
        self.assertIsNotNone(approval["decided_at"])

        app_module.STORE.close()
        app_module.STORE = store_module.Store(self.path)
        reloaded = app_module.STORE.get(created["id"])
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "approved")
        self.assertEqual(reloaded.approver, "Grace")
        self.assertEqual(reloaded.requester, "Ada")
        kinds = [item["kind"] for item in app_module.STORE.list_events()]
        self.assertEqual(kinds, ["approve", "create"])

    def test_approver_falls_back_to_env_and_is_required(self) -> None:
        created = self.queue(requester="")
        os.environ["GROK_APPROVER"] = "Env Approver"
        empty_requester = app_module.create_approval(app_module.ApprovalIn(title="Second", prompt="work"))
        self.assertEqual(empty_requester["requester"], "Env Approver")
        decided = app_module.approve(created["id"], body=app_module.DecisionIn(approver="  "))
        self.assertEqual(decided["approval"]["approver"], "Env Approver")
        os.environ.pop("GROK_APPROVER", None)
        pending = app_module.create_approval(app_module.ApprovalIn(title="Needs a person", prompt="work"))
        with self.assertRaises(HTTPException) as raised:
            app_module.reject(pending["id"], body=app_module.DecisionIn())
        self.assertEqual(raised.exception.status_code, 400)

    def test_second_decision_conflicts_and_events_stay(self) -> None:
        created = self.queue()
        app_module.reject(created["id"], body=app_module.DecisionIn(approver="Grace", note="No."))
        with self.assertRaises(HTTPException) as raised:
            app_module.approve(created["id"], body=app_module.DecisionIn(approver="Grace"))
        self.assertEqual(raised.exception.status_code, 409)
        missing = app_module.STORE.list_events()
        self.assertEqual([item["kind"] for item in missing], ["reject", "create"])
        with self.assertRaises(HTTPException) as missing_row:
            app_module.approve("apr-missing", body=app_module.DecisionIn(approver="Grace"))
        self.assertEqual(missing_row.exception.status_code, 404)
        self.assertEqual(len(app_module.STORE.list_events()), 2)
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM events", source)
        self.assertNotIn("UPDATE events", source)

    def test_database_path_follows_env(self) -> None:
        custom = self.path.parent / "custom.sqlite"
        previous = os.environ.get("GROK_DB_PATH")
        os.environ["GROK_DB_PATH"] = str(custom)
        try:
            self.assertEqual(store_module.default_db_path(), custom)
        finally:
            if previous is None:
                os.environ.pop("GROK_DB_PATH", None)
            else:
                os.environ["GROK_DB_PATH"] = previous


class LaunchTests(BotCase):
    def test_launch_requires_approved_record_and_uses_stored_fields(self) -> None:
        created = self.queue(prompt="Stored prompt only.")
        with self.assertRaises(HTTPException) as pending:
            app_module.trigger_agent(app_module.AgentIn(approval_id=created["id"]))
        self.assertEqual(pending.exception.status_code, 409)
        with self.assertRaises(HTTPException) as missing:
            app_module.trigger_agent(app_module.AgentIn(approval_id="apr-none"))
        self.assertEqual(missing.exception.status_code, 404)

        app_module.approve(created["id"], body=app_module.DecisionIn(approver="Grace"))
        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env") or {}

            class Result:
                returncode = 0
                stdout = json.dumps({"agent": {"id": "bc-real", "url": "https://cursor.com/agents/bc-real"}})
                stderr = ""

            return Result()

        with patch.object(app_module.subprocess, "run", fake_run):
            launched = app_module.trigger_agent(app_module.AgentIn(approval_id=created["id"]))
        command = captured["command"]
        self.assertNotIn("--auto-create-pr", command)
        self.assertNotIn("--work-on-current-branch", command)
        self.assertEqual(command[command.index("--prompt") + 1], "Stored prompt only.")
        self.assertEqual(command[command.index("--repo") + 1], REPO)
        self.assertEqual(command[command.index("--ref") + 1], "trunk")
        self.assertNotIn(FAKE_KEY, " ".join(command))
        self.assertEqual(launched["agent"]["agent_id"], "bc-real")
        self.assertEqual(launched["agent"]["approval_id"], created["id"])
        kinds = [item["kind"] for item in app_module.STORE.list_events()]
        self.assertEqual(kinds[0], "launch")
        self.assertEqual(app_module.STORE.list_events()[0]["actor"], "Grace")

        app_module.STORE.close()
        app_module.STORE = store_module.Store(self.path)
        reloaded = app_module.STORE.list_agents()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].agent_id, "bc-real")
        self.assertEqual(reloaded[0].prompt, "Stored prompt only.")

    def test_launch_error_redacts_the_api_key(self) -> None:
        created = self.queue()
        app_module.approve(created["id"], body=app_module.DecisionIn(approver="Grace"))
        os.environ["CURSOR_API_KEY"] = FAKE_KEY

        def fake_run(command, **kwargs):
            class Result:
                returncode = 1
                stdout = ""
                stderr = f"Cursor API returned HTTP 401: bad {FAKE_KEY}"

            return Result()

        with patch.object(app_module.subprocess, "run", fake_run):
            with self.assertRaises(HTTPException) as raised:
                app_module.trigger_agent(app_module.AgentIn(approval_id=created["id"]))
        self.assertEqual(raised.exception.status_code, 502)
        error = raised.exception.detail["agent"]["error"]
        self.assertNotIn(FAKE_KEY, error)
        self.assertNotIn(FAKE_KEY, self.path.read_text(encoding="utf-8", errors="replace"))
        self.assertIn("[redacted]", error)

    def test_empty_prompt_cannot_launch(self) -> None:
        created = self.queue(prompt="")
        result = app_module.approve(
            created["id"],
            launch=True,
            body=app_module.DecisionIn(approver="Grace"),
        )
        self.assertEqual(result["approval"]["status"], "approved")
        self.assertFalse(result["launch"]["ok"])
        self.assertEqual(app_module.STORE.list_agents(), [])

    def test_approve_and_launch_keeps_flags_off(self) -> None:
        created = self.queue()
        seen: dict = {}

        def fake_run(command, **kwargs):
            seen["command"] = command

            class Result:
                returncode = 0
                stdout = json.dumps({"agent": {"id": "bc-2", "url": "https://cursor.com/agents/bc-2"}})
                stderr = ""

            return Result()

        with patch.object(app_module.subprocess, "run", fake_run):
            result = app_module.approve(created["id"], launch=True, body=app_module.DecisionIn(approver="Grace"))
        self.assertTrue(result["launch"]["ok"])
        self.assertNotIn("--auto-create-pr", seen["command"])
        self.assertNotIn("--work-on-current-branch", seen["command"])
        payload = cursor_api_trigger.build_payload(REPO, "trunk", "hello")
        self.assertIs(payload["autoCreatePR"], False)
        self.assertIs(payload["workOnCurrentBranch"], False)


class SlackTests(BotCase):
    def test_signature_still_rejects_a_bad_mac(self) -> None:
        body = b"token=x"
        timestamp = str(int(time.time()))
        good = _sign(body, "dev-secret", timestamp)
        self.assertTrue(app_module.verify_slack_signature(body, timestamp, good, "dev-secret"))
        self.assertFalse(app_module.verify_slack_signature(body, timestamp, "v0=nope", "dev-secret"))
        self.assertFalse(app_module.verify_slack_signature(body, "not-a-time", good, "dev-secret"))
        self.assertFalse(app_module.verify_slack_signature(body, timestamp, good, ""))

    def test_slack_command_records_the_user_id(self) -> None:
        os.environ["GROK_APPROVER"] = "Should Not Become Requester"
        body = urllib_encode(
            {
                "command": "/grok",
                "user_id": "U123",
                "user_name": "ada",
                "text": "look at KIP-994",
            }
        )
        result = app_module._handle_slack_command(body)
        approval = app_module.STORE.get(result["text"].split()[1].rstrip("."))
        self.assertIsNotNone(approval)
        assert approval is not None
        self.assertEqual(approval.requester, "U123")
        self.assertEqual(approval.source, "slack")
        self.assertEqual(approval.status, "pending")
        self.assertIn("U123", app_module.STORE.list_events()[0]["actor"])

    def test_slack_mention_records_the_user_id(self) -> None:
        payload = {
            "type": "event_callback",
            "event": {"type": "app_mention", "user": "U777", "text": "<@UBOT> check lag"},
        }
        result = app_module._handle_slack_json(json.dumps(payload).encode())
        approval = app_module.STORE.get(result["approval_id"])
        self.assertIsNotNone(approval)
        assert approval is not None
        self.assertEqual(approval.requester, "U777")
        self.assertEqual(approval.prompt, "check lag")


class CursorListTests(BotCase):
    def test_repo_key_ignores_scheme_and_git_suffix(self) -> None:
        self.assertEqual(
            cursor_api_trigger.repo_key("https://github.com/1123/kafka.git"),
            cursor_api_trigger.repo_key("github.com/1123/kafka"),
        )
        self.assertEqual(
            cursor_api_trigger.repo_key("http://www.github.com/1123/kafka/"),
            "github.com/1123/kafka",
        )

    def test_list_uses_documented_endpoints_and_merges_branch(self) -> None:
        calls: list[tuple] = []

        def fake(method, path, api_key, query=None, body=None, timeout=20.0):
            calls.append((method, path, query, api_key))
            self.assertNotIn(FAKE_KEY, path)
            if path == "/v1/agents":
                return {
                    "items": [
                        {"id": "bc-keep", "latestRunId": "run-1"},
                        {"id": "bc-other", "latestRunId": "run-2"},
                    ]
                }
            if path == "/v1/agents/bc-keep":
                return {
                    "id": "bc-keep",
                    "name": "KIP-994 review",
                    "status": "IDLE",
                    "url": "https://cursor.com/agents/bc-keep",
                    "createdAt": "2026-11-01T00:00:00Z",
                    "latestRunId": "run-1",
                    "repos": [{"url": "https://github.com/1123/kafka.git", "startingRef": "trunk"}],
                }
            if path == "/v1/agents/bc-other":
                return {
                    "id": "bc-other",
                    "name": "Elsewhere",
                    "status": "ACTIVE",
                    "repos": [{"url": "https://github.com/other/repo", "startingRef": "main"}],
                    "latestRunId": "run-2",
                }
            if path == "/v1/agents/bc-old":
                return {
                    "id": "bc-old",
                    "name": "Older local launch",
                    "status": "ARCHIVED",
                    "url": "https://cursor.com/agents/bc-old",
                    "createdAt": "2026-10-01T00:00:00Z",
                    "latestRunId": "run-old",
                    "repos": [{"url": "https://github.com/1123/kafka", "startingRef": "trunk"}],
                }
            if path == "/v1/agents/bc-keep/runs/run-1":
                return {
                    "id": "run-1",
                    "status": "FINISHED",
                    "git": {
                        "branches": [
                            {
                                "repoUrl": "github.com/other/repo",
                                "branch": "cursor/other",
                                "prUrl": "https://github.com/other/repo/pull/1",
                            },
                            {
                                "repoUrl": "github.com/1123/kafka",
                                "branch": "cursor/kip-994",
                                "prUrl": "https://github.com/1123/kafka/pull/9",
                            },
                        ]
                    },
                }
            if path == "/v1/agents/bc-old/runs/run-old":
                return {"id": "run-old", "status": "EXPIRED", "git": {"branches": []}}
            raise AssertionError(path)

        found = cursor_api_trigger.list_repo_agents(
            FAKE_KEY,
            REPO,
            known_agent_ids=["bc-old"],
            fetch=fake,
            workers=1,
        )
        ids = [item["agent_id"] for item in found]
        self.assertEqual(ids, ["bc-keep", "bc-old"])
        self.assertEqual(found[0]["branch"], "cursor/kip-994")
        self.assertEqual(found[0]["pr_url"], "https://github.com/1123/kafka/pull/9")
        self.assertEqual(found[0]["run_status"], "FINISHED")
        self.assertEqual(found[0]["status"], "IDLE")
        paths = [call[1] for call in calls]
        self.assertIn("/v1/agents", paths)
        self.assertIn("/v1/agents/bc-keep", paths)
        self.assertIn("/v1/agents/bc-keep/runs/run-1", paths)
        self.assertIn("/v1/agents/bc-old", paths)
        self.assertTrue(all(call[3] == FAKE_KEY for call in calls))
        self.assertTrue(all(FAKE_KEY not in call[1] for call in calls))

    def test_fetch_keeps_the_key_out_of_the_url_and_errors(self) -> None:
        seen: dict = {}

        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=401,
                msg="no",
                hdrs=None,
                fp=io.BytesIO(f"unauthorized {FAKE_KEY}".encode()),
            )

        with patch.object(cursor_api_trigger.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(cursor_api_trigger.CursorApiError) as raised:
                cursor_api_trigger.fetch_json("GET", "/v1/agents", FAKE_KEY, {"limit": "20"})
        self.assertNotIn(FAKE_KEY, seen["url"])
        self.assertNotIn(FAKE_KEY, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))
        self.assertTrue(seen["auth"].startswith("Basic "))

    def test_detail_failures_do_not_look_like_an_empty_repo(self) -> None:
        def fake(method, path, api_key, query=None, body=None, timeout=20.0):
            if path == "/v1/agents":
                return {"items": [{"id": "bc-keep"}]}
            raise cursor_api_trigger.CursorApiError(f"Cursor API returned HTTP 401: bad {FAKE_KEY}")

        with self.assertRaises(cursor_api_trigger.CursorApiError) as raised:
            cursor_api_trigger.list_repo_agents(FAKE_KEY, REPO, fetch=fake, workers=1)
        self.assertNotIn(FAKE_KEY, str(raised.exception))

    def test_empty_cursor_refresh_still_records_fetch_time(self) -> None:
        os.environ["CURSOR_API_KEY"] = FAKE_KEY
        app_module.DIRECTORY = app_module.AgentDirectory(lambda repo, api_key: [])
        app_module.DIRECTORY.refresh(REPO)
        os.environ.pop("CURSOR_API_KEY", None)
        snapshot = app_module.status()
        self.assertEqual(snapshot["agents"], [])
        self.assertIsNotNone(snapshot["cursor_agents_fetched_at"])

    def test_status_merges_cached_cursor_agents_after_restart(self) -> None:
        os.environ["CURSOR_API_KEY"] = FAKE_KEY

        def loader(repo, api_key):
            self.assertEqual(api_key, FAKE_KEY)
            self.assertNotIn(FAKE_KEY, repo)
            return [
                {
                    "agent_id": "bc-live",
                    "name": "KIP-994 review",
                    "status": "ACTIVE",
                    "url": "https://cursor.com/agents/bc-live",
                    "repo": REPO,
                    "ref": "trunk",
                    "branch": "cursor/kip-994",
                    "pr_url": "https://github.com/1123/kafka/pull/15",
                    "run_status": "RUNNING",
                    "created_at": "2026-11-02T00:00:00Z",
                    "latest_run_id": "run-9",
                }
            ]

        app_module.DIRECTORY = app_module.AgentDirectory(loader)
        app_module.DIRECTORY.refresh(REPO)
        os.environ.pop("CURSOR_API_KEY", None)
        snapshot = app_module.status()
        self.assertIsNone(snapshot["cursor_agents_error"])
        self.assertEqual(snapshot["agents"][0]["origin"], "cursor")
        self.assertEqual(snapshot["agents"][0]["branch"], "cursor/kip-994")
        self.assertEqual(snapshot["agents"][0]["pr_url"], "https://github.com/1123/kafka/pull/15")

        created = self.queue()
        app_module.approve(created["id"], body=app_module.DecisionIn(approver="Grace"))

        def fake_run(command, **kwargs):
            class Result:
                returncode = 0
                stdout = json.dumps({"agent": {"id": "bc-live", "url": "https://cursor.com/agents/bc-live"}})
                stderr = ""

            return Result()

        with patch.object(app_module.subprocess, "run", fake_run):
            app_module.trigger_agent(app_module.AgentIn(approval_id=created["id"]))
        merged = app_module.status()["agents"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["origin"], "merged")
        self.assertEqual(merged[0]["approval_id"], created["id"])
        self.assertEqual(merged[0]["status"], "ACTIVE")
        self.assertEqual(merged[0]["branch"], "cursor/kip-994")

        def boom(repo, api_key):
            raise RuntimeError(f"down {FAKE_KEY}")

        os.environ["CURSOR_API_KEY"] = FAKE_KEY
        app_module.DIRECTORY = app_module.AgentDirectory(boom)
        app_module.DIRECTORY.refresh(REPO)
        self.assertNotIn(FAKE_KEY, app_module.DIRECTORY.error or "")
        os.environ.pop("CURSOR_API_KEY", None)

        app_module.STORE.close()
        app_module.STORE = store_module.Store(self.path)
        restarted = app_module.status()
        self.assertEqual(restarted["agents"][0]["agent_id"], "bc-live")
        self.assertEqual(restarted["agents"][0]["pr_url"], "https://github.com/1123/kafka/pull/15")
        self.assertTrue(any(item["kind"] == "launch" for item in restarted["events"]))
        self.assertNotIn(FAKE_KEY, self.path.read_bytes().decode("utf-8", errors="replace"))


@unittest.skipUnless(TestClient is not None, "fastapi TestClient needs httpx")
class HttpTests(BotCase):
    def test_slack_route_still_checks_the_signature(self) -> None:
        with TestClient(app_module.app) as client:
            missing = client.post("/slack/events", content=b"{}", headers={"content-type": "application/json"})
            self.assertEqual(missing.status_code, 503)
            os.environ["SLACK_SIGNING_SECRET"] = "dev-secret"
            body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
            timestamp = str(int(time.time()))
            bad = client.post(
                "/slack/events",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-slack-request-timestamp": timestamp,
                    "x-slack-signature": "v0=nope",
                },
            )
            self.assertEqual(bad.status_code, 401)
            good = client.post(
                "/slack/events",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-slack-request-timestamp": timestamp,
                    "x-slack-signature": _sign(body, "dev-secret", timestamp),
                },
            )
            self.assertEqual(good.status_code, 200)
            self.assertEqual(good.json()["challenge"], "abc")
            self.assertEqual(app_module.STORE.list_approvals(), [])

    def test_http_approve_and_blocked_launch(self) -> None:
        with TestClient(app_module.app) as client:
            queued = client.post(
                "/api/approvals",
                json={"title": "KIP-994", "prompt": "stored", "requester": "Ada", "repo": REPO, "ref": "trunk"},
            )
            self.assertEqual(queued.status_code, 200, queued.text)
            approval_id = queued.json()["id"]
            blocked = client.post("/api/agents", json={"approval_id": approval_id, "prompt": "ignore me", "repo": "https://github.com/other/repo"})
            self.assertEqual(blocked.status_code, 409)
            bare = client.post(f"/api/approvals/{approval_id}/approve", json={})
            self.assertEqual(bare.status_code, 400)
            os.environ["GROK_APPROVER"] = "Env Approver"
            approved = client.post(f"/api/approvals/{approval_id}/approve?launch=false", json={})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["approval"]["approver"], "Env Approver")
            captured: dict = {}

            def fake_run(command, **kwargs):
                captured["command"] = command

                class Result:
                    returncode = 0
                    stdout = json.dumps({"agent": {"id": "bc-http", "url": "https://cursor.com/agents/bc-http"}})
                    stderr = ""

                return Result()

            with patch.object(app_module.subprocess, "run", fake_run):
                launched = client.post("/api/agents", json={"approval_id": approval_id, "prompt": "ignore me"})
            self.assertEqual(launched.status_code, 200, launched.text)
            self.assertEqual(captured["command"][captured["command"].index("--prompt") + 1], "stored")
            self.assertEqual(captured["command"][captured["command"].index("--repo") + 1], REPO)
            self.assertNotIn("--auto-create-pr", captured["command"])


def _sign(body: bytes, secret: str, timestamp: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), b"v0:" + timestamp.encode("utf-8") + b":" + body, hashlib.sha256).hexdigest()
    return "v0=" + digest


def urllib_encode(fields: dict[str, str]) -> bytes:
    from urllib.parse import urlencode

    return urlencode(fields).encode()


if __name__ == "__main__":
    unittest.main()
