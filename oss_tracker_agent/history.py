"""Persistent run history log.

Each daily run appends one Markdown entry to ``RUN_HISTORY.md`` (newest first)
so the project can answer "what did the auto-pipeline do at 04:00 UTC?" without
having to download workflow logs.

Entries are delimited by the literal sentinel ``<!-- entry -->`` so we can
trivially split, prepend, and truncate them while keeping the file
human-readable and diff-friendly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import ActionStatus, DailyReport, PRClassification, PRSnapshot

ENTRY_DELIM = "<!-- entry -->"
DEFAULT_MAX_ENTRIES = 200

_HEADER = (
    "# OSS Tracker — Run History\n"
    "\n"
    "Auto-appended by the daily/hourly workflow. Newest run at top. "
    f"Capped at {DEFAULT_MAX_ENTRIES} entries.\n"
    "\n"
)


def _emoji_for_check_conclusion(conclusion: str | None) -> str:
    if conclusion is None:
        return "⏳"
    c = conclusion.upper()
    if c == "SUCCESS":
        return "✅"
    if c == "FAILURE":
        return "❌"
    if c == "CANCELLED":
        return "🚫"
    if c == "SKIPPED":
        return "⏭"
    if c == "NEUTRAL":
        return "⚪"
    return "❓"


def _pr_status_label(snap: PRSnapshot, cls: PRClassification | None) -> str:
    parts: list[str] = []
    parts.append("DRAFT" if snap.is_draft else "READY")
    if snap.review_decision:
        parts.append(snap.review_decision.replace("_", " ").lower())
    if snap.mergeable and snap.mergeable.upper() != "MERGEABLE":
        parts.append(f"mergeable={snap.mergeable.lower()}")
    if snap.merge_state and snap.merge_state.upper() not in {"CLEAN", "UNSTABLE", "UNKNOWN"}:
        parts.append(f"state={snap.merge_state.lower()}")
    fail_count = len(snap.failing_checks)
    if fail_count:
        parts.append(f"{fail_count} failing check{'s' if fail_count != 1 else ''}")
    if cls and cls.urgency != "low":
        parts.append(f"urgency={cls.urgency}")
    return " · ".join(parts)


def _render_entry(
    report: DailyReport,
    *,
    run_id: str,
    trigger: str,
    workflow_url: str | None,
) -> str:
    ts = report.timestamp.astimezone(timezone.utc)
    header_status = "✅ success" if not report.failed_actions else "❌ failures"

    cls_by_pr = {(c.repo, c.pr_number): c for c in report.classifications}
    attention = sorted(
        report.human_attention_prs,
        key=lambda c: ({"high": 0, "medium": 1, "low": 2}.get(c.urgency, 3), c.repo, c.pr_number),
    )
    attention_keys = {(c.repo, c.pr_number) for c in attention}
    quiet_snaps = [s for s in report.snapshots if (s.repo, s.number) not in attention_keys]

    auto_actions = [
        r
        for r in report.action_results
        if r.status == ActionStatus.SUCCESS and r.action.value not in {"NONE", "NOTIFY_HUMAN"}
    ]
    failed = report.failed_actions

    self_review = report.self_review
    verdict_line = (
        f"**Self-review:** {self_review.verdict} "
        f"({len(self_review.issues)} issue(s), {len(self_review.suggestions)} suggestion(s))"
        if self_review is not None
        else "**Self-review:** disabled"
    )

    run_link = f"[run {run_id}]({workflow_url})" if workflow_url else f"run {run_id}"

    out: list[str] = []
    out.append(ENTRY_DELIM)
    out.append(f"## {ts.strftime('%Y-%m-%d %H:%M UTC')} — {run_link} — {header_status}")
    out.append("")
    out.append(
        f"**Trigger:** `{trigger}` · **Scanned:** {len(report.snapshots)} · "
        f"**Need attention:** {len(attention)} · "
        f"**Auto-actions:** {len(auto_actions)} · **Failed:** {len(failed)}"
    )
    out.append("")
    out.append(verdict_line)
    out.append("")

    if attention:
        out.append("### 🔴 Needs human attention")
        for cls in attention:
            snap = next(
                (s for s in report.snapshots if s.repo == cls.repo and s.number == cls.pr_number),
                None,
            )
            url = snap.url if snap else f"https://github.com/{cls.repo}/pull/{cls.pr_number}"
            label = _pr_status_label(snap, cls) if snap else cls.urgency
            deep_dive_marker = " · 🔬 deep-dive attached" if cls.deep_dive else ""
            reasoning = cls.reasoning.strip().splitlines()[0] if cls.reasoning else ""
            reasoning_short = (reasoning[:140] + "…") if len(reasoning) > 140 else reasoning
            out.append(
                f"- [{cls.repo}#{cls.pr_number}]({url}) — `{label}`{deep_dive_marker}"
                + (f" — {reasoning_short}" if reasoning_short else "")
            )
        out.append("")

    if auto_actions:
        out.append("### 🤖 Auto-actions taken")
        for r in auto_actions:
            url = f"https://github.com/{r.repo}/pull/{r.pr_number}"
            detail = r.detail.strip().splitlines()[0] if r.detail else ""
            detail_short = (detail[:120] + "…") if len(detail) > 120 else detail
            out.append(
                f"- [{r.repo}#{r.pr_number}]({url}) — `{r.action.value}`"
                + (f" — {detail_short}" if detail_short else "")
            )
        out.append("")

    if failed:
        out.append("### ❌ Failed actions")
        for r in failed:
            url = f"https://github.com/{r.repo}/pull/{r.pr_number}"
            detail = r.detail.strip().splitlines()[0] if r.detail else ""
            detail_short = (detail[:120] + "…") if len(detail) > 120 else detail
            out.append(
                f"- [{r.repo}#{r.pr_number}]({url}) — `{r.action.value}` — {detail_short}"
            )
        out.append("")

    if quiet_snaps:
        out.append(f"### 🟢 Quiet ({len(quiet_snaps)})")
        for snap in sorted(quiet_snaps, key=lambda s: (s.repo, s.number)):
            checks_summary = ""
            if snap.checks:
                emojis = "".join(_emoji_for_check_conclusion(c.conclusion) for c in snap.checks[:8])
                checks_summary = f" {emojis}"
            label = _pr_status_label(snap, cls_by_pr.get((snap.repo, snap.number)))
            out.append(
                f"- [{snap.repo}#{snap.number}]({snap.url}) — `{label}`{checks_summary}"
            )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def append_run_history(
    report: DailyReport,
    history_path: Path,
    *,
    run_id: str,
    trigger: str,
    workflow_url: str | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> None:
    """Prepend a fresh entry for ``report`` to ``history_path`` (atomic write)."""

    new_entry = _render_entry(report, run_id=run_id, trigger=trigger, workflow_url=workflow_url)

    if history_path.exists():
        existing = history_path.read_text(encoding="utf-8")
    else:
        existing = ""

    if ENTRY_DELIM in existing:
        prefix, _, rest = existing.partition(ENTRY_DELIM)
        old_entries = [ENTRY_DELIM + chunk for chunk in rest.split(ENTRY_DELIM) if chunk.strip()]
        if not prefix.strip():
            prefix = _HEADER
    else:
        prefix = _HEADER
        old_entries = []

    kept = old_entries[: max_entries - 1]
    final = prefix.rstrip() + "\n\n" + new_entry + "\n" + "\n".join(kept)
    final = final.rstrip() + "\n"

    tmp = history_path.with_suffix(history_path.suffix + ".tmp")
    tmp.write_text(final, encoding="utf-8")
    tmp.replace(history_path)
