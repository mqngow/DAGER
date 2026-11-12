"""Database connection pools, SQLAlchemy Engine, and Session management."""

from contextlib import contextmanager
from typing import Generator, Optional
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dager.config import settings


# ==============================================================================
# Declarative ORM Base Class
# Serves as the shared metadata parent for all relational tables in `models.py`.
# ==============================================================================
class Base(DeclarativeBase):
    pass


# ==============================================================================
# SQLAlchemy Engine & Session Factory Builder
# Supports both local SQLite (`check_same_thread=False` for FastAPI/Prefect
# concurrency) and pooled PostgreSQL connections in production.
# ==============================================================================
_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def get_engine(database_url: Optional[str] = None) -> Engine:
    """Create or return the singleton SQLAlchemy Engine for the configured database URI."""
    global _engine
    url = database_url or settings.database_url

    if database_url is not None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        return create_engine(url, connect_args=connect_args, future=True)

    if _engine is None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Return a configured SQLAlchemy `sessionmaker` bound to the active Engine."""
    global _SessionLocal
    if engine is not None:
        return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SessionLocal


# ==============================================================================
# Schema Initialization & Transactional Session Context Manager
# `init_db()` creates all tables idempotently; `session_scope()` manages
# commit/rollback lifecycles for repositories, Prefect tasks, and FastAPI routes.
# ==============================================================================
def init_db(engine: Optional[Engine] = None) -> None:
    """Create all registered ORM tables in the target database if they do not exist."""
    # Import models so metadata is populated before `create_all`
    import dager.storage.models  # noqa: F401

    target_engine = engine or get_engine()
    Base.metadata.create_all(bind=target_engine)


@contextmanager
def session_scope(engine: Optional[Engine] = None) -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of database operations."""
    factory = get_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

