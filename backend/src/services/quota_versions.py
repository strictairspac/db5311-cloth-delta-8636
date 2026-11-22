"""Scheduled per-grade quota sets and the quota snapshot frozen at acceptance.

A quota version is one complete set of per-grade limits. Versions are immutable:
scheduling a new set adds a row instead of rewriting the current one, so a failed
switch leaves the previously effective version untouched. The version that applies
to an accounting date is the newest one whose effective date is on or before it.

When a run is accepted its whole quota set is copied to ``run_quota_pins`` in the
same transaction as the run row. Workers and retries read that snapshot, never the
live version table, so one quota slip can never mix limits from two versions.
"""

from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.models.tables import audit, quota_versions, run_quota_pins
from src.utils.time import utc_time

MAXIMUM = 1_000_000_000
SOURCE_VERSION = 'version'
SOURCE_LEGACY = 'legacy'
SOURCE_NONE = 'none'
_GRADE_KEYS = {'grade', 'capacity_g', 'capacity_pieces'}


def check_date(value, label='核算日'):
    """Return an ISO calendar date string, or raise a user-facing error."""
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f'{label}须为 YYYY-MM-DD 格式的日期')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{label}须为 YYYY-MM-DD 格式的日期') from None
    if parsed.isoformat() != value:
        raise ValueError(f'{label}须为 YYYY-MM-DD 格式的日期')
    return value


def validate_quotas(quotas):
    """Validate a complete quota set and return a plain copy of it."""
    if not isinstance(quotas, list):
        raise ValueError('分级仓位配额须为数组')
    if not quotas:
        raise ValueError('分级仓位配额至少须有一个等级')
    seen = set()
    normalized = []
    for index, row in enumerate(quotas, 1):
        location = f'分级仓位配额第 {index} 行'
        if not isinstance(row, dict) or set(row) != _GRADE_KEYS:
            raise ValueError(f'{location}须包含且仅包含 等级、重量上限（克）、件数上限')
        grade = row['grade']
        if (not isinstance(grade, str) or not 1 <= len(grade) <= 64
                or not grade.isprintable() or grade != grade.strip()):
            raise ValueError(f'{location}的等级须为 1 到 64 个可打印字符，首尾须为空白以外的字符')
        if grade in seen:
            raise ValueError(f'{location}的标识「{grade}」重复')
        seen.add(grade)
        for key, label in (('capacity_g', '重量上限（克）'), ('capacity_pieces', '件数上限')):
            value = row[key]
            if type(value) is not int or not 0 <= value <= MAXIMUM:
                raise ValueError(f'{location}的{label}须为 0 到 {MAXIMUM} 之间的整数')
        normalized.append({'grade': grade, 'capacity_g': row['capacity_g'],
                           'capacity_pieces': row['capacity_pieces']})
    return normalized


def recent(limit=100):
    with engine.connect() as conn:
        rows = conn.execute(select(quota_versions)
                            .order_by(quota_versions.c.version.desc()).limit(limit)).mappings()
        return [dict(row) for row in rows]


def resolve(conn, accounting_date):
    """Select one complete version effective on the accounting date."""
    return conn.execute(
        select(quota_versions)
        .where(quota_versions.c.effective_date <= accounting_date)
        .order_by(quota_versions.c.effective_date.desc(), quota_versions.c.version.desc())
        .limit(1)).mappings().first()


def create(effective_date, quotas, actor):
    """Schedule one complete quota set. Any failure rolls back and keeps the old version."""
    effective_date = check_date(effective_date, '生效日')
    quotas = validate_quotas(quotas)
    for _ in range(5):
        try:
            with engine.begin() as conn:
                # Serialize version allocation between concurrent planners on PostgreSQL.
                if conn.dialect.name == 'postgresql':
                    conn.execute(text('LOCK TABLE quota_versions IN EXCLUSIVE MODE'))
                current = conn.execute(select(func.max(quota_versions.c.version))).scalar_one_or_none()
                version = (current or 0) + 1
                identity = conn.execute(quota_versions.insert().values(
                    version=version, effective_date=effective_date, quotas=quotas,
                    author=actor, created_at=utc_time())).inserted_primary_key[0]
                conn.execute(audit.insert().values(actor=actor, action='quota_version',
                                                   reference=str(version), created_at=utc_time()))
                return dict(conn.execute(select(quota_versions)
                                         .where(quota_versions.c.id == identity)).mappings().one())
        except IntegrityError:
            # A concurrent planner took the same version number; retry the allocation.
            continue
    raise HTTPException(409, '配额版本切换失败，旧版本继续生效。')


def freeze(conn, payload):
    """Choose the quota set for a newly accepted run without writing it.

    A dated archive only ever uses a scheduled version: if none was in effect on
    its accounting date, no slip may be issued and inline quotas are never used as
    a substitute. An undated archive is an old one and keeps the quota set saved
    with it. The whole set is read as one row, so concurrent switches never mix
    two versions.
    """
    accounting_date = payload.get('accounting_date')
    quotas = payload.get('quotas')
    if accounting_date is not None:
        version = resolve(conn, accounting_date)
        if version is not None:
            return {'source': SOURCE_VERSION, 'version': version['version'],
                    'effective_date': version['effective_date'],
                    'accounting_date': accounting_date, 'quotas': version['quotas']}
        return {'source': SOURCE_NONE, 'version': None, 'effective_date': None,
                'accounting_date': accounting_date, 'quotas': []}
    if quotas:
        return {'source': SOURCE_LEGACY, 'version': None, 'effective_date': None,
                'accounting_date': None, 'quotas': quotas}
    return {'source': SOURCE_NONE, 'version': None, 'effective_date': None,
            'accounting_date': None, 'quotas': []}


def _values(run_id, pin):
    return {'run_id': run_id, 'source': pin['source'], 'version': pin['version'],
            'effective_date': pin['effective_date'], 'accounting_date': pin['accounting_date'],
            'quotas': pin['quotas'], 'created_at': utc_time()}


def store(conn, run_id, pin):
    """Persist a run's frozen quota set inside the caller's transaction.

    Idempotent: a second claim of the same run never overwrites the first snapshot.
    """
    values = _values(run_id, pin)
    if conn.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        statement = postgres_insert(run_quota_pins).values(**values).on_conflict_do_nothing(
            index_elements=[run_quota_pins.c.run_id])
        conn.execute(statement)
        return dict(conn.execute(select(run_quota_pins)
                                 .where(run_quota_pins.c.run_id == run_id)).mappings().one())
    if conn.dialect.name.startswith('mysql'):
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        statement = mysql_insert(run_quota_pins).values(**values).on_duplicate_key_update(
            run_id=run_quota_pins.c.run_id)
        conn.execute(statement)
        return dict(conn.execute(select(run_quota_pins)
                                 .where(run_quota_pins.c.run_id == run_id)).mappings().one())
    existing = load(conn, run_id)
    if existing is not None:
        return existing
    conn.execute(run_quota_pins.insert().values(**values))
    return pin


def load(conn, run_id):
    row = conn.execute(select(run_quota_pins)
                       .where(run_quota_pins.c.run_id == run_id)).mappings().first()
    return dict(row) if row is not None else None


def ensure(conn, run_id, payload):
    """Pin quotas for a run accepted before pinning existed; later retries reuse it."""
    existing = load(conn, run_id)
    if existing is not None:
        return existing
    return store(conn, run_id, freeze(conn, payload))


def label(pin):
    if pin and pin['source'] == SOURCE_VERSION:
        return f"v{pin['version']}"
    if pin and pin['source'] == SOURCE_LEGACY:
        return '随档配额'
    return '无适用版本'


def section(pin):
    source = {SOURCE_VERSION: '配额版本', SOURCE_LEGACY: '随档保存配额',
              SOURCE_NONE: '无适用版本'}.get(pin['source'], pin['source'])
    return {'title': '配额版本', 'rows': [{
        '配额来源': source, '配额版本': label(pin),
        '生效日': pin.get('effective_date') or '—',
        '核算日': pin.get('accounting_date') or '—'}]}


def annotate(run_rows):
    """Attach each run's frozen quota label so lists can show which version a slip used."""
    if not run_rows:
        return run_rows
    with engine.connect() as conn:
        ids = [row['id'] for row in run_rows]
        pins = {row['run_id']: dict(row) for row in conn.execute(
            select(run_quota_pins).where(run_quota_pins.c.run_id.in_(ids))).mappings()}
    for row in run_rows:
        pin = pins.get(row['id'])
        row['quota_version'] = label(pin) if pin else None
        row['quota_source'] = pin['source'] if pin else None
    return run_rows
