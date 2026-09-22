from models import AnalysisJob, JobLog, JobRun, JobStatus
from services import job_service


def test_create_analysis_job(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(
        db_session, customer.id, databricks_connection, request="Analyze my cluster", intent="cluster"
    )
    assert job.id
    assert job.status == JobStatus.QUEUED.value
    assert job.customer_id == customer.id
    assert job.connection_id == databricks_connection.id


def test_create_job_run_parent_child_relationship(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(db_session, customer.id, databricks_connection, "Analyze everything", "all")
    cluster_run = job_service.create_job_run(db_session, job, "cluster")
    query_run = job_service.create_job_run(db_session, job, "query")
    storage_run = job_service.create_job_run(db_session, job, "storage")

    db_session.refresh(job)
    run_ids = {r.id for r in job.job_runs}
    assert run_ids == {cluster_run.id, query_run.id, storage_run.id}
    assert all(r.analysis_job_id == job.id for r in job.job_runs)


def test_job_log_persistence(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(db_session, customer.id, databricks_connection, "Analyze my cluster", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    job_service.append_log(db_session, run, "INFO", "Starting analysis.")
    job_service.append_log(db_session, run, "ERROR", "Something went wrong.")

    # RUN_CREATED is a structured event; this test is about plain log lines.
    logs = db_session.query(JobLog).filter(JobLog.job_run_id == run.id, JobLog.event_type.is_(None)).all()
    assert len(logs) == 2
    assert {l.level for l in logs} == {"INFO", "ERROR"}


def test_status_enum_terminal_set():
    terminal = JobStatus.terminal()
    assert JobStatus.COMPLETED in terminal
    assert JobStatus.FAILED in terminal
    assert JobStatus.CANCELLED in terminal
    assert JobStatus.RUNNING not in terminal
    assert JobStatus.QUEUED not in terminal


def test_recompute_analysis_job_status_all_completed(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(db_session, customer.id, databricks_connection, "Analyze everything", "all")
    r1 = job_service.create_job_run(db_session, job, "cluster")
    r2 = job_service.create_job_run(db_session, job, "query")

    r1.status = JobStatus.COMPLETED.value
    r2.status = JobStatus.COMPLETED.value
    db_session.commit()

    job_service._recompute_analysis_job_status(db_session, job.id)
    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED.value


def test_recompute_analysis_job_status_one_failed(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(db_session, customer.id, databricks_connection, "Analyze everything", "all")
    r1 = job_service.create_job_run(db_session, job, "cluster")
    r2 = job_service.create_job_run(db_session, job, "query")

    r1.status = JobStatus.COMPLETED.value
    r2.status = JobStatus.FAILED.value
    db_session.commit()

    job_service._recompute_analysis_job_status(db_session, job.id)
    db_session.refresh(job)
    assert job.status == JobStatus.FAILED.value


def test_recompute_analysis_job_status_still_running(db_session, databricks_connection, customer):
    job = job_service.create_analysis_job(db_session, customer.id, databricks_connection, "Analyze everything", "all")
    r1 = job_service.create_job_run(db_session, job, "cluster")
    r2 = job_service.create_job_run(db_session, job, "query")

    r1.status = JobStatus.COMPLETED.value
    r2.status = JobStatus.RUNNING.value
    db_session.commit()

    job_service._recompute_analysis_job_status(db_session, job.id)
    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING.value
