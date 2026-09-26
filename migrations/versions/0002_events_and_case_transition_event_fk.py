"""events table and case_transitions event fk

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 16:54:27.438744

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Integer ids, the same type as the log tables' (PLAN D9).
_LOG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", _LOG_ID, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["support_cases.id"], name=op.f("fk_events_case_id_support_cases")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_events_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("source", "external_id", name="uq_events_source_external_id"),
    )
    op.create_index("ix_events_status_run_at", "events", ["status", "run_at"], unique=False)
    op.create_index("ix_events_user_id_status", "events", ["user_id", "status"], unique=False)
    op.create_index(
        "uq_events_one_processing_per_user",
        "events",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
        sqlite_where=sa.text("status = 'processing'"),
    )

    # case_transitions.event_id: UUID placeholder -> integer FK to events. No
    # events existed before this revision, so any old value refers to nothing.
    op.execute("UPDATE case_transitions SET event_id = NULL")
    with op.batch_alter_table("case_transitions", schema=None) as batch_op:
        batch_op.alter_column(
            "event_id",
            existing_type=sa.Uuid(),
            type_=_LOG_ID,
            existing_nullable=True,
            postgresql_using="NULL::bigint",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_case_transitions_event_id_events"), "events", ["event_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("case_transitions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_case_transitions_event_id_events"), type_="foreignkey"
        )
    op.execute("UPDATE case_transitions SET event_id = NULL")
    with op.batch_alter_table("case_transitions", schema=None) as batch_op:
        batch_op.alter_column(
            "event_id",
            existing_type=_LOG_ID,
            type_=sa.Uuid(),
            existing_nullable=True,
            postgresql_using="NULL::uuid",
        )

    # Dropping a table drops its indexes.
    op.drop_table("events")
