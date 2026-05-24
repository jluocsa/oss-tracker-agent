"""Deterministic Python tools: gh CLI wrappers, refresh script runner, SMTP sender."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from .models import (
    ActionResult,
    ActionStatus,
    ActionType,
    CheckRun,
    PRSnapshot,
    Review,
)

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    logger.debug("RUN: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _powershell_exe() -> str | None:
    """Return the first available PowerShell executable, or None."""
    for candidate in ("pwsh", "pwsh.exe", "powershell.exe"):
        if shutil.which(candidate):
            return candidate
    return None


def refresh_tracker(refresh_script: Path) -> str:
    """Invoke refresh-oss-status.ps1 via PowerShell. Returns tail of stdout."""
    if not str(refresh_script).strip():
        return "[skipped] REFRESH_SCRIPT not set"
    if not refresh_script.is_file():
        return f"[skipped] refresh script not found: {refresh_script}"
    ps = _powershell_exe()
    if not ps:
        return "[skipped] no powershell/pwsh on PATH"
    proc = _run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(refresh_script)],
        cwd=refresh_script.parent,
        timeout=300,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-10:])
    if proc.returncode != 0:
        return f"[exit={proc.returncode}]\n{tail}"
    return tail


def _gh_json(args: list[str], timeout: int = 60) -> Any:
    """Run a gh command that emits JSON. Returns parsed JSON or {} on failure."""
    proc = _run(["gh", *args], timeout=timeout)
    if proc.returncode != 0:
        logger.warning("gh %s failed: %s", " ".join(args), proc.stderr.strip()[:400])
        return {}
    try:
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        logger.warning("gh JSON parse error: %s", exc)
        return {}


def fetch_open_prs(author: str, ignore_repos: set[str]) -> list[PRSnapshot]:
    """List open PRs by author and enrich each with `gh pr view` detail."""
    search = _gh_json(
        [
            "search",
            "prs",
            "--author",
            author,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "repository,number,title,url,createdAt",
        ]
    )
    if not isinstance(search, list):
        return []

    snapshots: list[PRSnapshot] = []
    for item in search:
        repo = item.get("repository", {}).get("nameWithOwner", "")
        if not repo or repo in ignore_repos:
            continue
        number = item.get("number")
        if not number:
            continue

        detail = _gh_json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,url,title,mergeable,mergeStateStatus,reviewDecision,isDraft,headRepositoryOwner,statusCheckRollup,reviews,createdAt",
            ]
        )
        if not isinstance(detail, dict) or not detail:
            continue

        checks_raw = detail.get("statusCheckRollup") or []
        checks: list[CheckRun] = []
        for c in checks_raw:
            checks.append(
                CheckRun(
                    name=c.get("name") or "",
                    workflow_name=c.get("workflowName") or "",
                    conclusion=c.get("conclusion"),
                    status=c.get("status"),
                    details_url=c.get("detailsUrl"),
                )
            )

        reviews_raw = detail.get("reviews") or []
        reviews: list[Review] = []
        for r in reviews_raw:
            reviews.append(
                Review(
                    author=(r.get("author") or {}).get("login", ""),
                    state=r.get("state", ""),
                )
            )

        created_at_str = detail.get("createdAt") or item.get("createdAt") or ""
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_days = max(0, (datetime.now(timezone.utc) - created_at).days)
        except (ValueError, AttributeError):
            age_days = 0

        head_owner_obj = detail.get("headRepositoryOwner") or {}
        head_owner = head_owner_obj.get("login") if isinstance(head_owner_obj, dict) else None

        snapshots.append(
            PRSnapshot(
                number=number,
                repo=repo,
                title=detail.get("title") or "",
                url=detail.get("url") or "",
                mergeable=detail.get("mergeable"),
                merge_state=detail.get("mergeStateStatus"),
                review_decision=detail.get("reviewDecision"),
                is_draft=bool(detail.get("isDraft")),
                head_repository_owner=head_owner,
                age_days=age_days,
                checks=checks,
                reviews=reviews,
            )
        )

    return snapshots


def rerun_failed_checks(pr: PRSnapshot) -> ActionResult:
    """Re-run failed workflow runs on the PR via `gh run rerun --failed`."""
    if not pr.failing_checks:
        return ActionResult(
            pr_number=pr.number,
            repo=pr.repo,
            action=ActionType.RERUN_FAILED_CHECKS,
            status=ActionStatus.SKIPPED,
            detail="no failing checks",
        )

    run_ids: set[str] = set()
    for c in pr.failing_checks:
        if not c.details_url:
            continue
        parts = c.details_url.rstrip("/").split("/")
        if "runs" in parts:
            idx = parts.index("runs")
            if idx + 1 < len(parts):
                run_ids.add(parts[idx + 1])

    if not run_ids:
        return ActionResult(
            pr_number=pr.number,
            repo=pr.repo,
            action=ActionType.RERUN_FAILED_CHECKS,
            status=ActionStatus.SKIPPED,
            detail="failing checks have no actions run_id",
        )

    succeeded: list[str] = []
    failed: list[str] = []
    for run_id in run_ids:
        proc = _run(["gh", "run", "rerun", run_id, "--failed", "--repo", pr.repo])
        if proc.returncode == 0:
            succeeded.append(run_id)
        else:
            failed.append(f"{run_id}:{proc.stderr.strip()[:120]}")

    if failed and not succeeded:
        return ActionResult(
            pr_number=pr.number,
            repo=pr.repo,
            action=ActionType.RERUN_FAILED_CHECKS,
            status=ActionStatus.FAILED,
            detail="; ".join(failed),
        )
    return ActionResult(
        pr_number=pr.number,
        repo=pr.repo,
        action=ActionType.RERUN_FAILED_CHECKS,
        status=ActionStatus.SUCCESS,
        detail=f"reran {len(succeeded)} run(s): {','.join(succeeded)}"
        + (f"; failed: {';'.join(failed)}" if failed else ""),
    )


def update_branch(pr: PRSnapshot) -> ActionResult:
    """Update the PR branch with upstream main via `gh pr update-branch`."""
    proc = _run(["gh", "pr", "update-branch", str(pr.number), "--repo", pr.repo])
    if proc.returncode == 0:
        return ActionResult(
            pr_number=pr.number,
            repo=pr.repo,
            action=ActionType.UPDATE_BRANCH,
            status=ActionStatus.SUCCESS,
            detail=proc.stdout.strip()[:200] or "branch updated",
        )
    return ActionResult(
        pr_number=pr.number,
        repo=pr.repo,
        action=ActionType.UPDATE_BRANCH,
        status=ActionStatus.FAILED,
        detail=proc.stderr.strip()[:300],
    )


def fetch_failed_check_logs(
    pr: PRSnapshot,
    *,
    max_checks: int = 3,
    tail_chars: int = 2000,
) -> list[dict[str, str]]:
    """Pull tail of `gh run view --log-failed` for up to N failing checks on the PR.

    Returns a list of {check_name, workflow_name, log_tail}; empty if the PR has
    no failing checks or none expose a workflow run_id we can dereference.
    """
    if not pr.failing_checks:
        return []

    seen_runs: set[str] = set()
    results: list[dict[str, str]] = []
    for c in pr.failing_checks:
        if len(results) >= max_checks:
            break
        if not c.details_url:
            continue
        parts = c.details_url.rstrip("/").split("/")
        if "runs" not in parts:
            continue
        idx = parts.index("runs")
        if idx + 1 >= len(parts):
            continue
        run_id = parts[idx + 1]
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        proc = _run(
            ["gh", "run", "view", run_id, "--repo", pr.repo, "--log-failed"],
            timeout=60,
        )
        if proc.returncode != 0:
            logger.debug(
                "gh run view --log-failed %s on %s failed: %s",
                run_id, pr.repo, proc.stderr.strip()[:200],
            )
            continue
        log = (proc.stdout or "").strip()
        if not log:
            continue
        if len(log) > tail_chars:
            log = log[-tail_chars:]
        results.append(
            {
                "check_name": c.name,
                "workflow_name": c.workflow_name,
                "run_id": run_id,
                "log_tail": log,
            }
        )
    return results


def repo_matches_allowlist(repo: str, allowlist: list[str]) -> bool:
    """True if repo matches any fnmatch pattern in allowlist (e.g. 'jluocsa/*')."""
    for pat in allowlist:
        pat = pat.strip()
        if pat and fnmatch.fnmatch(repo, pat):
            return True
    return False


def enable_auto_merge(pr: PRSnapshot, method: str = "squash") -> ActionResult:
    """Enable auto-merge on a PR via `gh pr merge --auto`. Fires when branch protections allow."""
    if pr.is_draft:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.ENABLE_AUTO_MERGE,
            status=ActionStatus.SKIPPED,
            detail="PR is draft",
        )
    method_flag = f"--{method}"
    proc = _run(
        ["gh", "pr", "merge", str(pr.number), "--repo", pr.repo, "--auto", method_flag],
        timeout=30,
    )
    if proc.returncode == 0:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.ENABLE_AUTO_MERGE,
            status=ActionStatus.SUCCESS,
            detail=(proc.stdout.strip() or "auto-merge enabled")[:200],
        )
    msg = proc.stderr.strip()[:300]
    # "already enabled" / "clean merge possible" should not surface as failures
    lower = msg.lower()
    if "already" in lower and "auto" in lower:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.ENABLE_AUTO_MERGE,
            status=ActionStatus.SKIPPED,
            detail="auto-merge already enabled",
        )
    return ActionResult(
        pr_number=pr.number, repo=pr.repo,
        action=ActionType.ENABLE_AUTO_MERGE,
        status=ActionStatus.FAILED,
        detail=msg,
    )


def mark_ready_for_review(pr: PRSnapshot) -> ActionResult:
    """Convert a draft PR to ready for review via `gh pr ready`."""
    if not pr.is_draft:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.MARK_READY_FOR_REVIEW,
            status=ActionStatus.SKIPPED,
            detail="PR is not draft",
        )
    proc = _run(["gh", "pr", "ready", str(pr.number), "--repo", pr.repo], timeout=30)
    if proc.returncode == 0:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.MARK_READY_FOR_REVIEW,
            status=ActionStatus.SUCCESS,
            detail="draft \u2192 ready for review",
        )
    return ActionResult(
        pr_number=pr.number, repo=pr.repo,
        action=ActionType.MARK_READY_FOR_REVIEW,
        status=ActionStatus.FAILED,
        detail=proc.stderr.strip()[:300],
    )


def approve_pending_workflow_runs(pr: PRSnapshot) -> ActionResult:
    """Approve any `action_required` workflow runs attached to this PR.

    Fires when GitHub gates a first-time contributor's workflow on maintainer approval.
    Does nothing if no such runs exist.
    """
    proc = _run(
        [
            "gh", "api", f"repos/{pr.repo}/actions/runs",
            "--paginate",
            "-q",
            f".workflow_runs[] | select(.status==\"action_required\") | select(any(.pull_requests[]?; .number=={pr.number})) | .id",
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.APPROVE_WORKFLOW_RUN,
            status=ActionStatus.FAILED,
            detail=proc.stderr.strip()[:300],
        )
    ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not ids:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.APPROVE_WORKFLOW_RUN,
            status=ActionStatus.SKIPPED,
            detail="no action_required workflow runs",
        )
    approved: list[str] = []
    failed: list[str] = []
    for run_id in ids:
        p = _run(
            ["gh", "api", "-X", "POST", f"repos/{pr.repo}/actions/runs/{run_id}/approve"],
            timeout=30,
        )
        if p.returncode == 0:
            approved.append(run_id)
        else:
            failed.append(f"{run_id}:{p.stderr.strip()[:80]}")
    if approved and not failed:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.APPROVE_WORKFLOW_RUN,
            status=ActionStatus.SUCCESS,
            detail=f"approved {len(approved)} run(s): {','.join(approved)}",
        )
    if approved:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.APPROVE_WORKFLOW_RUN,
            status=ActionStatus.SUCCESS,
            detail=f"approved {len(approved)}; failed {len(failed)}: {';'.join(failed)}",
        )
    return ActionResult(
        pr_number=pr.number, repo=pr.repo,
        action=ActionType.APPROVE_WORKFLOW_RUN,
        status=ActionStatus.FAILED,
        detail="; ".join(failed)[:300],
    )


def resolve_review_threads(pr: PRSnapshot) -> ActionResult:
    """Resolve unresolved review threads where you (viewer) authored the comment and
    the PR author has pushed a commit since.
    """
    owner, name = pr.repo.split("/", 1)
    query = (
        "query($owner: String!, $name: String!, $number: Int!) {"
        "  viewer { login }"
        "  repository(owner: $owner, name: $name) {"
        "    pullRequest(number: $number) {"
        "      commits(last: 50) { nodes { commit { committedDate } } }"
        "      reviewThreads(first: 100) {"
        "        nodes {"
        "          id isResolved"
        "          comments(first: 1) { nodes { author { login } createdAt } }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    proc = _run(
        [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={pr.number}",
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.RESOLVE_REVIEW_THREADS,
            status=ActionStatus.FAILED,
            detail=proc.stderr.strip()[:300],
        )
    try:
        data = json.loads(proc.stdout).get("data", {})
    except json.JSONDecodeError:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.RESOLVE_REVIEW_THREADS,
            status=ActionStatus.FAILED,
            detail="graphql JSON parse failed",
        )
    viewer = (data.get("viewer") or {}).get("login", "")
    pull = (data.get("repository") or {}).get("pullRequest") or {}
    commits = (pull.get("commits") or {}).get("nodes") or []
    last_commit_at = commits[-1].get("commit", {}).get("committedDate") if commits else None
    threads = (pull.get("reviewThreads") or {}).get("nodes") or []
    to_resolve: list[str] = []
    for t in threads:
        if t.get("isResolved"):
            continue
        comments = (t.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        first = comments[0]
        if (first.get("author") or {}).get("login") != viewer:
            continue
        thread_at = first.get("createdAt") or ""
        if not last_commit_at or not thread_at or last_commit_at <= thread_at:
            continue
        to_resolve.append(t["id"])
    if not to_resolve:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.RESOLVE_REVIEW_THREADS,
            status=ActionStatus.SKIPPED,
            detail="no eligible threads to resolve",
        )
    mutation = (
        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) {"
        " thread { id isResolved } } }"
    )
    resolved: list[str] = []
    failed: list[str] = []
    for tid in to_resolve:
        p = _run(
            ["gh", "api", "graphql", "-f", f"query={mutation}", "-F", f"id={tid}"],
            timeout=30,
        )
        if p.returncode == 0:
            resolved.append(tid)
        else:
            failed.append(f"{tid[:12]}:{p.stderr.strip()[:80]}")
    if resolved and not failed:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.RESOLVE_REVIEW_THREADS,
            status=ActionStatus.SUCCESS,
            detail=f"resolved {len(resolved)} thread(s)",
        )
    if resolved:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.RESOLVE_REVIEW_THREADS,
            status=ActionStatus.SUCCESS,
            detail=f"resolved={len(resolved)} failed={len(failed)}",
        )
    return ActionResult(
        pr_number=pr.number, repo=pr.repo,
        action=ActionType.RESOLVE_REVIEW_THREADS,
        status=ActionStatus.FAILED,
        detail="; ".join(failed)[:300],
    )


def dismiss_stale_review(pr: PRSnapshot) -> ActionResult:
    """Dismiss reviews by you on this PR if the author has pushed since.

    Useful when you reviewed someone else's PR and they addressed your feedback.
    No-op on PRs you authored yourself (you can't review your own PR).
    """
    owner, name = pr.repo.split("/", 1)
    query = (
        "query($owner: String!, $name: String!, $number: Int!) {"
        "  viewer { login }"
        "  repository(owner: $owner, name: $name) {"
        "    pullRequest(number: $number) {"
        "      commits(last: 50) { nodes { commit { committedDate } } }"
        "      reviews(first: 50) {"
        "        nodes { id state submittedAt author { login } }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    proc = _run(
        [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={pr.number}",
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.DISMISS_STALE_REVIEW,
            status=ActionStatus.FAILED,
            detail=proc.stderr.strip()[:300],
        )
    try:
        data = json.loads(proc.stdout).get("data", {})
    except json.JSONDecodeError:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.DISMISS_STALE_REVIEW,
            status=ActionStatus.FAILED,
            detail="graphql JSON parse failed",
        )
    viewer = (data.get("viewer") or {}).get("login", "")
    pull = (data.get("repository") or {}).get("pullRequest") or {}
    commits = (pull.get("commits") or {}).get("nodes") or []
    last_commit_at = commits[-1].get("commit", {}).get("committedDate") if commits else None
    reviews = (pull.get("reviews") or {}).get("nodes") or []
    candidates = [
        r for r in reviews
        if (r.get("author") or {}).get("login") == viewer
        and r.get("state") in {"CHANGES_REQUESTED", "APPROVED"}
        and last_commit_at and r.get("submittedAt") and r["submittedAt"] < last_commit_at
    ]
    if not candidates:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.DISMISS_STALE_REVIEW,
            status=ActionStatus.SKIPPED,
            detail="no stale reviews by you",
        )
    mutation = (
        "mutation($id: ID!, $msg: String!) {"
        "  dismissPullRequestReview(input: {pullRequestReviewId: $id, message: $msg}) {"
        "    pullRequestReview { id state }"
        "  }"
        "}"
    )
    dismissed: list[str] = []
    failed: list[str] = []
    for r in candidates:
        p = _run(
            [
                "gh", "api", "graphql",
                "-f", f"query={mutation}",
                "-F", f"id={r['id']}",
                "-F", "msg=Auto-dismissed by oss-tracker-agent: author pushed new commits since this review.",
            ],
            timeout=30,
        )
        if p.returncode == 0:
            dismissed.append(r["id"][:12])
        else:
            failed.append(f"{r['id'][:12]}:{p.stderr.strip()[:80]}")
    if dismissed and not failed:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.DISMISS_STALE_REVIEW,
            status=ActionStatus.SUCCESS,
            detail=f"dismissed {len(dismissed)} stale review(s)",
        )
    if dismissed:
        return ActionResult(
            pr_number=pr.number, repo=pr.repo,
            action=ActionType.DISMISS_STALE_REVIEW,
            status=ActionStatus.SUCCESS,
            detail=f"dismissed={len(dismissed)} failed={len(failed)}",
        )
    return ActionResult(
        pr_number=pr.number, repo=pr.repo,
        action=ActionType.DISMISS_STALE_REVIEW,
        status=ActionStatus.FAILED,
        detail="; ".join(failed)[:300],
    )


def dispatch_copilot_agent(pr: PRSnapshot, prompt: str) -> ActionResult:
    """Post `@copilot <prompt>` as a PR comment to invoke GitHub's coding agent.

    The agent's model selection is governed by the *target repo's* Copilot
    settings, not by this dispatch. For Copilot Pro+/Business/Enterprise, the
    repo admin can pin a model at:
        https://github.com/<owner>/<repo>/settings/copilot/coding_agent
    The default at time of writing is Claude Sonnet 4.5.
    """
    body = f"@copilot {prompt}".strip()
    proc = _run(
        ["gh", "pr", "comment", str(pr.number), "--repo", pr.repo, "--body", body],
        timeout=30,
    )
    if proc.returncode == 0:
        return ActionResult(
            pr_number=pr.number,
            repo=pr.repo,
            action=ActionType.INVOKE_CODING_AGENT,
            status=ActionStatus.SUCCESS,
            detail=(proc.stdout.strip() or "dispatched — see PR for agent response")[:200],
        )
    return ActionResult(
        pr_number=pr.number,
        repo=pr.repo,
        action=ActionType.INVOKE_CODING_AGENT,
        status=ActionStatus.FAILED,
        detail=proc.stderr.strip()[:300],
    )


def send_email_smtp(
    subject: str,
    body_markdown: str,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    email_from: str,
    email_to: str,
    dry_run: bool = False,
) -> ActionResult:
    """Send the digest email via STARTTLS SMTP."""
    if dry_run:
        return ActionResult(
            pr_number=0,
            repo="-",
            action=ActionType.NOTIFY_HUMAN,
            status=ActionStatus.SKIPPED,
            detail=f"dry-run; would send to {email_to} (subject: {subject})",
        )

    if not smtp_password or smtp_password.startswith("<"):
        return ActionResult(
            pr_number=0,
            repo="-",
            action=ActionType.NOTIFY_HUMAN,
            status=ActionStatus.SKIPPED,
            detail="SMTP_PASSWORD not configured",
        )

    html_body = _markdown_to_html(body_markdown)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(body_markdown, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_username, smtp_password)
            server.sendmail(email_from, [email_to], msg.as_string())
        return ActionResult(
            pr_number=0,
            repo="-",
            action=ActionType.NOTIFY_HUMAN,
            status=ActionStatus.SUCCESS,
            detail=f"sent to {email_to}",
        )
    except Exception as exc:  # noqa: BLE001 — surface any SMTP failure
        return ActionResult(
            pr_number=0,
            repo="-",
            action=ActionType.NOTIFY_HUMAN,
            status=ActionStatus.FAILED,
            detail=f"{type(exc).__name__}: {exc}"[:400],
        )


def _markdown_to_html(md: str) -> str:
    """Lightweight markdown -> HTML for the email body (no external deps)."""
    lines = md.splitlines()
    out: list[str] = ["<html><body style='font-family: -apple-system, Segoe UI, sans-serif; font-size: 14px;'>"]
    in_list = False
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
        elif not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(text: str) -> str:
    import re

    # links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # bold **text**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    # inline code `x`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def load_config_from_env() -> dict[str, str]:
    """Read all relevant env vars in one shot."""
    # In CI (GitHub Actions sets CI=true), default to skipping the local refresh script
    # regardless of platform. On a local Windows dev box, fall back to the user's known path.
    is_ci = os.environ.get("CI", "").lower() == "true"
    if is_ci:
        platform_default = ""
    elif sys.platform == "win32":
        platform_default = r"C:\Users\luojohn\github\jluocsa\refresh-oss-status.ps1"
    else:
        platform_default = ""
    default_refresh = os.environ.get("REFRESH_SCRIPT", platform_default)
    return {
        "refresh_script": default_refresh,
        "gh_author": os.environ.get("GH_AUTHOR", "jluocsa"),
        "ignore_repos": os.environ.get("IGNORE_REPOS", "jluocsa/Practice-Exam"),
        "auto_rerun": os.environ.get("AUTO_RERUN_FAILED_CHECKS", "true"),
        "auto_update_branch": os.environ.get("AUTO_UPDATE_BRANCH", "true"),
        "deep_dive_enabled": os.environ.get("DEEP_DIVE_ENABLED", "true"),
        "deep_dive_max_prs": os.environ.get("DEEP_DIVE_MAX_PRS", "3"),
        "self_review_enabled": os.environ.get("SELF_REVIEW_ENABLED", "true"),
        "copilot_agent_enabled": os.environ.get("AUTO_INVOKE_COPILOT_AGENT", "false"),
        "copilot_agent_allowlist": os.environ.get("COPILOT_AGENT_REPO_ALLOWLIST", "jluocsa/*"),
        "copilot_agent_min_confidence": os.environ.get("COPILOT_AGENT_MIN_CONFIDENCE", "high"),
        "copilot_agent_max_dispatches": os.environ.get("COPILOT_AGENT_MAX_DISPATCHES_PER_RUN", "2"),
        # Quick click-button actions (all default off, all share the same allowlist)
        "quick_actions_allowlist": os.environ.get("QUICK_ACTIONS_REPO_ALLOWLIST", "jluocsa/*"),
        "auto_enable_auto_merge": os.environ.get("AUTO_ENABLE_AUTO_MERGE", "false"),
        "auto_merge_method": os.environ.get("AUTO_MERGE_METHOD", "squash"),
        "auto_mark_ready": os.environ.get("AUTO_MARK_READY_FOR_REVIEW", "false"),
        "auto_approve_workflow_run": os.environ.get("AUTO_APPROVE_WORKFLOW_RUN", "false"),
        "auto_resolve_threads": os.environ.get("AUTO_RESOLVE_REVIEW_THREADS", "false"),
        "auto_dismiss_stale_review": os.environ.get("AUTO_DISMISS_STALE_REVIEWS", "false"),
        "email_from": os.environ.get("NOTIFY_EMAIL_FROM", ""),
        "email_to": os.environ.get("NOTIFY_EMAIL_TO", ""),
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.office365.com"),
        "smtp_port": os.environ.get("SMTP_PORT", "587"),
        "smtp_user": os.environ.get("SMTP_USERNAME", ""),
        "smtp_pass": os.environ.get("SMTP_PASSWORD", ""),
        "email_dry_run": os.environ.get("EMAIL_DRY_RUN", "false"),
    }
