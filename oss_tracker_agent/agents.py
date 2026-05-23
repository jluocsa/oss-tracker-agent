"""LLM agent factories: analyzer + digest drafter.

These are the two LLM agents in the multi-agent system. The deterministic
tracker, executor, and notifier are plain Python in tools.py.
"""

from __future__ import annotations

import os

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient


ANALYZER_INSTRUCTIONS = """You are an OSS pull-request triage analyst.

You receive a JSON array of PR snapshots. Each snapshot includes:
- number, repo, title, url
- mergeable (e.g. MERGEABLE, CONFLICTING, UNKNOWN)
- merge_state (e.g. CLEAN, BLOCKED, BEHIND, UNSTABLE, DIRTY)
- review_decision (APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED, null)
- is_draft (bool)
- age_days (int)
- checks: list of {name, workflow_name, conclusion, status, details_url}
- reviews: list of {author, state}
- is_fork_pr (bool) — when True, repository secrets are unavailable in CI

For EACH PR, output a classification object with these fields:
- pr_number, repo
- recommended_actions: subset of [RERUN_FAILED_CHECKS, UPDATE_BRANCH, NOTIFY_HUMAN, NONE]
- urgency: "low" | "medium" | "high"
- needs_human: bool
- reasoning: one sentence (≤120 chars)

DECISION RULES (apply in this order, be conservative):

1. RERUN_FAILED_CHECKS — only if ALL of:
   - PR has at least one failing check, AND
   - the failure pattern looks like infra/flake (e.g. timeout, network, runner image),
     NOT a real test/lint failure, AND
   - the PR is NOT from a fork OR the failing workflow does not depend on repo secrets.
   For graphrag specifically: "Python Smoke Tests" and "Python Notebook Tests" failing
   on fork PRs is a known secrets-gated false positive — DO NOT recommend rerun for those;
   instead set needs_human=False and reasoning=\"fork-PR secrets gap (informational)\".

2. UPDATE_BRANCH — recommend if merge_state == \"BEHIND\" AND mergeable != \"CONFLICTING\".

3. NOTIFY_HUMAN — recommend if ANY of:
   - review_decision == \"CHANGES_REQUESTED\"
   - mergeable == \"CONFLICTING\" or merge_state == \"DIRTY\"
   - age_days >= 7 AND review_decision == \"REVIEW_REQUIRED\" (stale awaiting-review)
   - non-secret-gated check failures that look like real test failures
   - any review with state == \"CHANGES_REQUESTED\" or unanswered comments

4. NONE — set this when nothing else fires (PR is healthy, awaiting review on its normal cadence).

URGENCY:
- high: merge conflicts, CHANGES_REQUESTED, security-sounding workflow names
- medium: real test failures, stale > 14 days
- low: everything else, including fork-secrets informational

OUTPUT FORMAT — strict JSON, no prose, no markdown fences:

[
  {
    \"pr_number\": 123,
    \"repo\": \"owner/name\",
    \"recommended_actions\": [\"UPDATE_BRANCH\"],
    \"urgency\": \"low\",
    \"needs_human\": false,
    \"reasoning\": \"behind main, mergeable; auto-updatable\"
  }
]
"""


DIGEST_INSTRUCTIONS = """You write a concise daily OSS digest email body in Markdown.

You receive a JSON object with two fields:
- classifications: list of PR triage decisions
- action_results: list of auto-actions taken and their outcomes

Produce a Markdown email body with EXACTLY these sections (omit a section if empty):

## Needs your attention
Bullet list. Each item: `- [#NUM](url) repo — title — reasoning (urgency)`
Sort high → medium → low urgency.

## Auto-actions taken
Bullet list. Each item: `- [#NUM](url) repo — ACTION_NAME — status — detail`
Group by status: success first, then failed.

## Quiet PRs
One line: `N PR(s) healthy, no action needed.` Only show if at least one classification has recommended_actions == [NONE] or [] AND needs_human == false.

Keep the whole digest under 60 lines. Do NOT include greetings or sign-offs. Output only the Markdown body, no JSON, no commentary.
"""


def make_analyzer_agent() -> Agent:
    """LLM agent that classifies PRs into action buckets."""
    return Agent(
        _make_client(),
        ANALYZER_INSTRUCTIONS,
        name="oss_pr_analyzer",
    )


def make_digest_drafter_agent() -> Agent:
    """LLM agent that writes the human-readable email digest."""
    return Agent(
        _make_client(),
        DIGEST_INSTRUCTIONS,
        name="oss_digest_drafter",
    )


def _make_client() -> OpenAIChatClient:
    """Construct an OpenAI or Azure OpenAI chat client based on env."""
    if os.environ.get("OPENAI_API_KEY"):
        model = os.environ.get("OPENAI_CHAT_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        return OpenAIChatClient(model=model)
    if os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_ENDPOINT"):
        model = (
            os.environ.get("AZURE_OPENAI_CHAT_MODEL")
            or os.environ.get("AZURE_OPENAI_MODEL")
            or "gpt-4o-mini"
        )
        return OpenAIChatClient(
            model=model,
            azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
            api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION"),
        )
    raise RuntimeError(
        "No model credentials configured. Set OPENAI_API_KEY or AZURE_OPENAI_API_KEY in .env"
    )
