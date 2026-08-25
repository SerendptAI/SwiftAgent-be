from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.core.auth import verify_analytics_secret_key
from app.services.prompt_service import (
    list_templates, get_template, get_template_by_category,
    create_template, update_template, delete_template,
    get_version_history, rollback_to_version, get_version_diff,
    preview_prompt, create_or_update_company_override,
    delete_company_override, render_prompt_for_company,
    ensure_prompt_indexes, seed_default_prompts,
)
from app.models.prompt_models import (
    PromptTemplateCreate, PromptTemplateUpdate,
    PromptTemplateVersionHistory, PromptDiffResponse,
    PromptPreviewRequest, PromptPreviewResponse,
)

router = APIRouter(tags=["Prompt Studio"])


@router.on_event("startup")
async def init_prompt_studio():
    await ensure_prompt_indexes()
    await seed_default_prompts()


@router.get("/prompt-studio/templates")
async def list_prompt_templates(
    response: Response,
    category: Optional[str] = Query(None),
    company_id: Optional[str] = Query(None),
    include_global: bool = Query(True),
    auth: dict = Depends(verify_analytics_secret_key),
):
    response.headers["Cache-Control"] = "private, max-age=120"
    templates = await list_templates(category, company_id, include_global)
    return [t.model_dump() for t in templates]


@router.get("/prompt-studio/templates/{template_id}")
async def get_prompt_template(template_id: str, auth: dict = Depends(verify_analytics_secret_key)):
    template = await get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.post("/prompt-studio/templates")
async def create_prompt_template(
    data: PromptTemplateCreate,
    auth: dict = Depends(verify_analytics_secret_key),
):
    return await create_template(data, created_by=auth.get("company_id"), created_by_name="Admin")


@router.put("/prompt-studio/templates/{template_id}")
async def update_prompt_template(
    template_id: str, data: PromptTemplateUpdate,
    auth: dict = Depends(verify_analytics_secret_key),
):
    template = await update_template(template_id, data, updated_by=auth.get("company_id"), updated_by_name="Admin")
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.delete("/prompt-studio/templates/{template_id}")
async def delete_prompt_template(template_id: str, auth: dict = Depends(verify_analytics_secret_key)):
    success = await delete_template(template_id)
    if not success:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"success": True, "deleted": template_id}


@router.get("/prompt-studio/templates/{template_id}/history", response_model=PromptTemplateVersionHistory)
async def get_template_history(template_id: str, auth: dict = Depends(verify_analytics_secret_key)):
    return await get_version_history(template_id)


@router.post("/prompt-studio/templates/{template_id}/rollback")
async def rollback_template(
    template_id: str,
    target_version: int = Query(...),
    change_reason: Optional[str] = Query(None),
    auth: dict = Depends(verify_analytics_secret_key),
):
    template = await rollback_to_version(template_id, target_version, rolled_back_by=auth.get("company_id"), rolled_back_by_name="Admin", change_reason=change_reason)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.get("/prompt-studio/templates/{template_id}/diff", response_model=PromptDiffResponse)
async def diff_template_versions(
    template_id: str,
    version_a: int = Query(...),
    version_b: int = Query(...),
    auth: dict = Depends(verify_analytics_secret_key),
):
    diff = await get_version_diff(template_id, version_a, version_b)
    if not diff:
        raise HTTPException(status_code=404, detail="Version not found")
    return diff


@router.post("/prompt-studio/preview", response_model=PromptPreviewResponse)
async def preview_prompt_template(data: PromptPreviewRequest, auth: dict = Depends(verify_analytics_secret_key)):
    return await preview_prompt(data)


@router.get("/prompt-studio/categories")
async def list_categories(auth: dict = Depends(verify_analytics_secret_key)):
    return [
        {"id": "orchestrator", "name": "Orchestrator", "description": "Routing logic prompt"},
        {"id": "knowledge_agent", "name": "Knowledge Agent", "description": "KB search and response"},
        {"id": "navigation_agent", "name": "Navigation Agent", "description": "UI navigation guide"},
        {"id": "api_agent", "name": "API Agent", "description": "External API integration"},
        {"id": "scraper_agent", "name": "Scraper Agent", "description": "Website scraping"},
        {"id": "custom", "name": "Custom", "description": "Custom prompt"},
    ]


@router.put("/prompt-studio/company/{company_id}/override/{template_id}")
async def set_company_override(
    company_id: str, template_id: str,
    response: Response,
    template_text: str = Query(...),
    variables_override: Optional[str] = Query(None),
    change_reason: Optional[str] = Query(None),
    auth: dict = Depends(verify_analytics_secret_key),
):
    import json
    var_overrides = {}
    if variables_override:
        try:
            var_overrides = json.loads(variables_override)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
    return await create_or_update_company_override(company_id, template_id, template_text, var_overrides, created_by=auth.get("company_id"), change_reason=change_reason)


@router.delete("/prompt-studio/company/{company_id}/override/{template_id}")
async def delete_company_override(company_id: str, template_id: str, auth: dict = Depends(verify_analytics_secret_key)):
    success = await delete_company_override(company_id, template_id)
    if not success:
        raise HTTPException(status_code=404, detail="Override not found")
    return {"success": True, "deleted": f"{company_id}/{template_id}"}
