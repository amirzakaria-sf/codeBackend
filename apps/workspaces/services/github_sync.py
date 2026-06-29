from __future__ import annotations

import os
import re
import subprocess
from base64 import b64encode
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

        repo_ref = _ensure_repository_bootstrap(job, project_path)
        _ensure_branch_ready(project_path, repo_ref, "main", start_point="main")
        _ensure_branch_ready(project_path, repo_ref, job.base_branch, start_point="main")

        feature_branch = job.feature_branch.strip() or f"feature/foundry-run-{job.run_id or job.id}"
        job.feature_branch = feature_branch
        job.save(update_fields=["feature_branch", "updated_at"])
        _broadcast_sync_event(job)

        _checkout_feature_branch(project_path, repo_ref, feature_branch, job.base_branch)

        _run_git(project_path, ["add", "-A"])
        if _git_exit_code(project_path, ["diff", "--cached", "--quiet"]) == 0:
            _sync_local_branch_to_remote(project_path, repo_ref, job.base_branch)
            job.status = GitSyncJob.Status.SKIPPED
            job.last_error = "No local changes to commit."
            job.finished_at = timezone.now()
            job.save(update_fields=["feature_branch", "status", "last_error", "finished_at", "updated_at"])
            _broadcast_sync_event(job)
            return {"job_id": job.id, "status": job.status, "reason": job.last_error}

        commit_message = _build_commit_message(job)
        _commit_tracked_changes(project_path, ["commit", "-m", commit_message], job)
        job.commit_sha = _git_output(project_path, ["rev-parse", "HEAD"])
        job.save(update_fields=["commit_sha", "updated_at"])

        _run_git_authenticated(project_path, ["push", "-u", "origin", feature_branch])

        pr_payload = _create_or_get_pull_request(
            repo_ref=repo_ref,
            base_branch=job.base_branch,
            feature_branch=feature_branch,
            title=f"[{job.project.name}] Foundry automation run #{job.run_id or job.id}",
            body=_build_pr_body(job),
        )

        job.pr_number = pr_payload.get("number")
        job.pr_url = pr_payload.get("html_url") or ""
        job.save(update_fields=["pr_number", "pr_url", "updated_at"])
        _broadcast_sync_event(job)

        _merge_pull_request(repo_ref=repo_ref, pull_number=int(job.pr_number), commit_title=_build_merge_commit_title(job))
        _sync_local_branch_to_remote(project_path, repo_ref, job.base_branch)
        _delete_feature_branch_remote(repo_ref, feature_branch)
        _delete_feature_branch_local(project_path, feature_branch)

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
                message=f"PR #{job.pr_number} was created and merged into {job.base_branch}.",
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


def _ensure_repository_bootstrap(job: GitSyncJob, project_path: Path) -> GitHubRepoRef:
    repo_name = _derive_repository_name(job)
    owner_login = _github_authenticated_login()

    if not _is_git_repo(project_path):
        _git_init_main(project_path)

    if not _has_git_commits(project_path):
        _commit_tracked_changes(project_path, ["commit", "--allow-empty", "-m", "chore(foundry): initialize repository"], job)

    remote_url = _try_git_output(project_path, ["remote", "get-url", "origin"])
    if remote_url:
        repo_ref = _extract_repo_ref(remote_url)
        if repo_ref.owner != owner_login:
            raise GitSyncError("Existing git remote is not owned by the authenticated GitHub account configured for automation.")
    else:
        repo_ref = _create_or_get_repository(owner_login=owner_login, repo_name=repo_name)
        _run_git(project_path, ["remote", "add", "origin", _repository_https_url(repo_ref)])

    return repo_ref


def _ensure_branch_ready(project_path: Path, repo_ref: GitHubRepoRef, branch_name: str, *, start_point: str) -> None:
    if branch_name == "main":
        _run_git(project_path, ["checkout", "-B", "main"])
    else:
        _run_git(project_path, ["checkout", "-B", branch_name, start_point])
    _run_git_authenticated(project_path, ["push", "-u", "origin", branch_name])


def _checkout_feature_branch(project_path: Path, repo_ref: GitHubRepoRef, feature_branch: str, base_branch: str) -> None:
    _run_git_authenticated(project_path, ["fetch", "origin", base_branch])
    remote_ref = f"origin/{base_branch}"
    if _git_exit_code(project_path, ["rev-parse", "--verify", remote_ref]) == 0:
        _run_git(project_path, ["checkout", "-B", feature_branch, remote_ref])
    else:
        _run_git(project_path, ["checkout", "-B", feature_branch, base_branch])


def _sync_local_branch_to_remote(project_path: Path, repo_ref: GitHubRepoRef, branch_name: str) -> None:
    _run_git_authenticated(project_path, ["fetch", "origin", branch_name])
    _run_git(project_path, ["checkout", "-B", branch_name, f"origin/{branch_name}"])


def _delete_feature_branch_remote(repo_ref: GitHubRepoRef, feature_branch: str) -> None:
    try:
        with _github_client() as client:
            response = client.delete(f"/repos/{repo_ref.owner}/{repo_ref.repo}/git/refs/heads/{feature_branch}")
            if response.status_code not in {204, 404}:
                raise GitSyncError(f"GitHub branch delete failed ({response.status_code}): {response.text}")
    except Exception:
        return


def _delete_feature_branch_local(project_path: Path, feature_branch: str) -> None:
    try:
        _run_git(project_path, ["branch", "-D", feature_branch])
    except Exception:
        return


def _run_git(project_path: Path, args: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GitSyncError(f"git {' '.join(args)} failed: {stderr}")


def _git_output(project_path: Path, args: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GitSyncError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def _try_git_output(project_path: Path, args: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_exit_code(project_path: Path, args: list[str], *, env: dict[str, str] | None = None) -> int:
    result = subprocess.run(["git", *args], cwd=project_path, capture_output=True, text=True, check=False, env=env)
    return result.returncode


def _run_git_authenticated(project_path: Path, args: list[str]) -> None:
    env = dict(os.environ)
    env.update(_github_git_config_env())
    _run_git(project_path, args, env=env)


def _is_git_repo(project_path: Path) -> bool:
    return _git_exit_code(project_path, ["rev-parse", "--is-inside-work-tree"]) == 0


def _has_git_commits(project_path: Path) -> bool:
    return _git_exit_code(project_path, ["rev-parse", "--verify", "HEAD"]) == 0


def _git_init_main(project_path: Path) -> None:
    result = subprocess.run(["git", "init", "-b", "main"], cwd=project_path, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    fallback = subprocess.run(["git", "init"], cwd=project_path, capture_output=True, text=True, check=False)
    if fallback.returncode != 0:
        stderr = fallback.stderr.strip() or fallback.stdout.strip()
        raise GitSyncError(f"git init failed: {stderr}")
    _run_git(project_path, ["checkout", "-B", "main"])


def _commit_tracked_changes(project_path: Path, args: list[str], job: GitSyncJob) -> None:
    env = _git_identity_env(job)
    _run_git(project_path, args, env=env)


def _git_identity_env(job: GitSyncJob) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = settings.GITHUB_COMMITTER_NAME
    env["GIT_COMMITTER_NAME"] = settings.GITHUB_COMMITTER_NAME
    env["GIT_AUTHOR_EMAIL"] = settings.GITHUB_COMMITTER_EMAIL
    env["GIT_COMMITTER_EMAIL"] = settings.GITHUB_COMMITTER_EMAIL
    return env


def _extract_repo_ref(remote_url: str) -> GitHubRepoRef:
    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(\.git)?$", remote_url)
    if ssh_match:
        return GitHubRepoRef(owner=ssh_match.group("owner"), repo=ssh_match.group("repo"))

    https_match = re.match(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(\.git)?$", remote_url)
    if https_match:
        return GitHubRepoRef(owner=https_match.group("owner"), repo=https_match.group("repo"))

    raise GitSyncError("Only GitHub remotes are supported for automation.")


def _repository_https_url(repo_ref: GitHubRepoRef) -> str:
    return f"https://github.com/{repo_ref.owner}/{repo_ref.repo}.git"


def _derive_repository_name(job: GitSyncJob) -> str:
    owner_segment = (job.project.path_owner_username or "workspace").strip().lower()
    project_segment = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job.project.name.strip()).strip("-.") or f"project-{job.project_id}"
    candidate = f"{owner_segment}-{project_segment}" if owner_segment else project_segment
    candidate = re.sub(r"-+", "-", candidate).strip("-.")
    return candidate[:100]


def _github_http_extraheader() -> str:
    token = settings.GITHUB_TOKEN.strip()
    if not token:
        raise GitSyncError("GITHUB_TOKEN is not configured.")
    encoded = b64encode(f"x-access-token:{token}".encode("utf-8")).decode("utf-8")
    return f"AUTHORIZATION: basic {encoded}"


def _github_git_config_env() -> dict[str, str]:
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": _github_http_extraheader(),
    }


def _github_headers() -> dict[str, str]:
    token = settings.GITHUB_TOKEN.strip()
    if not token:
        raise GitSyncError("GITHUB_TOKEN is not configured.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }


def _github_client() -> httpx.Client:
    return httpx.Client(base_url=settings.GITHUB_API_BASE_URL.rstrip("/"), timeout=20, headers=_github_headers())


def _github_authenticated_login() -> str:
    with _github_client() as client:
        response = client.get("/user")
        if response.status_code >= 400:
            raise GitSyncError(f"GitHub user lookup failed ({response.status_code}): {response.text}")
        payload = response.json()
    login = str(payload.get("login") or "").strip()
    if not login:
        raise GitSyncError("GitHub user lookup did not return a login.")
    return login


def _create_or_get_repository(*, owner_login: str, repo_name: str) -> GitHubRepoRef:
    merge_flags = _repository_merge_capability_flags()
    with _github_client() as client:
        existing = client.get(f"/repos/{owner_login}/{repo_name}")
        if existing.status_code == 200:
            _validate_repository_merge_capabilities(existing.json(), _normalized_merge_method())
            return GitHubRepoRef(owner=owner_login, repo=repo_name)
        if existing.status_code not in {404, 403}:
            raise GitSyncError(f"GitHub repository lookup failed ({existing.status_code}): {existing.text}")

        response = client.post(
            "/user/repos",
            json={
                "name": repo_name,
                "private": settings.GITHUB_REPO_PRIVATE,
                "auto_init": False,
                "allow_auto_merge": True,
                "delete_branch_on_merge": True,
                **merge_flags,
            },
        )
        if response.status_code in {200, 201}:
            payload = response.json()
            _validate_repository_merge_capabilities(payload, _normalized_merge_method())
            return GitHubRepoRef(owner=str(payload.get("owner", {}).get("login") or owner_login), repo=str(payload.get("name") or repo_name))

        if response.status_code == 422:
            verify = client.get(f"/repos/{owner_login}/{repo_name}")
            if verify.status_code == 200:
                _validate_repository_merge_capabilities(verify.json(), _normalized_merge_method())
                return GitHubRepoRef(owner=owner_login, repo=repo_name)
            raise GitSyncError("GitHub repository creation failed validation, likely because the repository already exists with an incompatible state or the name is unavailable.")
        if response.status_code == 401:
            raise GitSyncError("GitHub repository creation failed because the token is invalid or expired.")
        if response.status_code == 403:
            raise GitSyncError("GitHub repository creation was forbidden. Check that the token has repo/private repository creation permissions and that account policy allows repository creation.")
        raise GitSyncError(f"GitHub repository creation failed ({response.status_code}): {response.text}")


def _create_or_get_pull_request(*, repo_ref: GitHubRepoRef, base_branch: str, feature_branch: str, title: str, body: str) -> dict:
    with _github_client() as client:
        existing = client.get(
            f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls",
            params={"state": "open", "head": f"{repo_ref.owner}:{feature_branch}", "base": base_branch},
        )
        if existing.status_code >= 400:
            raise GitSyncError(f"GitHub PR lookup failed ({existing.status_code}): {existing.text}")
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
            if response.status_code == 403:
                raise GitSyncError("GitHub PR creation was forbidden. Check pull request write permissions and repository policy.")
            if response.status_code == 422:
                raise GitSyncError("GitHub PR creation failed validation. The feature branch may already be merged, missing, or the base branch configuration is invalid.")
            raise GitSyncError(f"GitHub PR creation failed ({response.status_code}): {response.text}")
        return response.json()


def _merge_pull_request(*, repo_ref: GitHubRepoRef, pull_number: int, commit_title: str) -> dict:
    with _github_client() as client:
        mergeability_check = client.get(f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls/{pull_number}/merge")
        if mergeability_check.status_code == 204:
            pass
        elif mergeability_check.status_code == 404:
            raise GitSyncError(f"Pull request #{pull_number} is not mergeable yet or could not be found.")
        elif mergeability_check.status_code >= 400:
            pull_details = client.get(f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls/{pull_number}")
            if pull_details.status_code == 200:
                pull_payload = pull_details.json()
                mergeable_state = pull_payload.get("mergeable_state")
                draft_state = pull_payload.get("draft")
                raise GitSyncError(
                    f"Pull request #{pull_number} is not ready to merge. mergeable_state={mergeable_state!s}, draft={draft_state!s}.",
                )
            raise GitSyncError(f"GitHub mergeability check failed ({mergeability_check.status_code}): {mergeability_check.text}")

        response = client.put(
            f"/repos/{repo_ref.owner}/{repo_ref.repo}/pulls/{pull_number}/merge",
            json={
                "commit_title": commit_title,
                "merge_method": _normalized_merge_method(),
            },
        )
        if response.status_code >= 400:
            if response.status_code == 405:
                raise GitSyncError("GitHub PR merge was rejected by repository policy or merge method settings.")
            if response.status_code == 409:
                raise GitSyncError("GitHub PR merge failed because the pull request is not mergeable yet, likely due to conflicts or required checks.")
            if response.status_code == 422:
                raise GitSyncError("GitHub PR merge failed validation. Check branch protection rules, required reviews, or merge method configuration.")
            raise GitSyncError(f"GitHub PR merge failed ({response.status_code}): {response.text}")
        return response.json()


def _build_commit_message(job: GitSyncJob) -> str:
    prefix = settings.GITHUB_COMMIT_PREFIX.strip() or "chore(foundry):"
    return f"{prefix} run #{job.run_id or job.id}"


def _build_merge_commit_title(job: GitSyncJob) -> str:
    return f"Merge Foundry run #{job.run_id or job.id} into {job.base_branch}"


def _build_pr_body(job: GitSyncJob) -> str:
    run_id = job.run_id or "n/a"
    return (
        "## Foundry Automation\n"
        f"- Project: `{job.project.name}`\n"
        f"- Run ID: `{run_id}`\n"
        f"- Base branch: `{job.base_branch}`\n"
        "- Prompt content is intentionally omitted from GitHub metadata; refer to internal Foundry run history for details.\n"
    )


def _normalized_merge_method() -> str:
    configured = (settings.GITHUB_MERGE_METHOD or "squash").strip().lower()
    if configured not in {"merge", "squash", "rebase"}:
        raise GitSyncError(f"Unsupported GITHUB_MERGE_METHOD '{settings.GITHUB_MERGE_METHOD}'. Use one of: merge, squash, rebase.")
    return configured


def _repository_merge_capability_flags() -> dict[str, bool]:
    method = _normalized_merge_method()
    return {
        "allow_merge_commit": method == "merge",
        "allow_squash_merge": method == "squash",
        "allow_rebase_merge": method == "rebase",
    }


def _validate_repository_merge_capabilities(repo_payload: dict, merge_method: str) -> None:
    capability_map = {
        "merge": bool(repo_payload.get("allow_merge_commit", False)),
        "squash": bool(repo_payload.get("allow_squash_merge", False)),
        "rebase": bool(repo_payload.get("allow_rebase_merge", False)),
    }
    if capability_map.get(merge_method):
        return
    raise GitSyncError(
        f"Repository merge settings do not allow the configured merge method '{merge_method}'. "
        "Update the repository settings or change GITHUB_MERGE_METHOD.",
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
