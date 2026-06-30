import json

from django.core.management.base import BaseCommand, CommandError

from apps.workspaces.models import OrchestrationRun, OrchestrationRunActivity, OrchestrationStep, Project


class Command(BaseCommand):
    help = "Generate runtime verification evidence for a project (runs, daemon state, diff quality, recovery signals)."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", type=int, required=True, help="Project id to audit")
        parser.add_argument("--limit", type=int, default=20, help="Max number of recent runs to include")

    def handle(self, *args, **options):
        project_id = options["project_id"]
        limit = max(1, int(options["limit"]))

        project = Project.objects.filter(pk=project_id).first()
        if not project:
            raise CommandError(f"Project {project_id} not found.")

        runs = list(
            project.runs.select_related("user").order_by("-created_at")[:limit],
        )

        run_ids = [run.id for run in runs]
        steps = list(OrchestrationStep.objects.filter(run_id__in=run_ids))
        activities = list(
            OrchestrationRunActivity.objects.filter(run_id__in=run_ids).order_by("-created_at")[:500],
        )

        steps_by_run: dict[int, list[OrchestrationStep]] = {}
        for step in steps:
            steps_by_run.setdefault(step.run_id, []).append(step)

        activity_kinds_of_interest = (
            "recovery.",
            "daemon.",
            "run.cancelled",
            "run.failed",
        )

        agent_targets = {"frontend-wizard", "db-expert"}

        def _is_diff_non_empty(diff_text: str) -> bool:
            cleaned = (diff_text or "").strip()
            return bool(cleaned and cleaned not in {"[]", "{}"})

        run_summaries = []
        targeted_agent_completed_steps = 0
        targeted_agent_empty_diff_steps = 0

        for run in runs:
            run_steps = steps_by_run.get(run.id, [])
            completed_steps = [step for step in run_steps if step.status == "COMPLETED"]
            completed_with_non_empty_diff = [step for step in completed_steps if _is_diff_non_empty(step.generated_diff)]

            per_agent: dict[str, dict[str, int]] = {}
            for step in run_steps:
                bucket = per_agent.setdefault(step.assigned_agent, {"total": 0, "completed": 0, "non_empty_diff": 0, "empty_or_missing_diff": 0})
                bucket["total"] += 1
                if step.status == "COMPLETED":
                    bucket["completed"] += 1
                    if _is_diff_non_empty(step.generated_diff):
                        bucket["non_empty_diff"] += 1
                    else:
                        bucket["empty_or_missing_diff"] += 1

                    if step.assigned_agent in agent_targets:
                        targeted_agent_completed_steps += 1
                        if not _is_diff_non_empty(step.generated_diff):
                            targeted_agent_empty_diff_steps += 1

            run_activity = [
                {
                    "kind": activity.kind,
                    "level": activity.level,
                    "message": activity.message,
                    "created_at": activity.created_at.isoformat(),
                }
                for activity in activities
                if activity.run_id == run.id and activity.kind.startswith(activity_kinds_of_interest)
            ]

            run_summaries.append(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "cancellation_reason": run.cancellation_reason,
                    "current_phase": run.current_phase,
                    "last_error": run.last_error,
                    "stuck_recovery_count": run.stuck_recovery_count,
                    "sessions": {
                        "plan_session_id": run.plan_session_id,
                        "supervisor_session_id": run.supervisor_session_id,
                        "active_session_id": run.active_session_id,
                    },
                    "steps": {
                        "total": len(run_steps),
                        "completed": len(completed_steps),
                        "completed_with_non_empty_diff": len(completed_with_non_empty_diff),
                    },
                    "agent_diff_summary": per_agent,
                    "recovery_and_daemon_activity": run_activity,
                    "created_at": run.created_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                },
            )

        payload = {
            "project": {
                "id": project.id,
                "name": project.name,
                "absolute_path": project.absolute_path,
                "daemon_desired_state": project.daemon_desired_state,
                "daemon_pid": project.daemon_pid,
                "allocated_port": project.allocated_port,
                "daemon_last_heartbeat_at": project.daemon_last_heartbeat_at.isoformat() if project.daemon_last_heartbeat_at else None,
                "daemon_stop_requested_at": project.daemon_stop_requested_at.isoformat() if project.daemon_stop_requested_at else None,
                "is_locked": project.is_locked,
                "lock_acquired_at": project.lock_acquired_at.isoformat() if project.lock_acquired_at else None,
            },
            "runs": run_summaries,
            "no_empty_diff_loop_check": {
                "target_agents": sorted(agent_targets),
                "completed_steps": targeted_agent_completed_steps,
                "empty_or_missing_diff_steps": targeted_agent_empty_diff_steps,
                "pass": targeted_agent_empty_diff_steps == 0,
            },
        }

        self.stdout.write(json.dumps(payload, indent=2))
