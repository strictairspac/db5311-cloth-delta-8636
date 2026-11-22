from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import CaseForm, LoginForm, QuotaVersionForm, ReviewForm
from src.services import domain, quota_versions, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/quota-versions')
def list_quota_versions(_=Depends(current_user)):
    return quota_versions.recent()


@router.post('/quota-versions', status_code=201)
def create_quota_version(form: QuotaVersionForm, actor=Depends(reviewer)):
    try:
        return quota_versions.create(form.effective_date, form.quotas, actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return quota_versions.annotate(workflow.recent(runs))


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return quota_versions.annotate([workflow.enqueue(case_id, actor)])[0]


@router.post('/runs/{run_id}/retry', status_code=202)
def retry_run(run_id: int, actor=Depends(operator)):
    return quota_versions.annotate([workflow.retry(run_id, actor)])[0]


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
