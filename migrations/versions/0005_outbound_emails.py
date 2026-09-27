"""outbound emails

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 23:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The type of events.id (PLAN D9).
_LOG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_LIVE_WHERE = sa.text("status IN ('awaiting_approval', 'approved', 'sending')")


def upgrade() -> None:
    op.create_table(
        "outbound_emails",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("signature_name", sa.String(length=200), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_event_id", _LOG_ID, nullable=True),
        sa.Column("action_group_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_event_id", _LOG_ID, nullable=True),
        sa.Column("approved_by_action_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_action_id"],
            ["pending_actions.id"],
            name=op.f("fk_outbound_emails_approved_by_action_id_pending_actions"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_event_id"],
            ["events.id"],
            name=op.f("fk_outbound_emails_approved_by_event_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["support_cases.id"], name=op.f("fk_outbound_emails_case_id_support_cases")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_event_id"],
            ["events.id"],
            name=op.f("fk_outbound_emails_created_by_event_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["outbound_emails.id"],
            name=op.f("fk_outbound_emails_supersedes_id_outbound_emails"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_outbound_emails_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbound_emails")),
        sa.UniqueConstraint("case_id", "version", name=op.f("uq_outbound_emails_case_id")),
    )
    op.create_index(
        "uq_outbound_emails_one_live_per_case",
        "outbound_emails",
        ["case_id"],
        unique=True,
        postgresql_where=_LIVE_WHERE,
        sqlite_where=_LIVE_WHERE,
    )


def downgrade() -> None:
    # Dropping a table drops its indexes.
    op.drop_table("outbound_emails")
