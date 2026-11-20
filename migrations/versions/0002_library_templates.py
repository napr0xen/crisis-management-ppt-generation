"""library templates

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "library_templates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("anchor_map", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("anchor_count", sa.Integer(), nullable=False),
        sa.Column("ir", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('pptx', 'docx', 'xlsx')", name=op.f("ck_library_templates_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_library_templates_name")),
        sa.UniqueConstraint("sha256", name=op.f("uq_library_templates_sha256")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("library_templates")
