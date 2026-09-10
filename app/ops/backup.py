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
import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base

logger = logging.getLogger(__name__)

APP_MARKER = "seo-autogen"


class BackupError(Exception):
    """A backup/restore failure that must not leave a half-restore state."""


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


# ------------------------------------------------------------ validation
def _open_archive(archive: Path) -> tarfile.TarFile:
    try:
        return tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as error:
        raise BackupError(f"cannot open archive {archive}: {error}") from error


def validate_archive(archive: Path) -> dict[str, Any]:
    """Fully open + scan the archive BEFORE restoring anything (H12).

    Fails loudly (``BackupError``) on a corrupt / truncated / non-backup
    archive so a restore can never start and leave a half state. Returns
    ``{"tables": {name: row_count}, "artifact_files": [names]}`` so the
    caller can preview the restore.
    """
    try:
        with _open_archive(archive) as tar:
            members = tar.getmembers()
            manifest = None
            table_names: list[str] = []
            artifact_files: list[str] = []
            for member in members:
                if not member.isfile():
                    continue
                if member.name == "db/manifest.json":
                    fh = tar.extractfile(member)
                    if fh is None:
                        raise BackupError("archive manifest unreadable")
                    manifest = json.loads(fh.read().decode("utf-8"))
                elif member.name.startswith("db/") and member.name.endswith(".jsonl"):
                    table_names.append(member.name.removeprefix("db/").removesuffix(".jsonl"))
                elif member.name == "artifacts.tar":
                    # The inner tar must itself be readable — a truncated
                    # outer archive with a broken inner tar must fail here,
                    # not mid-restore. R-H07: EVERY member name is normalized
                    # and containment-checked here, before any write, so a
                    # traversal/absolute/symlink archive is rejected with zero
                    # filesystem and zero DB changes.
                    fh = tar.extractfile(member)
                    if fh is None:
                        raise BackupError("artifacts.tar unreadable")
                    inner = io.BytesIO(fh.read())
                    with tarfile.open(fileobj=inner, mode="r") as inner_tar:
                        for im in inner_tar.getmembers():
                            if im.issym() or im.islnk():
                                raise BackupError(
                                    "artifact archive contains a link member "
                                    f"{im.name!r} — refusing to restore"
                                )
                            if not im.isfile():
                                continue
                            _artifact_rel_parts(im.name)
                            artifact_files.append(im.name)
            if manifest is None:
                raise BackupError("archive is not a valid backup (missing db/manifest.json)")
            if manifest.get("app") != APP_MARKER:
                raise BackupError(f"archive app marker mismatch: {manifest.get('app')!r}")
            # Every declared table file must be present.
            declared = set(manifest.get("tables", {}).keys())
            if declared and declared != set(table_names):
                missing = declared - set(table_names)
                raise BackupError(f"archive declares tables missing from db/: {sorted(missing)}")
            # An archive that contains image rows must also ship the image
            # tree — otherwise restore would re-import dangling local_paths.
            if "images" in table_names:
                fh = tar.extractfile(tar.getmember("db/images.jsonl"))
                if fh is not None:
                    image_rows = sum(1 for line in io.TextIOWrapper(fh, encoding="utf-8") if line.strip())
                else:
                    image_rows = 0
                if image_rows and not artifact_files:
                    raise BackupError(
                        f"archive contains {image_rows} image rows but no artifact files — "
                        "refusing to restore dangling image paths"
                    )
    except BackupError:
        raise
    except (OSError, EOFError, tarfile.TarError, json.JSONDecodeError, ValueError) as error:
        raise BackupError(f"archive validation failed: {error}") from error
    return {"tables": {t: 0 for t in table_names}, "artifact_files": artifact_files}


def _artifact_rel_parts(arcname: str) -> tuple[str, ...]:
    """Normalize an inner-tar member name to a safe path under ``articles/``.

    R-H07: restore must never write outside the target ``articles`` tree.
    Rejects, before any write:

    * absolute paths (``/etc/passwd``, ``C:/x``, ``\\\\host\\share``);
    * traversal components (``articles/../escaped.txt``);
    * NUL bytes;
    * members not rooted at ``articles/``.

    Returns the relative parts *below* ``articles`` (empty for the
    ``articles`` root entry).
    """
    if not arcname or "\x00" in arcname:
        raise BackupError(f"invalid artifact member name: {arcname!r}")
    # tar uses POSIX separators; normalize backslashes too so a hand-made
    # archive cannot smuggle a Windows path past the check.
    normalized = arcname.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized.startswith("/"):
        raise BackupError(f"absolute artifact path rejected: {arcname!r}")
    parts = path.parts
    if not parts or parts[0] != "articles":
        raise BackupError(
            f"artifact member outside the articles tree rejected: {arcname!r}"
        )
    for part in parts[1:]:
        if part in ("", ".", ".."):
            raise BackupError(
                f"unsafe artifact path component {part!r} in {arcname!r}"
            )
        if ":" in part:
            raise BackupError(
                f"unsafe artifact path component {part!r} in {arcname!r}"
            )
    return tuple(parts[1:])


def _assert_within(path: Path, root: Path) -> None:
    """Containment check for a materialized path (R-H07, defense in depth)."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError as error:  # pragma: no cover - very unusual FS state
        raise BackupError(f"cannot resolve artifact path {path}: {error}") from error
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise BackupError(
            f"artifact path escapes the target directory: {path}"
        )


def _iter_artifact_files(archive: Path) -> list[tuple[str, bytes]]:
    """Read every safe file under the inner ``artifacts.tar`` (R-H07).

    Raises :class:`BackupError` on symlink/hardlink members, unsafe member
    names, or an unreadable inner tar — so a malicious archive is rejected
    before anything is written.
    """
    with _open_archive(archive) as tar:
        member = tar.getmember("artifacts.tar")
        fh = tar.extractfile(member)
        if fh is None:
            raise BackupError("artifacts.tar unreadable")
        buf = fh.read()
    out: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(buf), mode="r") as inner:
        for im in inner.getmembers():
            if im.issym() or im.islnk():
                raise BackupError(
                    f"artifact archive contains a link member {im.name!r} — "
                    "refusing to restore"
                )
            if not im.isfile():
                continue
            rel_parts = _artifact_rel_parts(im.name)
            if not rel_parts:
                raise BackupError(
                    f"artifact member has no file name: {im.name!r}"
                )
            member_fh = inner.extractfile(im)
            if member_fh is None:
                continue
            out.append(("/".join(rel_parts), member_fh.read()))
    return out


def _stage_artifacts(
    files: list[tuple[str, bytes]], staging: Path
) -> dict[str, Path]:
    """Write validated artifacts into ``staging`` (R-H07/R-M01).

    Everything lands under the staging directory first; the live tree is
    only touched after the DB rows and image list have been validated.
    """
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for rel, data in files:
        dest = staging.joinpath(*PurePosixPath(rel).parts)
        _assert_within(dest, staging)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written[rel] = dest
    return written


def _compensate_artifacts(
    promoted: list[Path], backed_up: list[tuple[Path, Path]]
) -> None:
    """Undo a partial publish: delete new files, restore the originals."""
    for target in promoted:
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort compensation
            logger.warning(
                "restore_compensation_unlink_failed",
                extra={"event": "restore_compensation_unlink_failed",
                       "path": str(target)},
            )
    for target, backup in backed_up:
        try:
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(target))
        except OSError:  # pragma: no cover - best effort compensation
            logger.error(
                "restore_compensation_move_failed",
                extra={"event": "restore_compensation_move_failed",
                       "path": str(target), "backup": str(backup)},
            )


def _promote_artifacts(
    staged: dict[str, Path], live_root: Path, rollback_dir: Path
) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Move staged files into the live tree, snapshotting overwritten files.

    R-M01: every live file that is about to be replaced is first moved into
    ``rollback_dir`` so a failure (here or later, e.g. the DB commit) can put
    the ORIGINAL tree back. Returns ``(promoted, backed_up)``.
    """
    promoted: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    if rollback_dir.exists():
        shutil.rmtree(rollback_dir)
    live_root.mkdir(parents=True, exist_ok=True)
    try:
        for rel in sorted(staged):
            staged_path = staged[rel]
            target = live_root.joinpath(*PurePosixPath(rel).parts)
            _assert_within(target, live_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = rollback_dir.joinpath(*PurePosixPath(rel).parts)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup))
                backed_up.append((target, backup))
            shutil.move(str(staged_path), str(target))
            promoted.append(target)
    except Exception as error:
        _compensate_artifacts(promoted, backed_up)
        raise BackupError(
            f"failed to publish restored artifacts: {error}"
        ) from error
    return promoted, backed_up


def _articles_rel(local_path: str) -> str | None:
    """The path below ``articles/`` for an archived ``local_path``."""
    parts = Path(local_path).parts
    if "articles" not in parts:
        return None
    return "/".join(parts[parts.index("articles") + 1:])


def _remap_local_path(local_path: str, rel_to_target: dict[str, str]) -> str:
    """Rewrite an archived ``images.local_path`` onto the target data dir.

    ``local_path`` is stored absolute (``{src}/articles/{job_id}/file``).
    On restore the file tree is written to a *different* root, so the path
    is re-anchored on the ``/articles/`` component and remapped to the
    target location (or left untouched when it is not an articles path).
    """
    parts = Path(local_path).parts
    if "articles" not in parts:
        return local_path
    rel = "/".join(parts[parts.index("articles") + 1:])
    return rel_to_target.get(rel, local_path)


def restore_archive(
    archive: Path,
    target_engine: Engine,
    *,
    clear: bool = False,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Re-import every table + the image artifacts from ``archive`` (H12).

    H12 guarantees (audit: *restore must recover the images, validate the
    archive before touching anything, and never leave a half-restore*):

    1. **Validate first** — :func:`validate_archive` fully opens the outer
       AND inner tar and checks the manifest before a single row is
       written, so a corrupt/truncated archive fails with
       :class:`BackupError` and the target DB stays untouched.
    2. **Extract + remap images** — the ``artifacts.tar`` tree is written
       into ``{data_dir}/articles`` via :func:`_extract_artifacts` (staged
       in a temp dir and moved into place), and every archived
       ``images.local_path`` is re-anchored onto the target data dir so the
       restored DB points at files that actually exist after the copy.
    3. **Path validation before commit** — every remapped image path must
       exist on disk *before* the DB transaction commits. A restore whose
       images cannot be satisfied fails with :class:`BackupError` and the
       DB is left untouched (no half-restore, no dangling ``local_path``).
    4. **Atomic DB restore** — every table's rows are staged in ONE
       transaction and committed once, so a mid-restore error rolls the
       whole DB section back (the old per-table commits could leave a
       half-restored database).

    With ``clear`` every table is emptied first (children before parents).
    Returns ``{"tables": {name: row_count}, "artifacts": [paths],
    "image_files": {local_path: sha256}, "missing_images": [local_path]}``.

    R-H07 / R-M01 ordering guarantees:

    1. archive + member-path validation happens first (zero writes on a
       malicious/invalid archive);
    2. artifacts are staged into ``{data_dir}/.restore_staging`` and every
       archived image row is checked against that staging tree — the LIVE
       ``articles`` tree is untouched so far;
    3. the live tree is then published with a rollback snapshot of every
       overwritten file;
    4. the DB transaction commits last; if it fails, the file publish is
       compensated, so DB and files both end up in their ORIGINAL state.
    """
    import hashlib

    # 1) Validate before touching the target at all (paths included).
    validate_archive(archive)

    data_path = Path(data_dir) if data_dir is not None else None
    staging = (data_path / ".restore_staging") if data_path else None
    rollback_dir = (data_path / ".restore_rollback") if data_path else None

    # 2) Stage the image tree (no live writes yet) and remap every archived
    #    local_path onto the FINAL live location.
    staged: dict[str, Path] = {}
    rel_to_target: dict[str, str] = {}
    try:
        if data_path is not None:
            staged = _stage_artifacts(_iter_artifact_files(archive), staging)
            live_root = data_path / "articles"
            rel_to_target = {
                rel: str(live_root.joinpath(*PurePosixPath(rel).parts))
                for rel in staged
            }

        maker = sessionmaker(bind=target_engine, expire_on_commit=False)
        by_table = {m.__tablename__: m for m in table_order()}
        tables_in_archive = {
            name: [_remap_row(name, r, rel_to_target) for r in rows]
            for name, rows in iter_archive_db(archive)
        }

        # 3) Validate every image row against the STAGING tree BEFORE any
        #    live write and BEFORE the DB commit (fail-fast, no half-restore).
        remapped_image_paths: list[str] = []
        missing_images: list[str] = []
        for r in tables_in_archive.get("images", []):
            archived_lp = r.get("local_path")
            if not archived_lp:
                continue
            remapped_image_paths.append(archived_lp)
            if data_path is None:
                if not Path(archived_lp).is_file():
                    missing_images.append(archived_lp)
                continue
            rel = _articles_rel(archived_lp)
            if rel is None or rel not in staged:
                missing_images.append(archived_lp)
        if missing_images:
            raise BackupError(
                "restore cannot satisfy image rows before touching the target "
                f"({len(missing_images)} missing, e.g. {missing_images[0]}) — "
                "the archive and the target data dir are out of sync"
            )

        # 4) Publish the staged tree with a rollback snapshot (R-M01).
        promoted: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        if data_path is not None and staged:
            promoted, backed_up = _promote_artifacts(
                staged, data_path / "articles", rollback_dir
            )

        # 5) Stage the whole DB section in one transaction (atomic). If it
        #    fails, compensate the file publish so DB + files stay original.
        counts: dict[str, int] = {}
        try:
            with maker() as session:
                if clear:
                    for model in reversed(by_table.values()):
                        session.execute(delete(model))
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
                session.commit()  # single commit — all-or-nothing DB restore
        except Exception as error:
            _compensate_artifacts(promoted, backed_up)
            raise BackupError(
                "database restore failed; the original artifacts were put "
                f"back ({error.__class__.__name__}: {error})"
            ) from error

        # Report the digest of each restored file (path-mapping + file-summary
        # check the audit demands).
        image_files: dict[str, str] = {
            lp: hashlib.sha256(Path(lp).read_bytes()).hexdigest()
            for lp in remapped_image_paths
        }
        logger.info(
            "backup_restored",
            extra={
                "event": "backup_restored",
                "archive": str(archive),
                "tables": counts,
                "artifacts": len(promoted),
                "images": len(image_files),
            },
        )
        return {
            "tables": counts,
            "artifacts": [str(p) for p in promoted],
            "image_files": image_files,
            "missing_images": missing_images,
        }
    finally:
        # R-M01: staging/rollback are scratch space; the live tree and the
        # DB are consistent at this point either way.
        for scratch in (staging, rollback_dir):
            if scratch is not None and scratch.exists():
                shutil.rmtree(scratch, ignore_errors=True)


def _remap_row(table_name: str, row: dict, rel_to_target: dict[str, str]) -> dict:
    """Apply the ``local_path`` remap to an archived row (images only)."""
    if table_name != "images" or not rel_to_target:
        return row
    lp = row.get("local_path")
    if lp:
        row = dict(row)
        row["local_path"] = _remap_local_path(lp, rel_to_target)
    return row


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
    p_restore.add_argument("--data-dir", default=None,
                           help="target data dir for the article images (default: DATA_DIR)")
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
        data_dir = Path(args.data_dir) if args.data_dir else Path(settings.data_dir)
        try:
            result = restore_archive(
                Path(args.archive), target_engine, clear=args.clear, data_dir=data_dir
            )
        except BackupError as error:
            print(f"[backup] restore failed: {error}")
            return 1
        counts = result["tables"]
        total = sum(counts.values())
        print(
            f"[backup] restored {total} rows across {len(counts)} tables, "
            f"{len(result['artifacts'])} artifact files, {len(result['image_files'])} images"
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
