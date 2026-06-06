from datetime import datetime
from pathlib import Path
import shutil

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from storage_paths import DATA_DIR

DATABASE_PATH = DATA_DIR / "app.db"
OLD_DATABASE_PATH = Path("isir.db")

DATA_DIR.mkdir(parents=True, exist_ok=True)
if not getattr(__import__("sys"), "frozen", False) and not DATABASE_PATH.exists() and OLD_DATABASE_PATH.exists():
    shutil.copy2(OLD_DATABASE_PATH, DATABASE_PATH)

DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"

Base = declarative_base()


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("first_name", "last_name", "birth_date", name="uq_client_identity"),
    )

    id = Column(Integer, primary_key=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    birth_date = Column(Date, nullable=False)
    project = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    last_checked_at = Column(DateTime)
    insolvency_status = Column(String(255), default="Nezkontrolováno", nullable=False)
    last_found_change = Column(Text)
    last_result_hash = Column(String(64))
    change_seen_at = Column(DateTime)

    changes = relationship(
        "InsolvencyChange",
        back_populates="client",
        cascade="all, delete-orphan",
        order_by="desc(InsolvencyChange.created_at)",
    )
    cases = relationship(
        "InsolvencyCase",
        back_populates="client",
        cascade="all, delete-orphan",
        order_by="InsolvencyCase.spisova_znacka",
    )


class InsolvencyCase(Base):
    __tablename__ = "insolvency_cases"

    id = Column(Integer, primary_key=True)
    client_id = Column(ForeignKey("clients.id"), nullable=False)
    spisova_znacka = Column(String(80), nullable=False)
    debtor_name = Column(String(255))
    address = Column(Text)
    state = Column(String(255))
    detail_url = Column(Text)
    proceeding_started_at = Column(DateTime)
    started_at = Column(Date)
    ended_at = Column(Date)

    last_event_id = Column(Integer)
    last_event_at = Column(DateTime)
    last_event_type = Column(String(255))
    last_event_description = Column(Text)
    document_url = Column(Text)
    document_count = Column(Integer, default=0, nullable=False)
    claims_deadline = Column(String(120))
    claims_total_amount = Column(Text)
    claims_count = Column(Integer)
    ai_checked_at = Column(DateTime)
    ai_model = Column(String(100))
    ai_category = Column(String(255))
    ai_summary = Column(Text)
    ai_key_points = Column(Text)
    ai_deadlines = Column(Text)
    ai_recommended_action = Column(Text)
    ai_raw_result = Column(Text)
    ai_case_study_at = Column(DateTime)
    ai_case_study = Column(Text)
    raw_result = Column(Text)

    client = relationship("Client", back_populates="cases")
    documents = relationship(
        "InsolvencyDocument",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="desc(InsolvencyDocument.event_at)",
    )


class InsolvencyDocument(Base):
    __tablename__ = "insolvency_documents"

    id = Column(Integer, primary_key=True)
    case_id = Column(ForeignKey("insolvency_cases.id"), nullable=False)
    event_at = Column(DateTime)
    title = Column(String(255), nullable=False)
    document_type = Column(String(40), nullable=False)
    source_url = Column(Text, nullable=False)
    local_path = Column(Text)
    file_size = Column(Integer)
    deleted_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("InsolvencyCase", back_populates="documents")


class InsolvencyChange(Base):
    __tablename__ = "insolvency_changes"

    id = Column(Integer, primary_key=True)
    client_id = Column(ForeignKey("clients.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    description = Column(Text, nullable=False)
    raw_result = Column(Text)

    client = relationship("Client", back_populates="changes")


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    migrate_db()


def migrate_db() -> None:
    columns = {
        row[1]
        for row in engine.connect().execute(text("PRAGMA table_info(insolvency_cases)")).fetchall()
    }

    migrations = []
    if "proceeding_started_at" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN proceeding_started_at DATETIME")
    if "document_count" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN document_count INTEGER NOT NULL DEFAULT 0")
    if "claims_deadline" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN claims_deadline VARCHAR(120)")
    if "claims_total_amount" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN claims_total_amount VARCHAR(120)")
    if "claims_count" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN claims_count INTEGER")
    if "ai_checked_at" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_checked_at DATETIME")
    if "ai_model" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_model VARCHAR(100)")
    if "ai_category" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_category VARCHAR(255)")
    if "ai_summary" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_summary TEXT")
    if "ai_key_points" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_key_points TEXT")
    if "ai_deadlines" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_deadlines TEXT")
    if "ai_recommended_action" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_recommended_action TEXT")
    if "ai_raw_result" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_raw_result TEXT")
    if "ai_case_study_at" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_case_study_at DATETIME")
    if "ai_case_study" not in columns:
        migrations.append("ALTER TABLE insolvency_cases ADD COLUMN ai_case_study TEXT")

    if migrations:
        with engine.begin() as connection:
            for migration in migrations:
                connection.execute(text(migration))

    document_columns = {
        row[1]
        for row in engine.connect().execute(text("PRAGMA table_info(insolvency_documents)")).fetchall()
    }
    document_migrations = []
    if "deleted_at" not in document_columns:
        document_migrations.append("ALTER TABLE insolvency_documents ADD COLUMN deleted_at DATETIME")

    if document_migrations:
        with engine.begin() as connection:
            for migration in document_migrations:
                connection.execute(text(migration))

    client_columns = {
        row[1]
        for row in engine.connect().execute(text("PRAGMA table_info(clients)")).fetchall()
    }
    client_migrations = []
    if "change_seen_at" not in client_columns:
        client_migrations.append("ALTER TABLE clients ADD COLUMN change_seen_at DATETIME")
    if "project" not in client_columns:
        client_migrations.append("ALTER TABLE clients ADD COLUMN project VARCHAR(100)")

    if client_migrations:
        with engine.begin() as connection:
            for migration in client_migrations:
                connection.execute(text(migration))
