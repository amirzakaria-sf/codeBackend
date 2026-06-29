from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from django.conf import settings
from django.utils import timezone

from ..models import GitSyncJob, OrchestrationRun
from .notifications import create_user_notification
from .realtime import broadcast_project_event


class GitSyncError(Exception):
    """Raised when GitHub sync automation fails."""


@dataclass
class GitHubRepoRef:
    owner: str
    repo: str


def enqueue_github_sync_for_run(run: OrchestrationRun) -> GitSyncJob:
    existing_job = GitSyncJob.objects.filter(run=run).first()
    if existing_job:
        return existing_job

    job = GitSyncJob.objects.create(
        project=run.project,
        run=run,
        user=run.user,
        base_branch=settings.GITHUB_PR_BASE_BRANCH,
        status=GitSyncJob.Status.QUEUED,
    )

    if not settings.GITHUB_AUTOMATION_ENABLED:
        job.status = GitSyncJob.Status.SKIPPED
        job.last_error = "GitHub automation is disabled."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        return job

    from ..tasks import execute_github_sync_task

    execute_github_sync_task.delay(job.id)
    return job


def retry_sync_job(job: GitSyncJob) -> GitSyncJob:
    job.status = GitSyncJob.Status.QUEUED
    job.last_error = ""
    job.started_at = None
    job.finished_at = None
    job.save(update_fields=["status", "last_error", "started_at", "finished_at", "updated_at"])
    from ..tasks import execute_github_sync_task

    execute_github_sync_task.delay(job.id)
    return job


def perform_github_sync(job_id: int) -> dict:
    job = GitSyncJob.objects.select_related("project", "run", "user").get(pk=job_id)
    project_path = Path(job.project.absolute_path)
    job.attempts += 1
    job.status = GitSyncJob.Status.RUNNING
    job.started_at = timezone.now()
    job.last_error = ""
    job.save(update_fields=["attempts", "status", "started_at", "last_error", "updated_at"])
    _broadcast_sync_event(job)

    try:
        if not project_path.exists():
            raise GitSyncError(f"Project path does not exist: {project_path}")

        remote_url = _git_output(project_path, ["remote", "get-url", "origin"])
        repo_ref = _extract_repo_ref(remote_url)

        _run_git(project_path, ["fetch", "origin", job.base_branch])

        feature_branch = job.feature_branch.strip() or f"feature/foundry-run-{job.run_id or job.id}"
        job.feature_branch = feature_branch
        _run_git(project_path, ["checkout", "-B", feature_branch, f"origin/{job.base_branch}"])

        _run_git(project_path, ["add", "-A"])
        if _git_exit_code(project_path, ["diff", "--cached", "--quiet"]) == 0:
            job.status = GitSyncJob.Status.SKIPPED
            job.last_error = "No local changes to commit."
            job.finished_at = timezone.now()
            job.save(update_fields=["feature_branch", "status", "last_error", "finished_at", "updated_at"])
            _broadcast_sync_event(job)
            return {"job_id": job.id, "status": job.status, "reason": job.last_error}

        commit_message = _build_commit_message(job)
        _run_git(project_path, ["commit", "-m", commit_message])
        job.commit_sha = _git_output(project_path, ["rev-parse", "HEAD"])
        _run_git(project_path, ["push", "-u", "origin", feature_branch])

        pr_payload = _create_or_get_pull_request(
            repo_ref=repo_ref,
            base_branch=job.base_branch,
            feature_branch=feature_branch,
            title=f"[{job.project.name}] Foundry automation run #{job.run_id or job.id}",
            body=_build_pr_body(job),
        )

        job.pr_number = pr_payload.get("number")
        job.pr_url = pr_payload.get("html_url") or ""
        job.status = GitSyncJob.Status.COMPLETED
        job.finished_at = timezone.now()
        job.save(
            update_fields=[
                "feature_branch",
                "commit_sha",
                "pr_number",
                "pr_url",
                "status",
                "finished_at",
                "updated_at",
            ],
        )
        _broadcast_sync_event(job)

        if job.user_id:
            create_user_notification(
                user=job.user,
                kind="github_sync_completed",
                title=f"GitHub sync completed for {job.project.name}",
                message=f"PR #{job.pr_number} is ready against {job.base_branch}.",
                project=job.project,
                run=job.run,
                payload={"sync_job_id": job.id, "pr_url": job.pr_url, "feature_branch": feature_branch},
            )

        return {
            "job_id": job.id,
            "status": job.status,
            "feature_branch": job.feature_branch,
            "pr_url": job.pr_url,
            "pr_number": job.pr_number,
        }
    except Exception as error:  # noqa: BLE001
        job.status = GitSyncJob.Status.FAILED
        job.last_error = str(error)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        _broadcast_sync_event(job)
        if job.user_id:
            create_user_notification(
                user=job.user,
                kind="github_sync_failed",
                title=f"GitHub sync failed for {job.project.name}",
                message=str(error),
                project=job.project,
                run=job.run,
                payload={"sync_job_id": job.id},
            )
        raise


def _run_git(project_path: Path, args: list[str]) -> None:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GitSyncError(f"git {' '.join(args)} failed: {stderr}")


def _git_output(project_path: Path, args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GitSyncError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def _git_exit_code(project_path: Path, args: list[str]) -> int:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False)
    return result.returncode


def _extract_repo_ref(remote_url: str) -> GitHubRepoRef:
    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(\.git)?$", remote_url)
    if ssh_match:
        return GitHubRepoRef(owner=ssh_match.group("owner"), repo=ssh_match.group("repo"))

    https_match = re.match(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(\.git)?$", remote_url)
    if https_match:
        return GitHubRepoRef(owner=https_match.group("owner"), repo=https_match.group("repo"))

    raise GitSyncError("Only GitHub remotes are supported for automation.")


def _create_or_get_pull_request(*, repo_ref: GitHubRepoRef, base_branch: str, feature_branch: str, title: str, body: str) -> dict:
    token = settings.GITHUB_TOKEN.strip()
    if not token:
        raise GitSyncError("GITHUB_TOKEN is not configured.")

    base_url = settings.GITHUB_API_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(base_url=base_url, timeout=20, headers=headers) as client:
        existing = client.get(
            f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls",
            params={"state": "open", "head": f"{repo_ref.owner}:{feature_branch}", "base": base_branch},
        )
        existing.raise_for_status()
        existing_payload = existing.json()
        if isinstance(existing_payload, list) and existing_payload:
            return existing_payload[0]

        response = client.post(
            f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls",
            json={
                "title": title,
                "head": feature_branch,
                "base": base_branch,
                "body": body,
            },
        )
        if response.status_code >= 400:
            raise GitSyncError(f"GitHub PR creation failed ({response.status_code}): {response.text}")
        return response.json()


def _build_commit_message(job: GitSyncJob) -> str:
    prefix = settings.GITHUB_COMMIT_PREFIX.strip() or "chore(foundry):"
    prompt_excerpt = (job.run.prompt if job.run_id else "automation sync").strip()
    if len(prompt_excerpt) > 70:
        prompt_excerpt = f"{prompt_excerpt[:67]}..."
    return f"{prefix} run #{job.run_id or job.id} - {prompt_excerpt}"


def _build_pr_body(job: GitSyncJob) -> str:
    run_id = job.run_id or "n/a"
    prompt = job.run.prompt if job.run_id else "Automated sync"
    return (
        "## Foundry Automation\n"
        f"- Project: `{job.project.name}`\n"
        f"- Run ID: `{run_id}`\n"
        f"- Base branch: `{job.base_branch}`\n\n"
        "### Original Prompt\n"
        f"{prompt}\n"
    )


def _broadcast_sync_event(job: GitSyncJob) -> None:
    broadcast_project_event(
        job.project_id,
        {
            "kind": "github_sync_updated",
            "project_id": job.project_id,
            "run_id": job.run_id,
            "sync_job_id": job.id,
            "sync_status": job.status,
            "feature_branch": job.feature_branch,
            "base_branch": job.base_branch,
            "pr_number": job.pr_number,
            "pr_url": job.pr_url,
            "last_error": job.last_error,
        },
    )
