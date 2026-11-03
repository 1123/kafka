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

"""SQLite store for Grok Bot approvals, launches, and the audit log.

The event table is append-only: rows are inserted and never updated or deleted.
The Cursor agent cache is the last successful API snapshot and is replaced as a
whole when a newer list arrives.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path() -> Path:
    configured = os.environ.get("GROK_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "grok-bot.sqlite"


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
    requester: str
    approver: str
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
    created_at: str


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                repo TEXT NOT NULL,
                ref TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                requester TEXT NOT NULL DEFAULT '',
                approver TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                approval_id TEXT,
                repo TEXT NOT NULL,
                ref TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                agent_id TEXT,
                url TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                approval_id TEXT,
                agent_run_id TEXT,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursor_cache (
                repo_key TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                url TEXT,
                repo TEXT NOT NULL DEFAULT '',
                ref TEXT NOT NULL DEFAULT '',
                branch TEXT,
                pr_url TEXT,
                run_status TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                latest_run_id TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (repo_key, agent_id)
            );
            CREATE TABLE IF NOT EXISTS cursor_cache_meta (
                repo_key TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL
            );
            """
        )

    def _alloc(self, counter: str, prefix: str) -> str:
        row = self._conn.execute("SELECT value FROM counters WHERE name = ?", (counter,)).fetchone()
        if row is None:
            value = 1
            self._conn.execute("INSERT INTO counters (name, value) VALUES (?, ?)", (counter, value))
        else:
            value = int(row["value"]) + 1
            self._conn.execute("UPDATE counters SET value = ? WHERE name = ?", (value, counter))
        return f"{prefix}-{value}"

    def _event(
        self,
        kind: str,
        actor: str,
        approval_id: str | None,
        agent_run_id: str | None,
        detail: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (ts, kind, actor, approval_id, agent_run_id, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_now(), kind, actor[:200], approval_id, agent_run_id, detail[:2000]),
        )

    def add_approval(
        self,
        title: str,
        prompt: str,
        repo: str,
        ref: str,
        source: str,
        requester: str,
    ) -> Approval:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                approval_id = self._alloc("approval", "apr")
                created_at = _now()
                approval = Approval(
                    id=approval_id,
                    title=(title or "").strip() or "Untitled request",
                    prompt=(prompt or "").strip(),
                    repo=repo,
                    ref=ref,
                    source=source,
                    status="pending",
                    note="",
                    requester=(requester or "").strip()[:200],
                    approver="",
                    created_at=created_at,
                )
                self._conn.execute(
                    """
                    INSERT INTO approvals (
                        id, title, prompt, repo, ref, source, status, note,
                        requester, approver, created_at, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.id,
                        approval.title,
                        approval.prompt,
                        approval.repo,
                        approval.ref,
                        approval.source,
                        approval.status,
                        approval.note,
                        approval.requester,
                        approval.approver,
                        approval.created_at,
                        None,
                    ),
                )
                self._event("create", approval.requester, approval.id, None, f"source={approval.source} title={approval.title}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return approval

    def decide(self, approval_id: str, status: str, note: str, approver: str) -> Approval:
        approver = (approver or "").strip()
        if not approver:
            raise PermissionError("approver required")
        if status not in {"approved", "rejected"}:
            raise ValueError(status)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
                if row is None:
                    raise KeyError(approval_id)
                if row["status"] != "pending":
                    raise ValueError(row["status"])
                decided_at = _now()
                self._conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, note = ?, approver = ?, decided_at = ?
                    WHERE id = ?
                    """,
                    (status, note[:2000], approver[:200], decided_at, approval_id),
                )
                kind = "approve" if status == "approved" else "reject"
                self._event(kind, approver, approval_id, None, note[:2000])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        approval = self.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        return approval

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        return _approval_from_row(row)

    def list_approvals(self) -> list[Approval]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM approvals ORDER BY rowid DESC").fetchall()
        return [_approval_from_row(row) for row in rows]

    def next_agent_id(self) -> str:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                agent_id = self._alloc("agent", "agent")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return agent_id

    def add_agent(self, run: AgentRun, actor: str) -> AgentRun:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO agent_runs (
                        id, approval_id, repo, ref, prompt, status, agent_id, url, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.approval_id,
                        run.repo,
                        run.ref,
                        run.prompt,
                        run.status,
                        run.agent_id,
                        run.url,
                        run.error,
                        run.created_at,
                    ),
                )
                detail = f"status={run.status}"
                if run.agent_id:
                    detail += f" remote={run.agent_id}"
                if run.error:
                    detail += f" error={run.error[:500]}"
                self._event("launch", (actor or "").strip()[:200], run.approval_id, run.id, detail)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return run

    def list_agents(self) -> list[AgentRun]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM agent_runs ORDER BY rowid DESC").fetchall()
        return [_agent_from_row(row) for row in rows]

    def list_events(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (max(int(limit), 0),),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "kind": row["kind"],
                "actor": row["actor"],
                "approval_id": row["approval_id"],
                "agent_run_id": row["agent_run_id"],
                "detail": row["detail"],
            }
            for row in rows
        ]

    def replace_cursor_cache(self, repo_key: str, agents: list[dict]) -> None:
        fetched_at = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM cursor_cache WHERE repo_key = ?", (repo_key,))
                for agent in agents:
                    agent_id = agent.get("agent_id")
                    if not isinstance(agent_id, str) or not agent_id:
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO cursor_cache (
                            repo_key, agent_id, name, status, url, repo, ref, branch, pr_url,
                            run_status, created_at, latest_run_id, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repo_key,
                            agent_id,
                            str(agent.get("name") or "")[:500],
                            str(agent.get("status") or "")[:100],
                            agent.get("url"),
                            str(agent.get("repo") or ""),
                            str(agent.get("ref") or ""),
                            agent.get("branch"),
                            agent.get("pr_url"),
                            agent.get("run_status"),
                            str(agent.get("created_at") or ""),
                            agent.get("latest_run_id"),
                            fetched_at,
                        ),
                    )
                self._conn.execute(
                    """
                    INSERT INTO cursor_cache_meta (repo_key, fetched_at)
                    VALUES (?, ?)
                    ON CONFLICT(repo_key) DO UPDATE SET fetched_at = excluded.fetched_at
                    """,
                    (repo_key, fetched_at),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def list_cursor_cache(self, repo_key: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM cursor_cache
                WHERE repo_key = ?
                ORDER BY created_at DESC, agent_id DESC
                """,
                (repo_key,),
            ).fetchall()
        return [
            {
                "agent_id": row["agent_id"],
                "name": row["name"],
                "status": row["status"],
                "url": row["url"],
                "repo": row["repo"],
                "ref": row["ref"],
                "branch": row["branch"],
                "pr_url": row["pr_url"],
                "run_status": row["run_status"],
                "created_at": row["created_at"],
                "latest_run_id": row["latest_run_id"],
            }
            for row in rows
        ]

    def cursor_cache_fetched_at(self, repo_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at FROM cursor_cache_meta WHERE repo_key = ?",
                (repo_key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["fetched_at"])


def _approval_from_row(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        title=row["title"],
        prompt=row["prompt"],
        repo=row["repo"],
        ref=row["ref"],
        source=row["source"],
        status=row["status"],
        note=row["note"],
        requester=row["requester"],
        approver=row["approver"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def _agent_from_row(row: sqlite3.Row) -> AgentRun:
    return AgentRun(
        id=row["id"],
        approval_id=row["approval_id"],
        repo=row["repo"],
        ref=row["ref"],
        prompt=row["prompt"],
        status=row["status"],
        agent_id=row["agent_id"],
        url=row["url"],
        error=row["error"],
        created_at=row["created_at"],
    )
