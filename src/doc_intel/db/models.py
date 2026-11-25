"""SQLAlchemy models. Source of truth: docs/data-model.md — change names there first."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _in(column: str, *values: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def _tenant_fk(table: str) -> ForeignKeyConstraint:
    # Composite FK: a child row can never point at another user's project (ADR-009).
    return ForeignKeyConstraint(
        ["project_id", "user_id"],
        ["projects.id", "projects.user_id"],
        ondelete="CASCADE",
        name=f"fk_{table}_project_tenant",
    )


def _recent_index(table: str) -> Index:
    return Index(f"ix_{table}_tenant_recent", "user_id", "project_id", text("created_at DESC"))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),
        UniqueConstraint("user_id", "openwebui_chat_id"),
        Index("ix_projects_user_recent", "user_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    openwebui_chat_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        _tenant_fk("documents"),
        UniqueConstraint("project_id", "sha256"),
        _in("role", "source", "template"),
        _in("status", "uploaded", "parsing", "indexed", "failed"),
        _recent_index("documents"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="source")
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    openwebui_file_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        _tenant_fk("jobs"),
        _in("kind", "ingest_document", "extract_template", "run_mapping", "render_artifact"),
        _in("status", "queued", "running", "succeeded", "failed"),
        Index("ix_jobs_queued", "created_at", postgresql_where=text("status = 'queued'")),
        _recent_index("jobs"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (_tenant_fk("templates"), _in("format", "pptx", "docx", "xlsx"))

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    format: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    anchor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _ts()


class TemplateIRVersion(Base):
    __tablename__ = "template_irs"
    __table_args__ = (_tenant_fk("template_irs"), UniqueConstraint("template_id", "version"))

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ir: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _ts()


class MappingRun(Base):
    __tablename__ = "mapping_runs"
    __table_args__ = (
        _tenant_fk("mapping_runs"),
        UniqueConstraint("template_ir_id", "version"),
        _in("trigger", "initial", "edit"),
        _in(
            "status",
            "queued",
            "retrieving",
            "extracting",
            "verifying",
            "rendering",
            "completed",
            "failed",
        ),
        _recent_index("mapping_runs"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # No ON DELETE: an IR or run that still has runs/artifacts can't be deleted (API returns 409).
    template_ir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("template_irs.id"), nullable=False
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mapping_runs.id")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    edits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        _tenant_fk("artifacts"),
        _in("format", "pptx", "docx", "xlsx"),
        _recent_index("artifacts"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mapping_runs.id"), nullable=False, unique=True
    )
    format: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _ts()


class LibraryTemplate(Base):
    """Global read-only catalog of template files with a prebuilt AnchorMap and IR.

    Tenancy exception: no user_id/project_id. Written by the operator (CLI,
    `/v1/library` ADR-014, or the `templates/` sync ADR-017); users get a per-project copy
    through `templates.library.adopt`.
    """

    __tablename__ = "library_templates"
    __table_args__ = (_in("format", "pptx", "docx", "xlsx"),)

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    category: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    anchor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ir: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # IR_BUILDER_VERSION at extraction; the templates/ sync re-extracts older rows (ADR-017)
    ir_builder_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _ts()
