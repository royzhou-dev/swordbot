"""initial schema: users and support cases

Revision ID: 0001
Revises:
Create Date: 2026-09-26 16:26:46.282571

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("signature_name", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_users_telegram_user_id")),
    )
    op.create_table(
        "support_cases",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("merchant_name", sa.String(length=200), nullable=True),
        sa.Column("merchant_domain", sa.String(length=253), nullable=True),
        sa.Column("issue_type", sa.String(length=64), nullable=True),
        sa.Column("issue_summary", sa.Text(), nullable=True),
        sa.Column("desired_resolution", sa.Text(), nullable=True),
        sa.Column("order_number", sa.String(length=128), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("support_email", sa.String(length=320), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=64), nullable=True),
        sa.Column("auto_reply_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("focused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_support_cases_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_cases")),
    )
    op.create_index(
        op.f("ix_support_cases_gmail_thread_id"), "support_cases", ["gmail_thread_id"], unique=False
    )
    op.create_index(
        "ix_support_cases_user_id_status", "support_cases", ["user_id", "status"], unique=False
    )
    op.create_index(
        "uq_support_cases_one_focused_per_user",
        "support_cases",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("focused"),
        sqlite_where=sa.text("focused"),
    )

    op.create_table(
        "case_facts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["support_cases.id"], name=op.f("fk_case_facts_case_id_support_cases")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_case_facts_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_facts")),
    )
    op.create_index("ix_case_facts_case_id_key", "case_facts", ["case_id", "key"], unique=False)

    op.create_table(
        "case_transitions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["support_cases.id"],
            name=op.f("fk_case_transitions_case_id_support_cases"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_case_transitions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_transitions")),
    )
    op.create_index(
        op.f("ix_case_transitions_case_id"), "case_transitions", ["case_id"], unique=False
    )


def downgrade() -> None:
    # Dropping a table drops its indexes.
    op.drop_table("case_transitions")
    op.drop_table("case_facts")
    op.drop_table("support_cases")
    op.drop_table("users")
