"""case messages; drop echo test buttons

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26 23:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The type of the log tables' ids and events.id (PLAN D9).
_LOG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "case_messages",
        sa.Column("id", _LOG_ID, autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("event_id", _LOG_ID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["support_cases.id"], name=op.f("fk_case_messages_case_id_support_cases")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_case_messages_event_id_events")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_case_messages_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_messages")),
    )
    op.create_index("ix_case_messages_case_id_id", "case_messages", ["case_id", "id"], unique=False)
    # The M3 echo test button is gone from the code; an old one would no longer
    # load, so remove any left in a development database.
    op.execute("DELETE FROM pending_actions WHERE kind = 'echo_test'")


def downgrade() -> None:
    # Dropping a table drops its indexes. Deleted test buttons are not restored.
    op.drop_table("case_messages")
