"""Data model (SQLAlchemy 2.x).

Master data      : roles, users, materials, material_lots, equipment, master_recipes, recipe_steps
Execution        : batches, step_executions, block_executions, data_entries, ipc_samples
GMP records      : signatures, deviations, audit_trail (append-only)

All datetimes are UTC.
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# --------------------------------------------------------------------------- master data
class Role(Base):
    __tablename__ = "roles"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)   # OPERATOR | SUPERVISOR | QA | ADMIN
    description: Mapped[str] = mapped_column(String(200))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    full_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(ForeignKey("roles.code"))
    password_hash: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Material(Base):
    __tablename__ = "materials"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    material_type: Mapped[str] = mapped_column(String(20))   # API | EXCIPIENT
    uom: Mapped[str] = mapped_column(String(10))


class MaterialLot(Base):
    __tablename__ = "material_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_no: Mapped[str] = mapped_column(String(30), unique=True)
    material_code: Mapped[str] = mapped_column(ForeignKey("materials.code"))
    status: Mapped[str] = mapped_column(String(20))           # QUARANTINE | RELEASED | REJECTED
    expiry_date: Mapped[date] = mapped_column(Date)
    qty_available: Mapped[float] = mapped_column(Float)


class Equipment(Base):
    __tablename__ = "equipment"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    equipment_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))           # CLEAN | DIRTY
    calibration_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    punch_set_id: Mapped[str | None] = mapped_column(String(30), nullable=True)


class MasterRecipe(Base):
    __tablename__ = "master_recipes"
    __table_args__ = (UniqueConstraint("code", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(150))
    product: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="APPROVED")
    batch_size: Mapped[float] = mapped_column(Float)
    batch_size_uom: Mapped[str] = mapped_column(String(20))
    batch_prefix: Mapped[str] = mapped_column(String(10))
    definition: Mapped[dict] = mapped_column(JSON)            # the full JSON as imported
    created_at: Mapped[datetime] = mapped_column(DateTime)
    steps: Mapped[list["RecipeStep"]] = relationship(order_by="RecipeStep.seq", back_populates="recipe")


class RecipeStep(Base):
    __tablename__ = "recipe_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("master_recipes.id"))
    seq: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    blocks: Mapped[list] = mapped_column(JSON)                # [{"type": "...", "config": {...}}, ...]
    recipe: Mapped[MasterRecipe] = relationship(back_populates="steps")


# --------------------------------------------------------------------------- execution
class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(30), unique=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("master_recipes.id"))
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disposition_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disposition_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    disposition_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[MasterRecipe] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    disposition_by: Mapped[User | None] = relationship(foreign_keys=[disposition_by_id])
    steps: Mapped[list["StepExecution"]] = relationship(order_by="StepExecution.seq", back_populates="batch")


class StepExecution(Base):
    __tablename__ = "step_executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    recipe_step_id: Mapped[int] = mapped_column(ForeignKey("recipe_steps.id"))
    seq: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="LOCKED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="steps")
    started_by: Mapped[User | None] = relationship()
    blocks: Mapped[list["BlockExecution"]] = relationship(order_by="BlockExecution.idx", back_populates="step")


class BlockExecution(Base):
    """One building block inside a step. The config is copied from the recipe when the batch is
    created, so a later recipe change never alters a batch that is already running."""
    __tablename__ = "block_executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    step_execution_id: Mapped[int] = mapped_column(ForeignKey("step_executions.id"))
    idx: Mapped[int] = mapped_column(Integer)
    block_type: Mapped[str] = mapped_column(String(30))
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")   # PENDING | COMPLETED
    state: Mapped[dict] = mapped_column(JSON, default=dict)               # e.g. signature ids
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    step: Mapped[StepExecution] = relationship(back_populates="blocks")
    entries: Mapped[list["DataEntry"]] = relationship(order_by="DataEntry.id", back_populates="block")
    samples: Mapped[list["IpcSample"]] = relationship(order_by="IpcSample.id", back_populates="block")


class DataEntry(Base):
    """One recorded value: a parameter, checklist answer, dispensed weight, equipment used, yield..."""
    __tablename__ = "data_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    block_execution_id: Mapped[int] = mapped_column(ForeignKey("block_executions.id"))
    key: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(150))
    value: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    limits: Mapped[str | None] = mapped_column(String(60), nullable=True)   # human-readable, e.g. "55 – 65"
    in_limits: Mapped[bool] = mapped_column(Boolean, default=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)           # False = rejected weighing attempt
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    original_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)               # lot, balance, target, ...
    entered_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    entered_at: Mapped[datetime] = mapped_column(DateTime)

    block: Mapped[BlockExecution] = relationship(back_populates="entries")
    entered_by: Mapped[User] = relationship()


class IpcSample(Base):
    __tablename__ = "ipc_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    block_execution_id: Mapped[int] = mapped_column(ForeignKey("block_executions.id"))
    sample_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))            # TAKEN | MISSED
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    taken_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    results: Mapped[list] = mapped_column(JSON, default=list)  # [{key,label,value,unit,limits,in_limits}]
    in_limits: Mapped[bool] = mapped_column(Boolean, default=True)

    block: Mapped[BlockExecution] = relationship(back_populates="samples")
    taken_by: Mapped[User | None] = relationship()


# --------------------------------------------------------------------------- GMP records
class Signature(Base):
    """21 CFR 11.50: signed record shows printed name, date/time and meaning."""
    __tablename__ = "signatures"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    username: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(100))
    meaning: Mapped[str] = mapped_column(String(20))           # PERFORMED | VERIFIED | REVIEWED | APPROVED | REJECTED
    utc_ts: Mapped[datetime] = mapped_column(DateTime)
    entity: Mapped[str] = mapped_column(String(30))            # what was signed: block | deviation | batch
    entity_id: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)


class Deviation(Base):
    __tablename__ = "deviations"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    step_execution_id: Mapped[int | None] = mapped_column(ForeignKey("step_executions.id"), nullable=True)
    related_entry_id: Mapped[int | None] = mapped_column(ForeignKey("data_entries.id"), nullable=True)
    related_sample_id: Mapped[int | None] = mapped_column(ForeignKey("ipc_samples.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(10))            # AUTO | MANUAL
    category: Mapped[str] = mapped_column(String(30))          # OUT_OF_LIMIT | CHECKLIST | MISSED_IPC | YIELD | WEIGHT | OTHER
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")   # OPEN | CLOSED
    raised_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    raised_at: Mapped[datetime] = mapped_column(DateTime)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)

    step: Mapped[StepExecution | None] = relationship()
    raised_by: Mapped[User] = relationship(foreign_keys=[raised_by_id])
    closed_by: Mapped[User | None] = relationship(foreign_keys=[closed_by_id])


class AuditTrail(Base):
    """URS-008 / 21 CFR 11.10(e). Insert-only: no API route updates or deletes it, and
    SQLite triggers (db.AUDIT_TRIGGERS) abort any UPDATE or DELETE."""
    __tablename__ = "audit_trail"
    id: Mapped[int] = mapped_column(primary_key=True)
    utc_ts: Mapped[datetime] = mapped_column(DateTime)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    field: Mapped[str | None] = mapped_column(String(60), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # for filtering a batch's history
