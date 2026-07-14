"""Safe source-tree updates via git merge.

Hard rule: never overwrite local work.
- dirty tracked files => refuse
- merge conflicts (dry-run) => refuse + write conflict log
- only a clean fast/auto merge is applied
"""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.platform.logging.logger import logger
from app.platform.paths import log_dir

_ROOT = Path(__file__).resolve().parents[2]

_LAUNCH_AGENT_LABEL = "local.grok2api"


def _schedule_service_restart(*, delay_seconds: float = 1.2) -> dict[str, Any]:
    """Restart LaunchAgent after response flush. Never overwrites git state."""
    import os

    uid = os.getuid()
    label = _LAUNCH_AGENT_LABEL
    domain = f"gui/{uid}"
    lines = [
        "#!/bin/bash",
        f"sleep {delay_seconds:.1f}",
        f'if launchctl print "{domain}/{label}" >/dev/null 2>&1; then',
        f'  launchctl kickstart -k "{domain}/{label}" >/tmp/grok2api-update-restart.log 2>&1 && exit 0',
        f'  launchctl kill SIGTERM "{domain}/{label}" >/tmp/grok2api-update-restart.log 2>&1 || true',
        "  sleep 0.8",
        f'  launchctl kickstart "{domain}/{label}" >>/tmp/grok2api-update-restart.log 2>&1 && exit 0',
        "fi",
        f'echo "LaunchAgent {label} not loaded; manual restart required" >>/tmp/grok2api-update-restart.log',
        "exit 1",
        "",
    ]
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        runner = log_dir() / "update-restart.sh"
        runner.write_text("\n".join(lines), encoding="utf-8")
        runner.chmod(0o700)
        subprocess.Popen(
            ["/bin/bash", str(runner)],
            cwd=str(_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_update_log(
            "UPDATE_RESTART_SCHEDULED",
            f"delay={delay_seconds}s label={label} domain={domain} runner={runner}",
        )
        return {
            "scheduled": True,
            "method": "launchctl_kickstart",
            "label": label,
            "delay_seconds": delay_seconds,
        }
    except Exception as exc:  # pragma: no cover
        logger.error("failed to schedule service restart: {}", exc)
        _write_update_log("UPDATE_RESTART_SCHEDULE_FAILED", str(exc))
        return {"scheduled": False, "method": "none", "error": str(exc)}


_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCHES = ("main", "master")
_CONFLICT_RE = re.compile(r"^CONFLICT \([^)]+\): Merge conflict in (.+)$")
_CONFLICT_PATH_RE = re.compile(r"^(?:CONFLICT \([^)]+\): .+? in |Auto-merging )?(.+)$")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd or _ROOT),
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _git(*args: str, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (proc.stdout or "", proc.stderr or "") if part).strip()


def _write_update_log(title: str, body: str) -> str:
    log_dir().mkdir(parents=True, exist_ok=True)
    path = log_dir() / "update.log"
    block = (
        f"\n===== {title} @ {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')} =====\n"
        f"{body.rstrip()}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return str(path)


def _write_conflict_report(payload: dict[str, Any]) -> str:
    log_dir().mkdir(parents=True, exist_ok=True)
    path = log_dir() / f"update-conflict-{_utc_stamp()}.md"
    files = payload.get("conflict_files") or []
    lines = [
        "# Grok2API 安全更新冲突报告",
        "",
        f"- 时间: `{payload.get('checked_at')}`",
        f"- 分支: `{payload.get('branch')}`",
        f"- 上游: `{payload.get('upstream')}`",
        f"- HEAD: `{payload.get('head')}`",
        f"- 上游提交: `{payload.get('upstream_head')}`",
        f"- 结果: **{payload.get('status')}** / `{payload.get('reason')}`",
        "",
        "## 冲突文件",
        "",
    ]
    if files:
        lines.extend(f"- `{name}`" for name in files)
    else:
        lines.append("- （未解析到具体路径，请查看下方原始输出）")
    lines.extend(
        [
            "",
            "## 给 Agent 的合并提示",
            "",
            "1. 不要 `git reset --hard` / 不要覆盖本地未提交改动。",
            "2. 先看冲突文件 diff：",
            "```bash",
            f"git fetch {payload.get('remote') or _DEFAULT_REMOTE}",
            f"git diff --name-status HEAD...{payload.get('upstream')}",
            "```",
            "3. 需要手工合并时再执行（会进入冲突状态，可随时 abort）：",
            "```bash",
            f"git merge --no-edit {payload.get('upstream')}",
            "# 处理完：git add ... && git commit",
            "# 放弃：git merge --abort",
            "```",
            "",
            "## 原始 dry-run 输出",
            "",
            "```text",
            str(payload.get("detail") or "").rstrip() or "(empty)",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _parse_conflict_files(output: str) -> list[str]:
    files: list[str] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line.startswith("CONFLICT "):
            continue
        match = _CONFLICT_RE.match(line)
        if match:
            files.append(match.group(1).strip())
            continue
        # fallback: last path-looking token
        if " in " in line:
            files.append(line.rsplit(" in ", 1)[-1].strip())
    # unique preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for item in files:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _repo_root(cwd: Path | None = None) -> Path | None:
    proc = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return Path(text) if text else None


def _current_branch(cwd: Path) -> str:
    proc = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    return (proc.stdout or "").strip() or "HEAD"


def _head_sha(cwd: Path) -> str:
    proc = _git("rev-parse", "HEAD", cwd=cwd)
    return (proc.stdout or "").strip()


def _tracked_dirty(cwd: Path) -> list[str]:
    proc = _git("status", "--porcelain", "--untracked-files=no", cwd=cwd)
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return lines


def _resolve_upstream(cwd: Path, remote: str = _DEFAULT_REMOTE) -> tuple[str | None, str | None]:
    # Prefer upstream of current branch, then origin/main|master.
    proc = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=cwd)
    if proc.returncode == 0:
        name = (proc.stdout or "").strip()
        if name:
            return name, None
    for branch in _DEFAULT_BRANCHES:
        ref = f"{remote}/{branch}"
        probe = _git("rev-parse", "--verify", ref, cwd=cwd)
        if probe.returncode == 0:
            return ref, None
    return None, f"未找到上游分支（尝试了 @{{u}} 与 {remote}/main|master）"


def inspect_update_state(
    *,
    remote: str = _DEFAULT_REMOTE,
    cwd: Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(cwd)
    if root is None:
        return {
            "status": "error",
            "reason": "not_a_git_repo",
            "message": "当前目录不是 git 仓库，无法安全合并更新。",
            "can_apply": False,
        }

    dirty = _tracked_dirty(root)
    branch = _current_branch(root)
    head = _head_sha(root)
    upstream, upstream_err = _resolve_upstream(root, remote=remote)
    payload: dict[str, Any] = {
        "status": "ok",
        "reason": "ready",
        "message": "工作区干净，可进行安全合并预检。",
        "can_apply": False,
        "repo_root": str(root),
        "branch": branch,
        "head": head,
        "remote": remote,
        "upstream": upstream,
        "dirty_tracked": dirty,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if dirty:
        payload.update(
            {
                "status": "blocked",
                "reason": "dirty_worktree",
                "message": "本地有未提交的已跟踪文件改动，已拒绝自动更新，避免覆盖。",
                "can_apply": False,
            }
        )
        return payload
    if not upstream:
        payload.update(
            {
                "status": "error",
                "reason": "upstream_missing",
                "message": upstream_err or "找不到上游分支。",
                "can_apply": False,
            }
        )
        return payload

    ahead_behind = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}", cwd=root)
    local_only = 0
    upstream_only = 0
    if ahead_behind.returncode == 0:
        parts = (ahead_behind.stdout or "").strip().split()
        if len(parts) == 2:
            local_only = int(parts[0])
            upstream_only = int(parts[1])
    payload["local_only_commits"] = local_only
    payload["upstream_only_commits"] = upstream_only
    payload["upstream_head"] = (_git("rev-parse", upstream, cwd=root).stdout or "").strip()

    if upstream_only == 0:
        payload.update(
            {
                "status": "ok",
                "reason": "already_up_to_date",
                "message": "本地已包含上游全部提交，无需合并。",
                "can_apply": False,
            }
        )
        return payload

    # Dry-run merge with merge-tree (no index/workdir mutation).
    dry = _git("merge-tree", "--write-tree", "HEAD", upstream, cwd=root)
    detail = _combined(dry)
    conflicts = _parse_conflict_files(detail)
    payload["dry_run_exit"] = dry.returncode
    payload["detail"] = detail
    if dry.returncode != 0 or conflicts or "CONFLICT " in detail:
        payload.update(
            {
                "status": "conflict",
                "reason": "merge_conflict",
                "message": "预检发现合并冲突，已拒绝自动更新。请查看冲突日志后手动处理。",
                "can_apply": False,
                "conflict_files": conflicts,
            }
        )
        report = _write_conflict_report(payload)
        log_path = _write_update_log(
            "UPDATE_BLOCKED_CONFLICT",
            f"upstream={upstream}\nreport={report}\nfiles={conflicts}\n\n{detail}",
        )
        payload["conflict_report"] = report
        payload["log_path"] = log_path
        logger.warning(
            "source update blocked by merge conflict: upstream={} files={} report={}",
            upstream,
            conflicts,
            report,
        )
        return payload

    payload.update(
        {
            "status": "ok",
            "reason": "clean_merge",
            "message": "预检通过：无冲突，可安全自动合并。",
            "can_apply": True,
            "conflict_files": [],
        }
    )
    return payload


def apply_source_update(
    *,
    remote: str = _DEFAULT_REMOTE,
    cwd: Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(cwd)
    if root is None:
        return {
            "status": "error",
            "reason": "not_a_git_repo",
            "message": "当前目录不是 git 仓库。",
            "applied": False,
        }

    # Always re-fetch before both preflight and apply.
    fetch = _git("fetch", "--prune", remote, cwd=root, timeout=180.0)
    if fetch.returncode != 0:
        detail = _combined(fetch)
        log_path = _write_update_log("UPDATE_FETCH_FAILED", detail)
        logger.error("source update fetch failed: {}", detail)
        return {
            "status": "error",
            "reason": "fetch_failed",
            "message": "拉取上游失败。",
            "detail": detail,
            "log_path": log_path,
            "applied": False,
        }

    preflight = inspect_update_state(remote=remote, cwd=root)
    if preflight.get("reason") == "already_up_to_date":
        preflight["applied"] = False
        return preflight
    if not preflight.get("can_apply"):
        preflight["applied"] = False
        if preflight.get("status") == "conflict" and not preflight.get("log_path"):
            # inspect already logs conflicts; ensure a general log line exists
            preflight["log_path"] = _write_update_log(
                "UPDATE_BLOCKED",
                f"reason={preflight.get('reason')}\n{preflight.get('message')}\n{preflight.get('detail') or ''}",
            )
        elif preflight.get("status") != "conflict":
            preflight["log_path"] = _write_update_log(
                "UPDATE_BLOCKED",
                f"reason={preflight.get('reason')}\ndirty={preflight.get('dirty_tracked')}\n{preflight.get('message')}",
            )
        return preflight

    upstream = str(preflight["upstream"])
    before = _head_sha(root)

    # Real merge. Never force, never reset.
    merge = _git("merge", "--no-edit", "--no-ff", upstream, cwd=root, timeout=180.0)
    detail = _combined(merge)
    if merge.returncode != 0:
        # If merge started and conflicted despite dry-run, abort immediately.
        if (root / ".git" / "MERGE_HEAD").exists():
            _git("merge", "--abort", cwd=root)
        conflicts = _parse_conflict_files(detail)
        payload = {
            **preflight,
            "status": "conflict",
            "reason": "merge_failed",
            "message": "实际合并失败，已中止并回退合并状态，本地文件未用上游覆盖。",
            "applied": False,
            "conflict_files": conflicts,
            "detail": detail,
            "head_before": before,
            "head_after": _head_sha(root),
        }
        report = _write_conflict_report(payload)
        log_path = _write_update_log(
            "UPDATE_MERGE_FAILED_ABORTED",
            f"upstream={upstream}\nreport={report}\n\n{detail}",
        )
        payload["conflict_report"] = report
        payload["log_path"] = log_path
        logger.error("source update merge failed and aborted: {}", detail)
        return payload

    after = _head_sha(root)
    restart = _schedule_service_restart(delay_seconds=1.2)
    payload = {
        **preflight,
        "status": "ok",
        "reason": "merged",
        "message": "已安全合并上游变更，服务即将自动重启以加载新代码。",
        "applied": True,
        "needs_restart": True,
        "restart": restart,
        "restart_scheduled": bool(restart.get("scheduled")),
        "head_before": before,
        "head_after": after,
        "detail": detail,
    }
    log_path = _write_update_log(
        "UPDATE_MERGED",
        (
            f"upstream={upstream}\n"
            f"before={before}\n"
            f"after={after}\n"
            f"restart={restart}\n\n"
            f"{detail}"
        ),
    )
    payload["log_path"] = log_path
    logger.info(
        "source update merged cleanly: {} -> {} via {} restart={}",
        before,
        after,
        upstream,
        restart.get("scheduled"),
    )
    return payload


async def inspect_update_state_async(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(inspect_update_state, **kwargs)


async def apply_source_update_async(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(apply_source_update, **kwargs)


__all__ = [
    "apply_source_update",
    "apply_source_update_async",
    "inspect_update_state",
    "inspect_update_state_async",
]
