"""
Audit trail module — automatic, hash-chained logging of all DB changes.

Provides:
- SQLAlchemy session event listener (captures CREATE / UPDATE / DELETE)
- Document archival helper (move files instead of deleting)
- Hash chain integrity verification
- Source detection (web / api / ai_chat / system)

GoBD compliance notes:
- Every mutation is recorded with before/after JSON snapshots
- Entries are chained via SHA-256 hashes — tampering breaks the chain
- Documents are never deleted, only archived under instance/uploads/archive/
"""

import hashlib
import json
import os
import shutil
from datetime import datetime

from flask import has_request_context, request
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm.attributes import get_history
from sqlalchemy.orm import object_session


# ── Models that should be audited ──────────────────────────────────────────
# ChatHistory is excluded (ephemeral, not bookkeeping-relevant).
AUDITED_MODELS = set()  # populated by init_audit()

# Models to skip (not relevant for bookkeeping audit)
SKIP_MODELS = {'ChatHistory', 'AuditLog'}

# Columns to always skip in snapshots (sensitive / noisy)
SKIP_COLUMNS = {'password_hash'}


# ── Serialisation helper ──────────────────────────────────────────────────

def _snapshot(obj):
    """Return a JSON-serialisable dict of all column values."""
    data = {}
    for col in obj.__table__.columns:
        if col.name in SKIP_COLUMNS:
            continue
        val = getattr(obj, col.name, None)
        if isinstance(val, (datetime,)):
            val = val.isoformat()
        elif isinstance(val, (type(None), int, float, bool, str)):
            pass  # JSON-native
        else:
            val = str(val)
        data[col.name] = val
    return data


def _diff(old, new):
    """Return only changed keys (for UPDATE actions)."""
    changed_old, changed_new = {}, {}
    for key in set(old) | set(new):
        ov, nv = old.get(key), new.get(key)
        if ov != nv:
            changed_old[key] = ov
            changed_new[key] = nv
    return changed_old, changed_new


# ── Hash chain ─────────────────────────────────────────────────────────────

def _compute_hash(previous_hash, timestamp_iso, action, entity_type, entity_id,
                  old_json, new_json):
    """SHA-256 over deterministic concatenation of entry fields."""
    payload = '|'.join([
        previous_hash,
        timestamp_iso,
        action,
        entity_type,
        str(entity_id or ''),
        old_json or '',
        new_json or '',
    ])
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _get_previous_hash(db):
    """Fetch the hash of the most recent audit entry."""
    from models import AuditLog
    last = db.session.query(AuditLog.entry_hash) \
        .order_by(AuditLog.id.desc()).first()
    return last[0] if last else '0' * 64


# ── Request context helpers ────────────────────────────────────────────────

def _detect_source():
    """Guess whether the current request comes from web, api, or ai_chat."""
    if not has_request_context():
        return 'system'
    path = request.path
    if path.startswith('/api/'):
        return 'api'
    if path.startswith('/admin/ai-chat') or path.startswith('/admin/ai_chat'):
        return 'ai_chat'
    return 'web'


def _current_user_info():
    """Return (user_id, username) or (None, 'system')."""
    if has_request_context():
        try:
            if current_user and current_user.is_authenticated:
                return current_user.id, current_user.username
        except Exception:
            pass
    return None, 'system'


def _current_ip():
    if has_request_context():
        return request.remote_addr
    return None


# ── Core: write an audit entry ─────────────────────────────────────────────

def _write_audit(db_session, action, entity_type, entity_id,
                 old_values=None, new_values=None, archived_files=None):
    """Insert one AuditLog row with hash chain."""
    from models import AuditLog

    user_id, username = _current_user_info()
    now = datetime.utcnow()
    ts_iso = now.isoformat()
    old_json = json.dumps(old_values, ensure_ascii=False, sort_keys=True) if old_values else None
    new_json = json.dumps(new_values, ensure_ascii=False, sort_keys=True) if new_values else None
    archived_json = json.dumps(archived_files, ensure_ascii=False) if archived_files else None

    prev_hash = _get_previous_hash_from_session(db_session)

    entry_hash = _compute_hash(prev_hash, ts_iso, action, entity_type,
                               entity_id, old_json, new_json)

    entry = AuditLog(
        timestamp=now,
        user_id=user_id,
        username=username,
        ip_address=_current_ip(),
        source=_detect_source(),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=old_json,
        new_values=new_json,
        archived_files=archived_json,
        previous_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db_session.add(entry)


def _get_previous_hash_from_session(db_session):
    """Get previous hash, considering unflushed audit entries in the session."""
    from models import AuditLog
    # Check for unflushed AuditLog objects first
    pending = [obj for obj in db_session.new if isinstance(obj, AuditLog)]
    if pending:
        # Return hash of the latest pending entry
        return pending[-1].entry_hash
    last = db_session.query(AuditLog.entry_hash) \
        .order_by(AuditLog.id.desc()).first()
    return last[0] if last else '0' * 64


# ── SQLAlchemy session event listener ──────────────────────────────────────

# Temporary storage for objects captured at before_flush time
_pending_creates = []
_pending_updates = []
_pending_deletes = []


def _on_before_flush(session, flush_context, instances):
    """Intercept all pending changes — capture snapshots before flush."""
    from models import AuditLog
    global _pending_creates, _pending_updates, _pending_deletes
    _pending_creates = []
    _pending_updates = []
    _pending_deletes = []

    # NEW objects → CREATE (capture the object, log after flush to get ID)
    for obj in list(session.new):
        cls_name = type(obj).__name__
        if cls_name in SKIP_MODELS or type(obj) not in AUDITED_MODELS:
            continue
        _pending_creates.append(obj)

    # DIRTY objects → UPDATE
    for obj in list(session.dirty):
        cls_name = type(obj).__name__
        if cls_name in SKIP_MODELS or type(obj) not in AUDITED_MODELS:
            continue
        if not session.is_modified(obj, include_collections=False):
            continue
        # Build old snapshot from attribute history (must capture before flush)
        old_snap = {}
        new_snap = _snapshot(obj)
        has_changes = False
        for col in obj.__table__.columns:
            if col.name in SKIP_COLUMNS:
                continue
            history = get_history(obj, col.name)
            if history.has_changes():
                has_changes = True
                # Get previous value from history.deleted (committed value before change)
                if history.deleted:
                    val = history.deleted[0]
                elif history.unchanged:
                    val = history.unchanged[0]
                else:
                    val = None
                if isinstance(val, datetime):
                    val = val.isoformat()
                elif not isinstance(val, (type(None), int, float, bool, str)):
                    val = str(val)
                old_snap[col.name] = val
            else:
                old_snap[col.name] = new_snap.get(col.name)

        if has_changes:
            diff_old, diff_new = _diff(old_snap, new_snap)
            if diff_old or diff_new:
                _pending_updates.append((cls_name, new_snap.get('id'), diff_old, diff_new))

    # DELETED objects → DELETE (capture snapshot before flush deletes them)
    for obj in list(session.deleted):
        cls_name = type(obj).__name__
        if cls_name in SKIP_MODELS or type(obj) not in AUDITED_MODELS:
            continue
        snap = _snapshot(obj)
        _pending_deletes.append((cls_name, snap.get('id'), snap))


def _on_after_flush(session, flush_context):
    """Write audit entries after flush — IDs are now available for CREATEs."""
    global _pending_creates, _pending_updates, _pending_deletes

    # Process CREATEs — now obj.id is populated
    for obj in _pending_creates:
        cls_name = type(obj).__name__
        snap = _snapshot(obj)
        _write_audit(session, 'CREATE', cls_name, snap.get('id'), None, snap)

    # Process UPDATEs
    for cls_name, entity_id, diff_old, diff_new in _pending_updates:
        _write_audit(session, 'UPDATE', cls_name, entity_id, diff_old, diff_new)

    # Process DELETEs
    for cls_name, entity_id, snap in _pending_deletes:
        _write_audit(session, 'DELETE', cls_name, entity_id, snap, None)

    _pending_creates = []
    _pending_updates = []
    _pending_deletes = []


# ── Document archival ──────────────────────────────────────────────────────

def archive_file(upload_folder, filename):
    """
    Move a file from uploads/ to uploads/archive/ instead of deleting it.
    Returns the archive filename (with timestamp prefix) or None.
    """
    src = os.path.join(upload_folder, filename)
    if not os.path.exists(src):
        return None

    archive_dir = os.path.join(upload_folder, 'archive')
    os.makedirs(archive_dir, exist_ok=True)

    # Prefix with timestamp to avoid collisions
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    archived_name = f"{ts}_{filename}"
    dst = os.path.join(archive_dir, archived_name)
    shutil.move(src, dst)
    return archived_name


# ── Integrity verification ─────────────────────────────────────────────────

def verify_integrity(db):
    """
    Walk the entire audit_log table and verify the hash chain.

    Returns:
        (is_valid: bool, total: int, first_broken_id: int | None, message: str)
    """
    from models import AuditLog

    entries = AuditLog.query.order_by(AuditLog.id.asc()).all()
    if not entries:
        return True, 0, None, 'Keine Einträge vorhanden.'

    prev_hash = '0' * 64
    for entry in entries:
        # Check chain link
        if entry.previous_hash != prev_hash:
            return (False, len(entries), entry.id,
                    f'Kettenbruch bei Eintrag #{entry.id}: '
                    f'previous_hash stimmt nicht überein.')

        expected = _compute_hash(
            entry.previous_hash,
            entry.timestamp.isoformat(),
            entry.action,
            entry.entity_type,
            entry.entity_id,
            entry.old_values,
            entry.new_values,
        )
        if entry.entry_hash != expected:
            return (False, len(entries), entry.id,
                    f'Hash-Fehler bei Eintrag #{entry.id}: '
                    f'Daten wurden möglicherweise manipuliert.')

        prev_hash = entry.entry_hash

    return True, len(entries), None, f'Alle {len(entries)} Einträge sind integer.'


# ── Initialisation ─────────────────────────────────────────────────────────

def log_action(action: str, entity_type: str, entity_id,
               old_values=None, new_values=None, archived_files=None):
    """Public helper: write an explicit audit entry (e.g. for PDF generation).

    Unlike the automatic SQLAlchemy listener, this lets callers record
    custom events that are not simple CRUD on a model row.
    """
    from models import db as _db
    _write_audit(
        _db.session, action, entity_type, entity_id,
        old_values, new_values, archived_files,
    )


def init_audit(app, db):
    """
    Register the SQLAlchemy event listener.  Call this once after db.init_app().
    """
    from models import (
        User, SiteSettings, Account, Category, Transaction,
        Asset, DepreciationCategory, Document, AuditLog,
        Customer, Quote, QuoteItem, Invoice, InvoiceItem,
    )

    global AUDITED_MODELS
    AUDITED_MODELS = {
        User, SiteSettings, Account, Category, Transaction,
        Asset, DepreciationCategory, Document,
        Customer, Quote, QuoteItem, Invoice, InvoiceItem,
    }

    event.listen(db.session.__class__, 'before_flush', _on_before_flush)
    event.listen(db.session.__class__, 'after_flush', _on_after_flush)
    app.logger.info('Audit trail initialised (hash-chained, %d models tracked).',
                    len(AUDITED_MODELS))
