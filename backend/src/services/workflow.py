import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import domain, quota_versions
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        payload = found(conn, cases, case_id)['payload']
        # Freeze the whole quota set at acceptance in the same transaction as the
        # run row. Later version switches, worker retries and re-claims all reuse
        # this snapshot, so one slip can never mix limits from two versions.
        pin = quota_versions.freeze(conn, payload)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        quota_versions.store(conn, run_id, pin)
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        payload = found(conn, cases, run.case_id)['payload']
        # A retry or a run accepted before pinning existed gets one deterministic
        # snapshot now, which every later retry reuses.
        pin = quota_versions.ensure(conn, run.id, payload)
        return run.id, token, payload, pin


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def retry(run_id, actor):
    """Re-queue a failed run without re-selecting its quota version.

    The frozen snapshot accepted with the original run is kept untouched, so a
    retry never switches to a version that became effective in the meantime.
    """
    with engine.begin() as conn:
        run = found(conn, runs, run_id)
        if run['status'] != Status.FAILED:
            raise HTTPException(409, '只有执行失败的记录可以重试')
        payload = found(conn, cases, run['case_id'])['payload']
        quota_versions.ensure(conn, run_id, payload)
        conn.execute(runs.update().where(runs.c.id == run_id).values(
            status=Status.QUEUED, started_at=None, finished_at=None, error=None,
            attempt_token=None))
        log(conn, actor, 'retry', run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        pass
    with engine.connect() as conn:
        return dict(conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one())


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
