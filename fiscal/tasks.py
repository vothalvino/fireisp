from celery import shared_task
from .jobs import dispatch_jobs, run_job


@shared_task(name='fiscal.tasks.process_fiscal_job', queue='fiscal', soft_time_limit=90, time_limit=120,
             acks_late=True, reject_on_worker_lost=True)
def process_fiscal_job(job_id):
    return run_job(job_id)


@shared_task(name='fiscal.tasks.dispatch_fiscal_jobs', queue='fiscal', soft_time_limit=90, time_limit=120)
def dispatch_fiscal_jobs():
    return dispatch_jobs()
