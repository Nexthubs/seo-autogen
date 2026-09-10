"""Database + artifact backup (spec P9 "Backup").

One command produces a timestamped tar.gz archive containing:

* ``db/*.jsonl`` — every table, one JSON object per row (``id`` always
  first so restore order is stable), plus ``db/manifest.json``
  (timestamp, table row counts, app version marker).
* ``artifacts.tar`` — everything under ``{data_dir}/articles`` (generated
  images) packed as an inner tar with arcname ``articles``.

Archives land in ``BACKUP_DIR`` (default ``{data_dir}/backups``) and the
oldest ones beyond ``BACKUP_KEEP`` are pruned automatically.

Restore re-imports the ``db`` section into a target database
(``--target`` URL, default: the configured ``DATABASE_URL``). Rows are
replaced by primary key; optional ``--clear`` empties every table first.
This is a manual, explicit operation — never run on a timer.

Usage::

    python3 -m app.ops.backup create
    python3 -m app.ops.backup list
    python3 -m app.ops.backup restore <archive> [--clear] [--target URL]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import tarfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base

logger = logging.getLogger(__name__)

APP_MARKER = "seo-autogen"


# ------------------------------------------------------------ helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def serialize_value(value: Any) -> Any:
    """Make one cell JSON-serializable (round-trippable by ``_restore_row``)."""
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize_value(v) for k, v in value.items()}
    return value


def _deserialize_datetime(value: Any) -> Any:
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return value


def _restore_row(model: type, payload: dict) -> Any:
    """Build an ORM instance from a JSONL payload (no session).

    Reverse of ``serialize_value``: hex strings become UUID objects for
    every ``UUID`` column, ISO strings become aware datetimes, numeric
    strings become ``Decimal``.
    """
    from sqlalchemy import DateTime, Numeric
    from sqlalchemy.dialects.postgresql import UUID as PGUUID

    kwargs: dict[str, Any] = {}
    for column in model.__table__.columns:
        if column.name not in payload:
            continue
        value = payload[column.name]
        if value is not None:
            if isinstance(column.type, PGUUID):
                value = _pk_value(model, value)
            elif isinstance(column.type, DateTime):
                value = _deserialize_datetime(value)
            elif isinstance(column.type, Numeric):
                value = Decimal(value)
        kwargs[column.name] = value
    return model(**kwargs)


def table_order() -> list[type]:
    """FK topological order (parents first) over every mapped model.

    Derived from ``Base.metadata.sorted_tables`` (a stable FK-safe order
    on both Postgres and SQLite).
    """
    from sqlalchemy.orm import configure_mappers

    configure_mappers()
    by_table_name = {
        mapper.local_table.name: mapper.class_
        for mapper in Base.registry.mappers
    }
    return [
        by_table_name[table.name] for table in Base.metadata.sorted_tables
    ]


def _row_to_payload(model: type, row: Any) -> dict:
    payload: dict[str, Any] = {}
    id_column = model.__table__.primary_key.columns[0]
    payload[id_column.name] = serialize_value(getattr(row, id_column.name))
    for column in model.__table__.columns:
        if column is id_column:
            continue
        payload[column.name] = serialize_value(getattr(row, column.name))
    return payload


# ------------------------------------------------------------ backup
def backup_db_jsonl(engine: Engine) -> dict[str, bytes]:
    """Serialize every table to JSONL bytes (``db/<table>.jsonl`` keys)."""
    out: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    with Session(bind=engine) as session:
        for model in table_order():
            lines = []
            n = 0
            for row in session.scalars(select(model)):
                lines.append(json.dumps(_row_to_payload(model, row), ensure_ascii=False))
                n += 1
            counts[model.__tablename__] = n
            out[f"db/{model.__tablename__}.jsonl"] = (
                "\n".join(lines).encode("utf-8") if lines else b""
            )
    manifest = {
        "app": APP_MARKER,
        "created_at": _utcnow().isoformat(),
        "tables": counts,
    }
    out["db/manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    return out


def backup_artifacts(data_dir: Path) -> dict[str, bytes]:
    """Serialize ``{data_dir}/articles`` into tar bytes (empty-safe)."""
    root = Path(data_dir) / "articles"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if root.exists():
            tar.add(str(root), arcname="articles")
    buf.seek(0)
    return {"artifacts.tar": buf.read()}


def archive_path(settings: Settings) -> Path:
    """Directory holding backup archives."""
    if settings.backup_dir:
        return Path(settings.backup_dir)
    return Path(settings.data_dir) / "backups"


def list_backups(settings: Settings | None = None) -> list[Path]:
    """Existing ``seo_backup_*.tar.gz`` archives, oldest first."""
    settings = settings or get_settings()
    d = archive_path(settings)
    if not d.exists():
        return []
    return sorted(d.glob("seo_backup_*.tar.gz"))


def prune_backups(settings: Settings | None = None, keep: int | None = None) -> list[Path]:
    """Delete the oldest archives beyond ``keep``; returns removed paths."""
    settings = settings or get_settings()
    keep = settings.backup_keep if keep is None else keep
    files = list_backups(settings)
    if len(files) <= keep:
        return []
    removed = []
    for path in files[: len(files) - keep]:
        path.unlink()
        removed.append(path)
    return removed


def create_backup(
    settings: Settings | None = None,
    engine: Engine | None = None,
    keep: int | None = None,
) -> Path:
    """Build one archive (db JSONL + artifact tar), prune, return its path."""
    settings = settings or get_settings()
    from app.db.session import engine as default_engine

    engine = engine or default_engine
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    db_files = backup_db_jsonl(engine)
    artifact_files = backup_artifacts(Path(settings.data_dir))

    target_dir = archive_path(settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"seo_backup_{stamp}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in {**db_files, **artifact_files}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(_utcnow().timestamp())
            tar.addfile(info, io.BytesIO(data))

    removed = prune_backups(settings, keep=keep)
    logger.info(
        "backup_created",
        extra={
            "event": "backup_created",
            "archive": str(path),
            "tables": len([k for k in db_files if k.endswith(".jsonl")]),
            "pruned": [str(p) for p in removed],
        },
    )
    return path


# ------------------------------------------------------------ restore
def iter_archive_db(archive: Path) -> Iterable[tuple[str, list[dict]]]:
    """Yield ``(table_name, rows)`` from an archive's ``db`` section."""
    with tarfile.open(archive, "r:gz") as tar:
        members = {
            m.name: m
            for m in tar.getmembers()
            if m.isfile() and m.name.startswith("db/") and m.name.endswith(".jsonl")
        }
        for name in sorted(members, key=lambda n: n.removeprefix("db/")):
            fh = tar.extractfile(members[name])
            assert fh is not None
            rows = [
                json.loads(line)
                for line in io.TextIOWrapper(fh, encoding="utf-8") if line.strip()
            ]
            table = name.removeprefix("db/").removesuffix(".jsonl")
            yield table, rows


def _pk_value(model: type, value: Any) -> Any:
    """Rebuild a primary key value from its JSONL form (hex → UUID)."""
    if isinstance(value, str):
        try:
            return uuid.UUID(value.replace("-", ""))
        except ValueError:
            return value
    return value


def restore_archive(
    archive: Path,
    target_engine: Engine,
    *,
    clear: bool = False,
) -> dict[str, int]:
    """Re-import every table from ``archive`` into ``target_engine``.

    Rows are replaced by primary key (existing matching rows deleted,
    then the archived rows re-inserted), so restore is idempotent. With
    ``clear`` every table is emptied first (children before parents).
    Returns per-table archived row counts.
    """
    maker = sessionmaker(bind=target_engine, expire_on_commit=False)
    by_table = {m.__tablename__: m for m in table_order()}
    counts: dict[str, int] = {}

    with maker() as session:
        if clear:
            for model in reversed(by_table.values()):
                session.execute(delete(model))
            session.commit()

        # Re-order tables by FK dependency (parents first); tables the
        # archive does not contain are skipped.
        tables_in_archive = dict(iter_archive_db(archive))
        for table_name, model in by_table.items():
            rows = tables_in_archive.get(table_name)
            if rows is None:
                continue
            id_column = model.__table__.primary_key.columns[0]
            pk_attr = getattr(model, id_column.name)
            pks = [_pk_value(model, r[id_column.name]) for r in rows]
            if pks:
                session.execute(delete(model).where(pk_attr.in_(pks)))
            for payload in rows:
                session.add(_restore_row(model, payload))
            counts[table_name] = len(rows)
            session.commit()
    logger.info(
        "backup_restored",
        extra={"event": "backup_restored", "archive": str(archive), "tables": counts},
    )
    return counts


# ------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.ops.backup", description="Create / list / restore backups."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("create", help="create one backup archive (+ prune old)")
    sub.add_parser("list", help="list existing archives")
    p_restore = sub.add_parser("restore", help="restore an archive into a database")
    p_restore.add_argument("archive", help="path to a seo_backup_*.tar.gz")
    p_restore.add_argument("--clear", action="store_true",
                           help="empty every table before restoring")
    p_restore.add_argument("--target", default=None,
                           help="target SQLAlchemy URL (default: DATABASE_URL)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()

    if args.command == "create":
        path = create_backup(settings)
        print(f"[backup] created {path}")
        return 0

    if args.command == "list":
        files = list_backups(settings)
        if not files:
            print("[backup] no archives found")
        for path in files:
            print(f"[backup] {path}  ({path.stat().st_size} bytes)")
        return 0

    if args.command == "restore":
        from app.db.session import engine as default_engine

        target_url = args.target or settings.database_url
        target_engine = (
            default_engine if target_url == settings.database_url
            else create_engine(target_url)
        )
        counts = restore_archive(Path(args.archive), target_engine, clear=args.clear)
        total = sum(counts.values())
        print(f"[backup] restored {total} rows across {len(counts)} tables")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
