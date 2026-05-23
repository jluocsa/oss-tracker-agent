"""OSS Tracker Agent entrypoint.

Pipeline (run daily, no LLM agents needed for plumbing):
1. Refresh OSS-STATUS.md via the existing PowerShell script.
2. Snapshot all open PRs via `gh`.
3. ANALYZER AGENT classifies each PR -> recommended actions + needs_human.
4. Executor runs auto-actions (rerun flaky checks, update branch).
5. DIGEST DRAFTER AGENT writes the email body.
6. If any PR needs human attention OR any action failed, send the email.

Usage:
    python -m oss_tracker_agent.main --cli       # one-shot (Task Scheduler)
    python -m oss_tracker_agent.main --server    # agentdev server mode

Env: see .env.template.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .agents import make_analyzer_agent, make_digest_drafter_agent
from .models import (
    ActionResult,
    ActionStatus,
    ActionType,
    DailyReport,
    PRClassification,
    PRSnapshot,
)
from .tools import (
    fetch_open_prs,
    load_config_from_env,
    refresh_tracker,
    rerun_failed_checks,
    send_email_smtp,
    update_branch,
)

logger = logging.getLogger("oss_tracker_agent")


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _snapshot_for_llm(snap: PRSnapshot) -> dict:
    """Trim a snapshot to the JSON shape the analyzer sees."""
    return {
        "pr_number": snap.number,
        "repo": snap.repo,
        "title": snap.title,
        "url": snap.url,
        "mergeable": snap.mergeable,
        "merge_state": snap.merge_state,
        "review_decision": snap.review_decision,
        "is_draft": snap.is_draft,
        "age_days": snap.age_days,
        "is_fork_pr": snap.is_fork_pr,
        "checks": [
            {
                "name": c.name,
                "workflow_name": c.workflow_name,
                "conclusion": c.conclusion,
                "status": c.status,
            }
            for c in snap.checks
        ],
        "reviews": [{"author": r.author, "state": r.state} for r in snap.reviews],
    }


def _extract_response_text(response) -> str:
    """Get plain text out of an AgentResponse regardless of content shape."""
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()
    messages = getattr(response, "messages", None) or []
    chunks: list[str] = []
    for m in messages:
        for c in getattr(m, "contents", None) or getattr(m, "content", None) or []:
            t = getattr(c, "text", None)
            if t:
                chunks.append(t)
    return "\n".join(chunks).strip()


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _parse_classifications(raw: str, snaps: list[PRSnapshot]) -> list[PRClassification]:
    """Coerce the analyzer's JSON into PRClassification objects with fallbacks."""
    try:
        parsed = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError as exc:
        logger.warning("analyzer JSON parse failed: %s; raw=%s", exc, raw[:400])
        # Fallback: treat every PR as needing human attention
        return [
            PRClassification(
                pr_number=s.number,
                repo=s.repo,
                recommended_actions=[ActionType.NOTIFY_HUMAN],
                urgency="medium",
                needs_human=True,
                reasoning="analyzer output unparseable",
            )
            for s in snaps
        ]

    if not isinstance(parsed, list):
        parsed = [parsed]

    classifications: list[PRClassification] = []
    for item in parsed:
        try:
            actions_raw = item.get("recommended_actions") or []
            actions: list[ActionType] = []
            for a in actions_raw:
                try:
                    actions.append(ActionType(a))
                except ValueError:
                    logger.debug("unknown action from analyzer: %s", a)
            classifications.append(
                PRClassification(
                    pr_number=int(item["pr_number"]),
                    repo=str(item["repo"]),
                    recommended_actions=actions,
                    urgency=str(item.get("urgency", "low")),
                    needs_human=bool(item.get("needs_human", False)),
                    reasoning=str(item.get("reasoning", "")),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("classification item skipped: %s; item=%s", exc, item)
    return classifications


def _execute_actions(
    snaps: list[PRSnapshot],
    classifications: list[PRClassification],
    *,
    auto_rerun: bool,
    auto_update_branch: bool,
) -> list[ActionResult]:
    by_key = {(s.repo, s.number): s for s in snaps}
    results: list[ActionResult] = []
    for cls in classifications:
        snap = by_key.get((cls.repo, cls.pr_number))
        if snap is None:
            continue
        for action in cls.recommended_actions:
            if action == ActionType.RERUN_FAILED_CHECKS:
                if not auto_rerun:
                    results.append(
                        ActionResult(
                            pr_number=snap.number,
                            repo=snap.repo,
                            action=action,
                            status=ActionStatus.SKIPPED,
                            detail="AUTO_RERUN_FAILED_CHECKS=false",
                        )
                    )
                    continue
                results.append(rerun_failed_checks(snap))
            elif action == ActionType.UPDATE_BRANCH:
                if not auto_update_branch:
                    results.append(
                        ActionResult(
                            pr_number=snap.number,
                            repo=snap.repo,
                            action=action,
                            status=ActionStatus.SKIPPED,
                            detail="AUTO_UPDATE_BRANCH=false",
                        )
                    )
                    continue
                results.append(update_branch(snap))
            # NOTIFY_HUMAN / NONE are not executed here
    return results


async def run_daily(verbose: bool = False) -> DailyReport:
    """Run one full daily pass and return the report."""
    cfg = load_config_from_env()

    logger.info("step 1/6: refresh tracker")
    refresh_path_str = cfg["refresh_script"].strip()
    if not refresh_path_str:
        refresh_log = "[skipped] REFRESH_SCRIPT not set"
    else:
        refresh_log = refresh_tracker(Path(refresh_path_str))
    logger.info("refresh tail: %s", refresh_log.replace("\n", " | ")[:300])

    logger.info("step 2/6: fetch open PRs (author=%s)", cfg["gh_author"])
    ignore = {r.strip() for r in cfg["ignore_repos"].split(",") if r.strip()}
    snaps = fetch_open_prs(cfg["gh_author"], ignore)
    logger.info("snapshots: %d PR(s)", len(snaps))

    report = DailyReport(
        timestamp=datetime.now(timezone.utc),
        refresh_log_tail=refresh_log,
        snapshots=snaps,
    )

    if not snaps:
        logger.info("no open PRs — nothing to triage")
        return report

    logger.info("step 3/6: analyzer agent classifies %d PR(s)", len(snaps))
    analyzer = make_analyzer_agent()
    analyzer_input = json.dumps([_snapshot_for_llm(s) for s in snaps], indent=2)
    analyzer_response = await analyzer.run(analyzer_input)
    analyzer_text = _extract_response_text(analyzer_response)
    if verbose:
        logger.debug("analyzer raw output:\n%s", analyzer_text)
    classifications = _parse_classifications(analyzer_text, snaps)
    report.classifications = classifications
    logger.info(
        "classifications: %d (human-attention: %d)",
        len(classifications),
        sum(1 for c in classifications if c.needs_human),
    )

    logger.info("step 4/6: execute auto-actions")
    action_results = _execute_actions(
        snaps,
        classifications,
        auto_rerun=cfg["auto_rerun"].lower() == "true",
        auto_update_branch=cfg["auto_update_branch"].lower() == "true",
    )
    report.action_results = action_results
    logger.info(
        "actions: %d total (success=%d, failed=%d, skipped=%d)",
        len(action_results),
        sum(1 for r in action_results if r.status == ActionStatus.SUCCESS),
        sum(1 for r in action_results if r.status == ActionStatus.FAILED),
        sum(1 for r in action_results if r.status == ActionStatus.SKIPPED),
    )

    logger.info("step 5/6: digest drafter agent")
    drafter = make_digest_drafter_agent()
    drafter_input = json.dumps(
        {
            "classifications": [c.model_dump(mode="json") for c in classifications],
            "action_results": [r.model_dump(mode="json") for r in action_results],
        },
        indent=2,
    )
    digest_response = await drafter.run(drafter_input)
    report.digest_markdown = _extract_response_text(digest_response)
    logger.info("digest drafted (%d chars)", len(report.digest_markdown))

    logger.info("step 6/6: notification decision")
    if not report.email_required:
        logger.info("no human-attention PRs and no failed actions — email skipped")
        return report

    if not (cfg["smtp_user"] and cfg["smtp_pass"] and cfg["email_from"] and cfg["email_to"]):
        logger.info("SMTP/email config not provided — email skipped (digest printed in summary)")
        return report

    subject = (
        f"OSS daily — {len(report.human_attention_prs)} need attention, "
        f"{len(report.failed_actions)} auto-action failures"
    )
    email_result = send_email_smtp(
        subject=subject,
        body_markdown=report.digest_markdown or "(empty digest)",
        smtp_host=cfg["smtp_host"],
        smtp_port=int(cfg["smtp_port"] or 587),
        smtp_username=cfg["smtp_user"],
        smtp_password=cfg["smtp_pass"],
        email_from=cfg["email_from"],
        email_to=cfg["email_to"],
        dry_run=cfg["email_dry_run"].lower() == "true",
    )
    report.action_results.append(email_result)
    logger.info("email: %s — %s", email_result.status.value, email_result.detail)

    return report


def _print_report_summary(report: DailyReport) -> None:
    print("=" * 70)
    print(f"OSS Tracker Agent — run at {report.timestamp.isoformat()}")
    print("=" * 70)
    print(f"PRs scanned          : {len(report.snapshots)}")
    print(f"Need human attention : {len(report.human_attention_prs)}")
    print(f"Auto-actions run     : {len(report.action_results)}")
    print(f"Failed actions       : {len(report.failed_actions)}")
    if report.digest_markdown:
        print("\n--- digest ---")
        print(report.digest_markdown)
        print("--- end digest ---")


def _load_env() -> None:
    """Load .env from project root."""
    project_root = Path(__file__).resolve().parent.parent
    dotenv_path = project_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
    else:
        load_dotenv()  # fall back to cwd / process env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oss_tracker_agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="one-shot daily run (default)")
    mode.add_argument("--server", action="store_true", help="serve via agentdev for the Agent Inspector")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    parser.add_argument("--port", type=int, default=8088, help="server port (only with --server)")
    args = parser.parse_args(argv)

    _load_env()
    _setup_logging(args.verbose)

    if args.server:
        return _run_server(args.port)

    try:
        report = asyncio.run(run_daily(verbose=args.verbose))
    except RuntimeError as exc:
        logger.error("startup error: %s", exc)
        return 2

    _print_report_summary(report)
    return 0


def _run_server(port: int) -> int:
    """Minimal FastAPI server so the agent can be inspected via agentdev."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        import uvicorn
    except ImportError:
        logger.error(
            "FastAPI/uvicorn not installed. Install with: pip install fastapi uvicorn"
        )
        return 3

    app = FastAPI(title="OSS Tracker Agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/run")
    async def run_endpoint() -> JSONResponse:
        report = await run_daily(verbose=False)
        return JSONResponse(report.model_dump(mode="json"))

    @app.post("/analyze")
    async def analyze_endpoint(body: dict) -> JSONResponse:
        """Forward a raw PR-snapshot list to the analyzer agent."""
        analyzer = make_analyzer_agent()
        response = await analyzer.run(json.dumps(body))
        return JSONResponse({"text": _extract_response_text(response)})

    logger.info("starting server on http://127.0.0.1:%d", port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
