from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.services.company_service import get_company
from app.models.form_models import (
    WebsiteFormCreate,
    OnlineFormCreate,
    FormUpdate,
    FormResponse,
    FormSubmissionResponse
)
from app.services.form_service import form_service

router = APIRouter()

async def get_authorized_company(company_id: str, current_user: dict):
    user_id = current_user["user_id"]
    company = await get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or access denied.")
    return company

@router.post("/{company_id}/website-forms", response_model=FormResponse)
async def create_website_form(
    company_id: str,
    form_data: WebsiteFormCreate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.create_website_form(company_id, form_data)

@router.post("/{company_id}/online-forms", response_model=FormResponse)
async def create_online_form(
    company_id: str,
    form_data: OnlineFormCreate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.create_online_form(company_id, form_data)

@router.get("/{company_id}", response_model=List[FormResponse])
async def list_forms(
    company_id: str,
    skip: int = 0,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_forms_for_company(company_id, skip, limit)

@router.get("/{company_id}/{form_id}", response_model=FormResponse)
async def get_form(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    form = await form_service.get_form_by_id(form_id)
    if not form or form.company_id != company_id:
        raise HTTPException(status_code=404, detail="Form not found")
    return form

@router.put("/{company_id}/{form_id}", response_model=FormResponse)
async def update_form(
    company_id: str,
    form_id: str,
    update_data: FormUpdate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    form = await form_service.update_form(form_id, company_id, update_data)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    return form

@router.delete("/{company_id}/{form_id}")
async def delete_form(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    success = await form_service.delete_form(form_id, company_id)
    if not success:
        raise HTTPException(status_code=404, detail="Form not found")
    return {"status": "deleted"}

@router.get("/{company_id}/submissions/all", response_model=List[FormSubmissionResponse])
async def list_all_submissions(
    company_id: str,
    is_read: Optional[bool] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_all_submissions_for_company(company_id, is_read, skip, limit)

@router.get("/{company_id}/{form_id}/submissions", response_model=List[FormSubmissionResponse])
async def list_form_submissions(
    company_id: str,
    form_id: str,
    is_read: Optional[bool] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_submissions_for_form(form_id, company_id, is_read, skip, limit)

@router.put("/{company_id}/submissions/{submission_id}/read", response_model=FormSubmissionResponse)
async def mark_submission_read(
    company_id: str,
    submission_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    submission = await form_service.mark_submission_as_read(submission_id, company_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission

@router.get("/{company_id}/submissions/{submission_id}", response_model=FormSubmissionResponse)
async def get_submission(
    company_id: str,
    submission_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    submission = await form_service.get_submission_by_id(submission_id, company_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission

@router.delete("/{company_id}/submissions/{submission_id}", status_code=204)
async def delete_submission(
    company_id: str,
    submission_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    success = await form_service.delete_submission(submission_id, company_id)
    if not success:
        raise HTTPException(status_code=404, detail="Submission not found")
