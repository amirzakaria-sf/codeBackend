from celery import shared_task

from .services.github_sync import perform_github_sync
from .services.recovery import recover_stuck_run, scan_and_enqueue_stuck_runs
from .services.orchestration import perform_orchestration_run


@shared_task(bind=True)
def execute_orchestration_run_task(self, run_id: int):
    return perform_orchestration_run(run_id)


@shared_task(bind=True)
def scan_stuck_runs_task(self):
    enqueued = scan_and_enqueue_stuck_runs()
    return {"enqueued_run_ids": enqueued, "count": len(enqueued)}


@shared_task(bind=True)
def recover_stuck_run_task(self, run_id: int):
    return recover_stuck_run(run_id)


@shared_task(bind=True)
def execute_github_sync_task(self, sync_job_id: int):
    return perform_github_sync(sync_job_id)
