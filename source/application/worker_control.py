"""GitHub Variables based control plane for the video workers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests

HEARTBEAT_VAR = "MAC_VIDEO_WORKER_HEARTBEAT_MS"
LOCK_UNTIL_VAR = "VIDEO_WORKER_FALLBACK_LOCK_UNTIL_MS"
LOCK_OWNER_VAR = "VIDEO_WORKER_FALLBACK_LOCK_OWNER"


class WorkerControlError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


@dataclass
class FallbackDecision:
    should_run: bool
    heartbeat_fresh: bool
    lock_acquired: bool
    reason: str


class GitHubWorkerControl:
    """Store worker heartbeats and fallback locks in GitHub Actions variables."""

    def __init__(
        self,
        repository: str,
        token: str,
        owner: str,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.repository = repository.strip()
        self.token = token.strip()
        self.owner = owner.strip() or "unknown-worker"
        self.api_url = api_url.rstrip("/")
        if not self.repository:
            raise WorkerControlError("Missing GITHUB_REPOSITORY or VIDEO_WORKER_GITHUB_REPOSITORY")
        if not self.token:
            raise WorkerControlError("Missing GITHUB_TOKEN, GH_TOKEN, or VIDEO_WORKER_GITHUB_TOKEN")

    @classmethod
    def from_env(cls, owner: str) -> "GitHubWorkerControl":
        return cls(
            repository=(
                os.getenv("VIDEO_WORKER_GITHUB_REPOSITORY")
                or os.getenv("GITHUB_REPOSITORY")
                or ""
            ),
            token=(
                os.getenv("VIDEO_WORKER_GITHUB_TOKEN")
                or os.getenv("GITHUB_TOKEN")
                or os.getenv("GH_TOKEN")
                or ""
            ),
            owner=owner,
            api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _variable_url(self, name: str) -> str:
        return f"{self.api_url}/repos/{self.repository}/actions/variables/{name}"

    def get_variable(self, name: str) -> str:
        response = requests.get(
            self._variable_url(name),
            headers=self._headers(),
            timeout=20,
        )
        if response.status_code == 404:
            return ""
        if not response.ok:
            raise WorkerControlError(
                f"GitHub variable read failed: HTTP {response.status_code}"
            )
        return str((response.json() or {}).get("value") or "")

    def set_variable(self, name: str, value: str) -> None:
        payload = {"name": name, "value": str(value)}
        response = requests.patch(
            self._variable_url(name),
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        if response.status_code == 404:
            response = requests.post(
                f"{self.api_url}/repos/{self.repository}/actions/variables",
                headers=self._headers(),
                json=payload,
                timeout=20,
            )
        if response.status_code not in {201, 204}:
            raise WorkerControlError(
                f"GitHub variable write failed: HTTP {response.status_code}"
            )

    def heartbeat_age_seconds(self, now_ms: int | None = None) -> int | None:
        heartbeat_ms = _parse_int(self.get_variable(HEARTBEAT_VAR), 0)
        if heartbeat_ms <= 0:
            return None
        now = now_ms if now_ms is not None else _now_ms()
        return max(0, int((now - heartbeat_ms) / 1000))

    def heartbeat_is_fresh(
        self,
        max_age_seconds: int,
        now_ms: int | None = None,
    ) -> bool:
        age = self.heartbeat_age_seconds(now_ms)
        return age is not None and age <= max_age_seconds

    def update_heartbeat(self, now_ms: int | None = None) -> int:
        now = now_ms if now_ms is not None else _now_ms()
        self.set_variable(HEARTBEAT_VAR, str(now))
        return now

    def fallback_lock_active(self, now_ms: int | None = None) -> bool:
        lock_until_ms = _parse_int(self.get_variable(LOCK_UNTIL_VAR), 0)
        now = now_ms if now_ms is not None else _now_ms()
        return lock_until_ms > now

    def acquire_fallback_if_needed(
        self,
        heartbeat_max_age_seconds: int,
        lock_ttl_seconds: int,
        now_ms: int | None = None,
    ) -> FallbackDecision:
        now = now_ms if now_ms is not None else _now_ms()
        if self.heartbeat_is_fresh(heartbeat_max_age_seconds, now):
            return FallbackDecision(False, True, False, "mac_heartbeat_fresh")

        lock_until_ms = _parse_int(self.get_variable(LOCK_UNTIL_VAR), 0)
        lock_owner = self.get_variable(LOCK_OWNER_VAR)
        if lock_until_ms > now and lock_owner and lock_owner != self.owner:
            return FallbackDecision(False, False, False, "fallback_lock_held")

        self.set_variable(LOCK_OWNER_VAR, self.owner)
        self.set_variable(LOCK_UNTIL_VAR, str(now + lock_ttl_seconds * 1000))
        return FallbackDecision(True, False, True, "fallback_lock_acquired")
