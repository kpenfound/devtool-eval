"""GitHub REST helpers against a mock transport."""

from __future__ import annotations

import httpx
import pytest

from harness import github


def repo_with(handler) -> github.Repo:
    r = github.Repo("org/slot-01", "t")
    r.client = httpx.Client(base_url="https://api.github.com/repos/org/slot-01",
                            transport=httpx.MockTransport(handler))
    return r


def test_close_pr_tolerates_already_closed():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PATCH":
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(200, json={"number": 9, "state": "closed"})

    repo_with(handler).close_pr(9)


def test_close_pr_raises_when_still_open():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PATCH":
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(200, json={"number": 9, "state": "open"})

    with pytest.raises(RuntimeError, match="Validation Failed"):
        repo_with(handler).close_pr(9)
