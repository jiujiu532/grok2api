"""GitHub update checks with lightweight in-process caching.

Prefers GitHub Releases. If the upstream repo has no releases/tags (common for
active forks), falls back to the default-branch latest commit so "check update"
still works when authenticated.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import aiohttp

from app.platform.meta import get_project_version

_REPO = "jiujiu532/grok2api"
_RELEASES_URL = f"https://api.github.com/repos/{_REPO}/releases"
_TAGS_URL = f"https://api.github.com/repos/{_REPO}/tags"
_COMMITS_URL = f"https://api.github.com/repos/{_REPO}/commits"
_REPO_URL = f"https://api.github.com/repos/{_REPO}"
_CACHE_TTL_SECONDS = 86400.0
_ERROR_TTL_SECONDS = 300.0
_RATE_LIMIT_TTL_SECONDS = 1800.0
_LOCK = asyncio.Lock()
_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_ROOT = Path(__file__).resolve().parents[2]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_version(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    return text


def _parse_version(value: str) -> tuple[int, int, int, int, int] | None:
    normalized = _normalize_version(value)
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:(?:\.|-)?rc(\d+))?$", normalized, re.IGNORECASE)
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    is_final = 1 if rc is None else 0
    rc_number = int(rc or 0)
    return int(major or 0), int(minor or 0), int(patch or 0), is_final, rc_number


def _is_newer(latest: str, current: str) -> bool:
    latest_parsed = _parse_version(latest)
    current_parsed = _parse_version(current)
    if latest_parsed and current_parsed:
        return latest_parsed > current_parsed
    return _normalize_version(latest) > _normalize_version(current)


def _release_version_key(release: dict[str, Any]) -> tuple[int, int, int, int, int] | None:
    version = str(release.get("tag_name") or release.get("name") or "").strip()
    return _parse_version(version)


def _select_latest_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[tuple[int, int, int, int, int], dict[str, Any]]] = []
    for release in releases:
        if not isinstance(release, dict) or bool(release.get("draft")):
            continue
        version_key = _release_version_key(release)
        if version_key is None:
            continue
        candidates.append((version_key, release))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _github_token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GROK2API_GITHUB_TOKEN"):
        value = str(os.getenv(name, "") or "").strip()
        if value:
            return value
    try:
        from app.platform.config.snapshot import get_config

        value = str(get_config("app.github_token", "") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return ""


def _normalize_error_message(value: str) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if "rate limit exceeded" in lowered or "api rate limit exceeded" in lowered:
        if _github_token():
            return (
                "GitHub API rate limit exceeded（当前已配置 Token，可能额度暂时用尽）。"
                "请稍后再试。"
            )
        return (
            "GitHub API rate limit exceeded（匿名额度 60 次/小时已用尽）。"
            "解决：配置 GITHUB_TOKEN 或 app.github_token。"
        )
    if text.startswith("GitHub release query failed:"):
        status_match = re.search(r"GitHub release query failed:\s*(\d{3})", text)
        if status_match:
            return f"GitHub release query failed ({status_match.group(1)})."
        return "GitHub release query failed."
    if text == "GitHub releases response invalid":
        return "GitHub releases response invalid."
    if text == "No valid GitHub releases found":
        return "No valid GitHub releases found."
    return text


def _rate_from_headers(headers: Any) -> dict[str, Any]:
    def _i(name: str) -> int | None:
        raw = headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    return {
        "limit": _i("X-RateLimit-Limit"),
        "remaining": _i("X-RateLimit-Remaining"),
        "reset": _i("X-RateLimit-Reset"),
    }


def _local_head_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_ROOT),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def _local_upstream_sha() -> str:
    for ref in ("@{u}", "origin/main", "origin/master"):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", ref],
                cwd=str(_ROOT),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if proc.returncode == 0:
                sha = (proc.stdout or "").strip()
                if sha:
                    return sha
        except Exception:
            continue
    return ""


def _build_payload(
    *,
    latest_version: str = "",
    release_name: str = "",
    release_url: str = "",
    published_at: str = "",
    release_notes: str = "",
    update_available: bool = False,
    error: str = "",
    auth_mode: str = "anonymous",
    rate: dict[str, Any] | None = None,
    source: str = "release",
    latest_commit: str = "",
    local_commit: str = "",
) -> dict[str, Any]:
    current_version = get_project_version()
    payload: dict[str, Any] = {
        "current_version": current_version,
        "latest_version": latest_version,
        "release_name": release_name,
        "release_url": release_url or f"https://github.com/{_REPO}",
        "published_at": published_at,
        "release_notes": release_notes,
        "update_available": bool(update_available),
        "checked_at": _utc_now_iso(),
        "status": "error" if error else "ok",
        "error": _normalize_error_message(error) if error else "",
        "auth_mode": auth_mode,
        "github_token_configured": bool(_github_token()),
        "source": source,
        "latest_commit": latest_commit,
        "local_commit": local_commit,
    }
    if rate:
        payload["rate_limit"] = rate
    return payload


async def _get_json(session: aiohttp.ClientSession, url: str, headers: dict[str, str], params: dict[str, str] | None = None) -> tuple[Any, dict[str, Any], int]:
    async with session.get(url, headers=headers, params=params) as response:
        rate = _rate_from_headers(response.headers)
        text = await response.text()
        if response.status != 200:
            raise RuntimeError(f"GitHub release query failed: {response.status} {text.strip()}".strip())
        try:
            data = await response.json(content_type=None)
        except Exception:
            # response.json may fail if already consumed; parse text
            import json as _json
            data = _json.loads(text)
        return data, rate, response.status


async def _fetch_update_info() -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=12)
    token = _github_token()
    auth_mode = "token" if token else "anonymous"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "grok2api-update-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    local_commit = _local_head_sha()
    rate: dict[str, Any] | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1) Releases
        try:
            releases, rate, _ = await _get_json(session, _RELEASES_URL, headers, {"per_page": "20"})
            if isinstance(releases, list) and releases:
                release = _select_latest_release(releases)
                if release:
                    latest_version = _normalize_version(str(release.get("tag_name") or release.get("name") or ""))
                    return _build_payload(
                        latest_version=latest_version,
                        release_name=str(release.get("name") or "").strip(),
                        release_url=str(release.get("html_url") or "").strip(),
                        published_at=str(release.get("published_at") or "").strip(),
                        release_notes=str(release.get("body") or "").strip(),
                        update_available=bool(latest_version) and _is_newer(latest_version, get_project_version()),
                        auth_mode=auth_mode,
                        rate=rate,
                        source="release",
                        local_commit=local_commit,
                    )
        except Exception as exc:
            # keep going to commit fallback only for empty/404-ish; hard fail rate limit
            if "rate limit exceeded" in str(exc).lower():
                raise

        # 2) Tags
        try:
            tags, rate, _ = await _get_json(session, _TAGS_URL, headers, {"per_page": "20"})
            if isinstance(tags, list) and tags:
                tag = tags[0] if isinstance(tags[0], dict) else {}
                latest_version = _normalize_version(str(tag.get("name") or ""))
                sha = str(((tag.get("commit") or {}) if isinstance(tag.get("commit"), dict) else {}).get("sha") or "")
                return _build_payload(
                    latest_version=latest_version,
                    release_name=latest_version,
                    release_url=f"https://github.com/{_REPO}/releases/tag/{latest_version}" if latest_version else f"https://github.com/{_REPO}",
                    published_at="",
                    release_notes="上游仓库暂无 GitHub Release，已使用最新 Tag 作为版本源。",
                    update_available=bool(latest_version) and _is_newer(latest_version, get_project_version()),
                    auth_mode=auth_mode,
                    rate=rate,
                    source="tag",
                    latest_commit=sha,
                    local_commit=local_commit,
                )
        except Exception as exc:
            if "rate limit exceeded" in str(exc).lower():
                raise

        # 3) Default branch latest commit
        repo, rate, _ = await _get_json(session, _REPO_URL, headers)
        default_branch = str((repo or {}).get("default_branch") or "main").strip() or "main"
        commits, rate, _ = await _get_json(
            session,
            _COMMITS_URL,
            headers,
            {"sha": default_branch, "per_page": "1"},
        )
        if not isinstance(commits, list) or not commits:
            raise RuntimeError("No valid GitHub releases found")
        commit = commits[0] if isinstance(commits[0], dict) else {}
        sha = str(commit.get("sha") or "").strip()
        commit_info = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
        message = str((commit_info or {}).get("message") or "").strip()
        published_at = str(((commit_info or {}).get("committer") or {}).get("date") or "").strip()
        html_url = str(commit.get("html_url") or f"https://github.com/{_REPO}/commits/{default_branch}").strip()
        short = sha[:7] if sha else ""
        update_available = bool(sha and local_commit and not local_commit.startswith(sha) and sha != local_commit)
        # If local tracking origin/main equals remote, not available.
        upstream = _local_upstream_sha()
        if sha and upstream and (upstream == sha or upstream.startswith(sha) or sha.startswith(upstream)):
            # still may be behind if local HEAD differs from upstream; prefer HEAD comparison
            pass
        if sha and local_commit:
            # Local may be ahead of origin/main (common for private forks).
            # Only report update when remote tip is NOT already an ancestor of HEAD.
            try:
                proc = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                    cwd=str(_ROOT),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                update_available = proc.returncode != 0
            except Exception:
                update_available = sha != local_commit and not local_commit.startswith(sha)
        notes = (
            "上游仓库当前没有 GitHub Release / Tag，已回退到默认分支最新提交。\n\n"
            f"- 分支: `{default_branch}`\n"
            f"- 最新提交: `{short}`\n"
            f"- 本地 HEAD: `{(local_commit[:7] if local_commit else '-')}`\n\n"
            f"{message}"
        )
        return _build_payload(
            latest_version=short or default_branch,
            release_name=f"{default_branch}@{short}" if short else default_branch,
            release_url=html_url,
            published_at=published_at,
            release_notes=notes,
            update_available=update_available,
            auth_mode=auth_mode,
            rate=rate,
            source="commit",
            latest_commit=sha,
            local_commit=local_commit,
        )


async def get_latest_release_info(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get("payload")
    expires_at = float(_CACHE.get("expires_at") or 0.0)
    if not force and cached and expires_at > now:
        return cached

    async with _LOCK:
        cached = _CACHE.get("payload")
        expires_at = float(_CACHE.get("expires_at") or 0.0)
        now = time.monotonic()
        if not force and cached and expires_at > now:
            return cached

        auth_mode = "token" if _github_token() else "anonymous"
        try:
            payload = await _fetch_update_info()
            ttl = _CACHE_TTL_SECONDS
        except Exception as exc:
            msg = str(exc)
            payload = _build_payload(error=msg, auth_mode=auth_mode, local_commit=_local_head_sha())
            ttl = (
                _RATE_LIMIT_TTL_SECONDS
                if "rate limit exceeded" in msg.lower()
                else _ERROR_TTL_SECONDS
            )

        _CACHE["payload"] = payload
        _CACHE["expires_at"] = now + ttl
        return payload


__all__ = ["get_latest_release_info"]
