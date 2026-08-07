import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.security import decode_access_token
from app.core.database import db
from app.services.company_service import get_company

logger = logging.getLogger(__name__)
from app.models.form_models import (
    WebsiteFormCreate,
    OnlineFormCreate,
    FormUpdate,
    FormResponse,
    WebsiteFormCreateResponse,
    OnlineFormCreateResponse,
    FormSubmissionResponse,
    FormKeysResponse,
    WebsiteOverview,
    WebsiteDeleteInfo,
    PageDeleteInfo,
    FormDeleteInfo,
    EntryDeleteInfo,
    WebsiteLabelUpdate,
    PageLabelUpdate,
    LabelsResponse,
    BulkDeleteRequest,
    FormRenameRequest,
    FormReplyRequest,
)
from app.services.form_service import form_service
from app.services import form_key_service
from app.services import company_email_service

class RegeneratedKeys(BaseModel):
    api_key: str
    public_key: str
    snippet: str

router = APIRouter()

async def get_authorized_company(company_id: str, current_user: dict):
    user_id = current_user["user_id"]
    company = await get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or access denied.")
    return company


# ── CREATE FORMS ──

@router.post("/{company_id}/website-forms", response_model=WebsiteFormCreateResponse)
async def create_website_form(
    company_id: str,
    form_data: WebsiteFormCreate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.create_website_form(company_id, form_data)


@router.post("/{company_id}/online-forms", response_model=OnlineFormCreateResponse)
async def create_online_form(
    company_id: str,
    form_data: OnlineFormCreate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.create_online_form(company_id, form_data)


# ── 4-LEVEL DELETE HIERARCHY (Must be before /{form_id} to avoid collision) ──

@router.get("/{company_id}/delete/websites", response_model=List[WebsiteDeleteInfo])
async def list_websites_for_delete(
    company_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_websites_for_company(company_id)


@router.get("/{company_id}/delete/{form_id}/pages", response_model=List[PageDeleteInfo])
async def list_pages_for_delete(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_pages_for_form(form_id, company_id)


@router.get("/{company_id}/delete/{form_id}/pages/{page_path:path}/forms", response_model=List[FormDeleteInfo])
async def list_forms_for_delete(
    company_id: str,
    form_id: str,
    page_path: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_forms_for_page(form_id, company_id, page_path)


@router.get("/{company_id}/delete/{form_id}/pages/{page_path:path}/forms/{form_identifier}/entries", response_model=List[EntryDeleteInfo])
async def list_entries_for_delete(
    company_id: str,
    form_id: str,
    page_path: str,
    form_identifier: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_entries_for_form_group(form_id, company_id, page_path, form_identifier, skip, limit)


@router.delete("/{company_id}/websites")
async def delete_website(
    company_id: str,
    website: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    deleted = await form_service.delete_website(company_id, website)
    return {"status": "success", "deleted_forms": deleted}


@router.delete("/{company_id}/{form_id}/pages/{page_path:path}/forms/{form_identifier}")
async def delete_form_group(
    company_id: str,
    form_id: str,
    page_path: str,
    form_identifier: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    deleted_count = await form_service.delete_by_form_identifier(form_id, company_id, page_path, form_identifier)
    return {"status": "success", "deleted_count": deleted_count}


@router.delete("/{company_id}/{form_id}/pages/{page_path:path}")
async def delete_page(
    company_id: str,
    form_id: str,
    page_path: str,
    current_user: dict = Depends(get_current_user)
):
    # This overlaps with legacy delete_page, but uses form_id instead of website url
    await get_authorized_company(company_id, current_user)
    deleted_count = await form_service.delete_by_page(form_id, company_id, page_path)
    return {"status": "success", "deleted_count": deleted_count}


@router.delete("/{company_id}/submissions/bulk")
async def bulk_delete_submissions(
    company_id: str,
    request: BulkDeleteRequest,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    deleted_count = await form_service.delete_submissions_bulk(request.submission_ids, company_id)
    return {"status": "success", "deleted_count": deleted_count}


# ── SUBMISSIONS LISTING & OVERVIEW ──

@router.get("/{company_id}/{form_id}/overview", response_model=WebsiteOverview)
async def get_form_overview(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    overview = await form_service.get_website_overview(form_id, company_id)
    if not overview:
        raise HTTPException(status_code=404, detail="Form not found")
    return overview


@router.get("/{company_id}/{form_id}/pages/{page_path:path}/forms/{form_identifier}/submissions", response_model=List[FormSubmissionResponse])
async def list_form_group_submissions(
    company_id: str,
    form_id: str,
    page_path: str,
    form_identifier: str,
    is_read: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_submissions_by_form_group(form_id, company_id, page_path, form_identifier, is_read, skip, limit)


@router.get("/{company_id}/{form_id}/pages/{page_path:path}/submissions", response_model=List[FormSubmissionResponse])
async def list_page_submissions(
    company_id: str,
    form_id: str,
    page_path: str,
    is_read: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_submissions_by_page(form_id, company_id, page_path, is_read, skip, limit)


# ── KEYS & SECURITY INFO ──

@router.get("/{company_id}/{form_id}/keys", response_model=FormKeysResponse)
async def get_form_keys(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    keys = await form_service.get_form_keys(form_id, company_id)
    if not keys:
        raise HTTPException(status_code=404, detail="Keys not found for this form")
    return keys


@router.post("/{company_id}/{form_id}/keys/regenerate", response_model=RegeneratedKeys)
async def regenerate_form_keys(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    keys = await form_service.regenerate_form_keys(form_id, company_id)
    if not keys:
        raise HTTPException(status_code=404, detail="Form not found")
    snippet = form_key_service.generate_snippet(keys["public_key"])
    return {
        "api_key": keys["api_key"],
        "public_key": keys["public_key"],
        "snippet": snippet
    }


# ── RENAME FORMS/PAGES/WEBSITES ──

@router.put("/{company_id}/{form_id}/pages/{page_path:path}/forms/{form_identifier}/rename")
async def rename_form_group(
    company_id: str,
    form_id: str,
    page_path: str,
    form_identifier: str,
    request: FormRenameRequest,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    success = await form_service.rename_form_group(form_id, company_id, page_path, form_identifier, request.new_name)
    if not success:
        raise HTTPException(status_code=404, detail="Form not found")
    return {"status": "success"}


@router.put("/{company_id}/websites/label")
async def update_website_label(
    company_id: str,
    payload: WebsiteLabelUpdate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    updated = await form_service.rename_website_label(company_id, payload.old_website, payload.new_website)
    return {"status": "success", "updated_forms": updated}


@router.put("/{company_id}/pages/label")
async def update_page_label(
    company_id: str,
    payload: PageLabelUpdate,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    updated = await form_service.rename_page_label(company_id, payload.website, payload.old_page, payload.new_page)
    return {"status": "success", "updated_forms": updated}


# ── LEGACY CRUD OPERATIONS (kept for backward compatibility) ──

@router.get("/{company_id}", response_model=List[FormResponse])
async def list_forms(
    company_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_forms_for_company(company_id, skip, limit)


@router.get("/{company_id}/submissions/all", response_model=List[FormSubmissionResponse])
async def list_all_submissions(
    company_id: str,
    is_read: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_all_submissions_for_company(company_id, is_read, skip, limit)


@router.get("/{company_id}/labels", response_model=LabelsResponse)
async def get_labels(
    company_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    return await form_service.get_labels_for_company(company_id)


@router.delete("/{company_id}/pages")
async def delete_page_legacy(
    company_id: str,
    website: str,
    page: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    deleted = await form_service.delete_page(company_id, website, page)
    return {"status": "success", "deleted_forms": deleted}


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


@router.post("/{company_id}/{form_id}/pause", response_model=FormResponse)
async def pause_form_endpoint(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    form = await form_service.pause_form(form_id, company_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    return form


@router.post("/{company_id}/{form_id}/resume", response_model=FormResponse)
async def resume_form_endpoint(
    company_id: str,
    form_id: str,
    current_user: dict = Depends(get_current_user)
):
    await get_authorized_company(company_id, current_user)
    form = await form_service.resume_form(form_id, company_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    return form


@router.get("/{company_id}/{form_id}/submissions", response_model=List[FormSubmissionResponse])
async def list_form_submissions(
    company_id: str,
    form_id: str,
    is_read: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
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


@router.post("/{company_id}/submissions/{submission_id}/reply", response_model=FormSubmissionResponse)
async def reply_to_submission(
    company_id: str,
    submission_id: str,
    request: FormReplyRequest,
    current_user: dict = Depends(get_current_user)
):
    company = await get_authorized_company(company_id, current_user)
    
    # Send the email
    try:
        await company_email_service.send_form_reply(
            company_id=company_id,
            submission_id=submission_id,
            reply_text=request.reply_text,
            subject=request.subject,
            agent_name=current_user.get("full_name") or current_user.get("email")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to send form reply email for submission {submission_id}: {e}")
        raise HTTPException(status_code=500, detail="An internal error occurred while trying to send the email.")

    # Fetch and return the updated submission
    submission = await form_service.get_submission_by_id(submission_id, company_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found after reply")
    return submission


# ── REALTIME WEBSOCKET ENDPOINTS ──


async def _verify_ws_auth(websocket: WebSocket, company_id: str, token: Optional[str] = None):
    """Authenticate WebSocket connection via JWT query token or Authorization header."""
    try:
        if not token:
            auth_header = websocket.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
            else:
                token = websocket.query_params.get("token")

        if not token:
            await websocket.send_json({"type": "error", "message": "Missing authentication token (?token=... or Authorization: Bearer ...)"})
            await websocket.close(code=1008)
            return None

        payload = decode_access_token(token)
        if not payload or not payload.get("sub"):
            await websocket.send_json({"type": "error", "message": "Invalid or missing token"})
            await websocket.close(code=1008)
            return None
        user_id = payload.get("sub")
        company = await get_company(company_id, user_id)
        if not company:
            await websocket.send_json({"type": "error", "message": "Company not found or unauthorized"})
            await websocket.close(code=1008)
            return None
        return company
    except Exception as e:
        logger.error(f"WebSocket auth failed: {e}")
        await websocket.close(code=1008)
        return None


@router.websocket("/{company_id}/ws")
async def forms_list_websocket(
    websocket: WebSocket,
    company_id: str,
    token: Optional[str] = Query(None)
):
    """Real-time WebSocket for the list of forms for a company.
    Pushes the updated form list whenever forms change.
    Requires a valid JWT token passed as a query parameter (?token=...) or Authorization header.
    """
    await websocket.accept()
    if not await _verify_ws_auth(websocket, company_id, token):
        return

    try:
        forms = await form_service.get_forms_for_company(company_id, 0, 50)
        await websocket.send_json({
            "items": jsonable_encoder(forms),
            "total": len(forms)
        })
    except Exception as e:
        logger.error(f"Error fetching initial forms for WS: {e}")
        await websocket.close()
        return

    try:
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
        async with db.forms.watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                full_doc = change.get("fullDocument")
                if full_doc and full_doc.get("company_id") != company_id:
                    continue

                forms = await form_service.get_forms_for_company(company_id, 0, 50)
                await websocket.send_json({
                    "items": jsonable_encoder(forms),
                    "total": len(forms)
                })
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for company {company_id} forms list")
    except Exception as e:
        logger.error(f"WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/{company_id}/submissions/ws")
async def all_submissions_websocket(
    websocket: WebSocket,
    company_id: str,
    token: Optional[str] = Query(None)
):
    """Real-time WebSocket for all form submissions of a company.
    Pushes the initial list of submissions upon connection, and whenever any submission changes.
    Requires a valid JWT token passed as a query parameter (?token=...) or Authorization header.
    """
    await websocket.accept()
    if not await _verify_ws_auth(websocket, company_id, token):
        return

    try:
        submissions = await form_service.get_all_submissions_for_company(company_id, None, 0, 50)
        total = await form_service.count_all_submissions_for_company(company_id)
        await websocket.send_json({
            "items": jsonable_encoder(submissions),
            "total": total,
            "limit": 50,
            "skip": 0,
            "has_next": len(submissions) < total
        })
    except Exception as e:
        logger.error(f"Error fetching initial submissions for WS: {e}")
        await websocket.close()
        return

    try:
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
        async with db.form_submissions.watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                full_doc = change.get("fullDocument")
                if full_doc and full_doc.get("company_id") != company_id:
                    continue

                submissions = await form_service.get_all_submissions_for_company(company_id, None, 0, 50)
                total = await form_service.count_all_submissions_for_company(company_id)
                await websocket.send_json({
                    "items": jsonable_encoder(submissions),
                    "total": total,
                    "limit": 50,
                    "skip": 0,
                    "has_next": len(submissions) < total
                })
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for company {company_id} submissions")
    except Exception as e:
        logger.error(f"WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/{company_id}/{form_id}/submissions/ws")
async def form_submissions_websocket(
    websocket: WebSocket,
    company_id: str,
    form_id: str,
    token: Optional[str] = Query(None)
):
    """Real-time WebSocket for submissions of a specific form.
    Pushes the initial list of submissions upon connection, and whenever any submission for this form changes.
    Requires a valid JWT token passed as a query parameter (?token=...) or Authorization header.
    """
    await websocket.accept()
    if not await _verify_ws_auth(websocket, company_id, token):
        return

    try:
        submissions = await form_service.get_submissions_for_form(form_id, company_id, None, 0, 50)
        total = await form_service.count_submissions_for_form(form_id, company_id)
        await websocket.send_json({
            "items": jsonable_encoder(submissions),
            "total": total,
            "limit": 50,
            "skip": 0,
            "has_next": len(submissions) < total
        })
    except Exception as e:
        logger.error(f"Error fetching initial form submissions for WS: {e}")
        await websocket.close()
        return

    try:
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
        async with db.form_submissions.watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                full_doc = change.get("fullDocument")
                if full_doc and (full_doc.get("company_id") != company_id or full_doc.get("form_id") != form_id):
                    continue

                submissions = await form_service.get_submissions_for_form(form_id, company_id, None, 0, 50)
                total = await form_service.count_submissions_for_form(form_id, company_id)
                await websocket.send_json({
                    "items": jsonable_encoder(submissions),
                    "total": total,
                    "limit": 50,
                    "skip": 0,
                    "has_next": len(submissions) < total
                })
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for form {form_id} submissions")
    except Exception as e:
        logger.error(f"WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/{company_id}/{form_id}/overview/ws")
async def form_overview_websocket(
    websocket: WebSocket,
    company_id: str,
    form_id: str,
    token: Optional[str] = Query(None)
):
    """Real-time WebSocket for a website form's overview (pages, forms, submission counts).
    Pushes the initial overview upon connection, and whenever any submission for this form changes.
    Requires a valid JWT token passed as a query parameter (?token=...) or Authorization header.
    """
    await websocket.accept()
    if not await _verify_ws_auth(websocket, company_id, token):
        return

    try:
        overview = await form_service.get_website_overview(form_id, company_id)
        if overview:
            await websocket.send_json(jsonable_encoder(overview))
    except Exception as e:
        logger.error(f"Error fetching initial form overview for WS: {e}")
        await websocket.close()
        return

    try:
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
        async with db.form_submissions.watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                full_doc = change.get("fullDocument")
                if full_doc and (full_doc.get("company_id") != company_id or full_doc.get("form_id") != form_id):
                    continue

                overview = await form_service.get_website_overview(form_id, company_id)
                if overview:
                    await websocket.send_json(jsonable_encoder(overview))
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for form {form_id} overview")
    except Exception as e:
        logger.error(f"WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass
