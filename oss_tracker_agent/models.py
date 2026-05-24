"""Pydantic models for OSS tracker agent."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """Actions the agent can decide to take on a PR."""

    NONE = "NONE"
    RERUN_FAILED_CHECKS = "RERUN_FAILED_CHECKS"
    UPDATE_BRANCH = "UPDATE_BRANCH"
    INVOKE_CODING_AGENT = "INVOKE_CODING_AGENT"
    # Quick click-button actions (all gated, default off, scoped via allowlist)
    ENABLE_AUTO_MERGE = "ENABLE_AUTO_MERGE"
    MARK_READY_FOR_REVIEW = "MARK_READY_FOR_REVIEW"
    APPROVE_WORKFLOW_RUN = "APPROVE_WORKFLOW_RUN"
    RESOLVE_REVIEW_THREADS = "RESOLVE_REVIEW_THREADS"
    DISMISS_STALE_REVIEW = "DISMISS_STALE_REVIEW"
    NOTIFY_HUMAN = "NOTIFY_HUMAN"


class ActionStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class CheckRun(BaseModel):
    name: str
    workflow_name: str = ""
    conclusion: Optional[str] = None
    status: Optional[str] = None
    details_url: Optional[str] = None


class Review(BaseModel):
    author: str
    state: str


class PRSnapshot(BaseModel):
    number: int
    repo: str
    title: str
    url: str
    mergeable: Optional[str] = None
    merge_state: Optional[str] = None
    review_decision: Optional[str] = None
    is_draft: bool = False
    head_repository_owner: Optional[str] = None
    age_days: int = 0
    checks: list[CheckRun] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)

    @property
    def failing_checks(self) -> list[CheckRun]:
        return [c for c in self.checks if c.conclusion == "FAILURE"]

    @property
    def is_fork_pr(self) -> bool:
        if not self.head_repository_owner:
            return False
        owner, _ = self.repo.split("/", 1)
        return self.head_repository_owner.lower() != owner.lower()


class DeepDiveAnalysis(BaseModel):
    """Per-PR root-cause analysis produced by the deep-dive sub-agent."""

    root_cause: str = ""
    suggested_action: str = ""
    confidence: str = "low"  # low | medium | high


class PRClassification(BaseModel):
    """LLM-produced classification of what to do with a PR."""

    pr_number: int
    repo: str
    recommended_actions: list[ActionType] = Field(default_factory=list)
    urgency: str = "low"  # low | medium | high
    needs_human: bool = False
    reasoning: str = ""
    deep_dive: Optional[DeepDiveAnalysis] = None


class ActionResult(BaseModel):
    pr_number: int
    repo: str
    action: ActionType
    status: ActionStatus
    detail: str = ""


class SelfReview(BaseModel):
    """LLM critic verdict on the drafter's final digest."""

    verdict: str = "approved"  # approved | concerns | broken
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class DailyReport(BaseModel):
    timestamp: datetime
    refresh_log_tail: str = ""
    snapshots: list[PRSnapshot] = Field(default_factory=list)
    classifications: list[PRClassification] = Field(default_factory=list)
    action_results: list[ActionResult] = Field(default_factory=list)
    digest_markdown: str = ""
    self_review: Optional[SelfReview] = None

    @property
    def human_attention_prs(self) -> list[PRClassification]:
        return [c for c in self.classifications if c.needs_human]

    @property
    def failed_actions(self) -> list[ActionResult]:
        return [r for r in self.action_results if r.status == ActionStatus.FAILED]

    @property
    def email_required(self) -> bool:
        return bool(self.human_attention_prs) or bool(self.failed_actions)
