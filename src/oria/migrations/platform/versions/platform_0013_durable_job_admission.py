"""Persist owned sessions, idempotent jobs and atomic job state events."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0013"
down_revision = "platform_0012"
branch_labels = None
depends_on = None

_OWNER = (
    "tenant_id",
    "session_id",
    "requester_subject_id",
    "requester_issuer",
    "requester_external_subject",
)
_STATUSES = (
    "'queued','running','waiting_approval','waiting_event','retry_scheduled',"
    "'cancellation_requested','cancelled','succeeded','failed'"
)


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("requester_subject_id", sa.String(), nullable=False),
        sa.Column("requester_issuer", sa.String(), nullable=False),
        sa.Column("requester_external_subject", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "session_id"),
        sa.UniqueConstraint(*_OWNER, name="uq_sessions_owner"),
    )
    op.create_table(
        "jobs",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("requester_subject_id", sa.String(), nullable=False),
        sa.Column("requester_issuer", sa.String(), nullable=False),
        sa.Column("requester_external_subject", sa.String(), nullable=False),
        sa.Column("scenario", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("schedule_policy", sa.String(), nullable=False),
        sa.Column("webhook_id", sa.String(), nullable=True),
        sa.Column("accepted_checkpoint_ns", sa.String(), nullable=True),
        sa.Column("accepted_checkpoint_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "job_id"),
        sa.ForeignKeyConstraint(list(_OWNER), [f"sessions.{name}" for name in _OWNER]),
        sa.UniqueConstraint(
            "tenant_id", "requester_subject_id", "idempotency_key", name="uq_jobs_idempotency"
        ),
        sa.UniqueConstraint("tenant_id", "thread_id", name="uq_jobs_thread"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_jobs_status"),
        sa.CheckConstraint("scenario IN ('campaign','attribution')", name="ck_jobs_scenario"),
        sa.CheckConstraint(
            "revision >= 0 AND attempt >= 0 AND attempt <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 10 AND lease_epoch >= 0",
            name="ck_jobs_budget",
        ),
        sa.CheckConstraint("schedule_policy = 'once'", name="ck_jobs_schedule"),
        sa.CheckConstraint(
            "(accepted_checkpoint_ns IS NULL) = (accepted_checkpoint_id IS NULL)",
            name="ck_jobs_checkpoint",
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["tenant_id", "status", "created_at"])
    op.create_table(
        "job_events",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "job_id", "sequence"),
        sa.ForeignKeyConstraint(["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"]),
        sa.CheckConstraint("sequence >= 0", name="ck_job_events_sequence"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_job_events_status"),
        sa.CheckConstraint("event_type = 'job.' || status", name="ck_job_events_type"),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in ("sessions", "jobs", "job_events"):
            op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
            op.execute(
                sa.text(
                    f'CREATE POLICY oria_tenant_isolation ON "{table}" '
                    "USING (tenant_id = NULLIF(current_setting('oria.tenant_id', true), '')) "
                    "WITH CHECK (tenant_id = NULLIF(current_setting('oria.tenant_id', true), ''))"
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("LOCK TABLE sessions, jobs, job_events IN ACCESS EXCLUSIVE MODE")
        )
        for table in ("sessions", "jobs", "job_events"):
            if connection.execute(
                sa.text("SELECT row_security_active(:table)"), {"table": table}
            ).scalar_one():
                raise ValueError("cannot verify unfiltered job data for downgrade")
    for table in ("job_events", "jobs", "sessions"):
        if connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one():
            raise ValueError("cannot downgrade while durable job or session data exists")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("sessions")
