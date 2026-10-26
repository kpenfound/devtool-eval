"""GitHub App plumbing: installation tokens scoped to one slot, and slot cleanup.

All automation goes through a GitHub App installed on a test org; there are no bot user
accounts. Never forks: an agent running ``gh pr create`` in a fork can target upstream.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import jwt

from harness.config import GitHubConfig

API = "https://api.github.com"


class GitHubApp:
    def __init__(self, cfg: GitHubConfig):
        if not cfg.enabled:
            raise RuntimeError("GitHub App is not configured (github.app_id/installation_id/key)")
        self.cfg = cfg
        self._key = Path(cfg.private_key_path).expanduser().read_text()  # type: ignore[arg-type]

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.cfg.app_id)}
        return jwt.encode(payload, self._key, algorithm="RS256")

    def installation_token(
        self, repos: list[str] | None = None, permissions: dict[str, str] | None = None
    ) -> str:
        """A token limited to ``repos`` (bare names in the org) and ``permissions``."""
        body: dict = {}
        if repos is not None:
            body["repositories"] = repos
        if permissions is not None:
            body["permissions"] = permissions
        r = httpx.post(
            f"{API}/app/installations/{self.cfg.installation_id}/access_tokens",
            headers=_headers(self._app_jwt()),
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["token"]

    def slot_token(self, repo: str) -> str:
        return self.installation_token([_bare(repo)], self.cfg.agent_permissions)

    def harness_token(self, repos: list[str] | None = None) -> str:
        return self.installation_token([_bare(r) for r in repos] if repos else None)


def revoke(token: str) -> None:
    r = httpx.delete(f"{API}/installation/token", headers=_headers(token), timeout=30)
    if r.status_code not in (204, 401):
        r.raise_for_status()


class Repo:
    """REST helpers for one repo, using a harness-side token."""

    def __init__(self, full_name: str, token: str):
        self.full_name = full_name
        self.client = httpx.Client(
            base_url=f"{API}/repos/{full_name}", headers=_headers(token), timeout=30
        )

    def _paged(self, path: str, **params) -> list[dict]:
        out, page = [], 1
        while True:
            r = self.client.get(path, params={**params, "per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def open_prs(self) -> list[dict]:
        return self._paged("/pulls", state="open")

    def close_pr(self, number: int) -> None:
        self.client.patch(f"/pulls/{number}", json={"state": "closed"}).raise_for_status()

    def branches(self) -> list[str]:
        return [b["name"] for b in self._paged("/branches")]

    def delete_branch(self, name: str) -> None:
        r = self.client.delete(f"/git/refs/heads/{name}")
        if r.status_code not in (204, 422):
            r.raise_for_status()

    def disable_actions(self) -> None:
        """Slots carry the upstream's workflows; they must never run."""
        self.client.put("/actions/permissions", json={"enabled": False}).raise_for_status()

    def default_branch(self) -> str:
        r = self.client.get("")
        r.raise_for_status()
        return r.json()["default_branch"]

    def commit_statuses(self, sha: str) -> dict:
        checks = self.client.get(f"/commits/{sha}/check-runs")
        checks.raise_for_status()
        status = self.client.get(f"/commits/{sha}/status")
        status.raise_for_status()
        return {"check_runs": checks.json().get("check_runs", []), "status": status.json()}


def create_private_repo(org: str, name: str, token: str, description: str = "") -> None:
    r = httpx.post(
        f"{API}/orgs/{org}/repos",
        headers=_headers(token),
        json={"name": name, "private": True, "description": description, "auto_init": False},
        timeout=30,
    )
    if r.status_code == 422 and "already exists" in r.text:
        return
    r.raise_for_status()


def _bare(repo: str) -> str:
    return repo.split("/", 1)[-1]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
