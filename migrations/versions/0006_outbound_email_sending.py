"""outbound email sending: Message-ID, Gmail ids, sent_at

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbound_emails", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rfc822_message_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("gmail_message_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("gmail_thread_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("outbound_emails", schema=None) as batch_op:
        batch_op.drop_column("sent_at")
        batch_op.drop_column("gmail_thread_id")
        batch_op.drop_column("gmail_message_id")
        batch_op.drop_column("rfc822_message_id")
