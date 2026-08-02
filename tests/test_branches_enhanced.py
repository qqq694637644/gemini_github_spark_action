from __future__ import annotations

import asyncio

import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.models.branches import CreateWorkBranchRequest
from app.policy.rules import Policy
from app.services.branches import BranchService

MAIN_SHA = "1111111111111111111111111111111111111111"
SPARK_SHA = "2222222222222222222222222222222222222222"


class BranchGitHubStub:
    def __init__(self) -> None:
        self.created_refs: list[tuple[str, str]] = []

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        if branch == "spark/existing":
            return SPARK_SHA
        return MAIN_SHA

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict:
        self.created_refs.append((branch, sha))
        if branch == "spark/existing":
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "already exists", status_code=409)
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict:
        return {"name": branch, "commit": {"sha": SPARK_SHA, "url": "https://api.github.test/commit"}, "protected": False}


def make_service() -> BranchService:
    settings = Settings(gateway_action_secret="secret", allowed_repos="acme/demo")
    return BranchService(BranchGitHubStub(), Policy(settings), settings)


def test_create_work_branch_can_continue_existing_named_branch() -> None:
    service = make_service()
    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref="main", branch="spark/existing", purpose_slug="fix", continue_if_exists=True),
        )
    )

    assert response.branch == "spark/existing"
    assert response.base_sha == MAIN_SHA
    assert response.head_sha == SPARK_SHA
    assert response.already_exists is True


def test_create_work_branch_can_use_existing_spark_branch_as_base() -> None:
    service = make_service()

    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref="spark/existing", branch="spark/child", purpose_slug="follow-up"),
        )
    )

    assert response.branch == "spark/child"
    assert response.base_ref == "spark/existing"
    assert response.base_sha == SPARK_SHA
    assert response.head_sha == SPARK_SHA


def test_create_work_branch_can_use_arbitrary_existing_branch_as_base() -> None:
    service = make_service()

    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref="feature/parent", branch="spark/from-feature", purpose_slug="follow-up"),
        )
    )

    assert response.branch == "spark/from-feature"
    assert response.base_ref == "feature/parent"
    assert response.base_sha == MAIN_SHA
    assert response.head_sha == MAIN_SHA


def test_create_work_branch_rejects_unprefixed_explicit_branch() -> None:
    service = make_service()

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.create_work_branch(
                "acme",
                "demo",
                CreateWorkBranchRequest(base_ref="main", branch="feature/direct-maintenance", purpose_slug="follow-up"),
            )
        )

    assert exc.value.error_code == ErrorCode.BRANCH_NOT_ALLOWED


def test_create_work_branch_rejects_default_branch_even_when_continue_is_enabled() -> None:
    service = make_service()

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.create_work_branch(
                "acme",
                "demo",
                CreateWorkBranchRequest(base_ref="main", branch="main", continue_if_exists=True),
            )
        )

    assert exc.value.error_code == ErrorCode.BRANCH_NOT_ALLOWED


def test_create_work_branch_rejects_explicit_empty_branch() -> None:
    service = make_service()

    with pytest.raises(ApiError) as exc:
        asyncio.run(
            service.create_work_branch(
                "acme",
                "demo",
                CreateWorkBranchRequest(base_ref="main", branch="", purpose_slug="empty-branch"),
            )
        )

    assert exc.value.error_code == ErrorCode.BRANCH_NOT_ALLOWED


def test_create_work_branch_auto_generates_only_when_branch_is_none() -> None:
    service = make_service()

    response = asyncio.run(
        service.create_work_branch(
            "acme",
            "demo",
            CreateWorkBranchRequest(base_ref="main", branch=None, purpose_slug="auto branch"),
        )
    )

    assert response.branch.startswith("spark/auto-branch-")
    assert response.base_ref == "main"
    assert response.base_sha == MAIN_SHA
