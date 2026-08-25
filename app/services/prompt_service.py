"""
Prompt Template Service — Agent Prompt Studio
Database-stored prompt templates with variable interpolation, per-company overrides,
version history, and rollback.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.database import db
from app.models.prompt_models import (
    PromptTemplate,
    PromptTemplateCreate,
    PromptTemplateUpdate,
    PromptVariable,
    PromptTemplateVersion,
    PromptTemplateVersionHistory,
    PromptDiffResponse,
    PromptPreviewRequest,
    PromptPreviewResponse,
    RenderedPrompt,
    CompanyPromptOverride,
)

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


_SEED_TEMPLATES = {
    "orchestrator_v1": {
        "name": "Orchestrator Routing Prompt",
        "category": "orchestrator",
        "is_default": True,
        "is_global": True,
        "template_text": """Your job is to analyze the conversation and route the user's request to the correct specialized expert.

ROUTING RULES:
- Read the user's request carefully.
- If it requires a specific expert, call the corresponding transfer tool IMMEDIATELY.
- DO NOT answer the question yourself if an expert is needed.
- CRITICAL: For questions about information that can change (like pricing, plans, "about us", or direct questions about the company), default to routing to the scraper agent FIRST to check the website, since this information is updated frequently there.
- CRITICAL: If the user explicitly asks to speak to a human, create a ticket, or contact support, you MUST call the `escalate_to_human` tool IMMEDIATELY.
- If the user says a simple greeting (e.g. "hi", "hello") or something that requires no tools, respond directly and conversationally.

OUTPUT FORMAT — ABSOLUTE RULE (VIOLATIONS WILL BREAK THE CUSTOMER EXPERIENCE):
- When routing: Your response MUST contain ONLY the tool call. Absolutely ZERO text.
- When chatting: Respond with short, conversational text. No tool calls.
- NEVER mix text and tool calls in the same response.""",
        "variables": [],
        "tags": ["routing", "v1", "default"],
    },
    "knowledge_agent_v1": {
        "name": "Knowledge Agent Prompt",
        "category": "knowledge_agent",
        "is_default": True,
        "is_global": True,
        "template_text": """You are the Knowledge Base Expert.
Your job is to answer the user's question using the company's knowledge base.
Always use the `search_knowledge_base` tool to find answers. 
If the user provides a link and asks you to learn from it, use `scrape_documentation_link`.
Never guess or hallucinate information. If the answer is not in the knowledge base, use transfer tools.
If the user asks about pricing, plans, "about us", or direct questions about the company, transfer to scraper.
SEAMLESS FALLBACK RULE: If search returns no results, immediately call transfer_to_scraper.""",
        "variables": [],
        "tags": ["knowledge", "v1", "default"],
    },
    "navigation_agent_v1": {
        "name": "Navigation Agent Prompt",
        "category": "navigation_agent",
        "is_default": True,
        "is_global": True,
        "template_text": """You are the Navigation & UI Guide Expert.
Your job is to guide users through the dashboard UI.
CRITICAL: You MUST use `get_dashboard_navigation` to fetch navigation data BEFORE answering.
Then call `render_navigation_guide` to send interactive visual steps to the screen.
CRITICAL RULES:
1. No text alongside tool calls. ONLY return the tool call.
2. After `render_navigation_guide`: DO NOT output ANY text at all.
3. ONLY respond with text after the tool if the tool returned an error.""",
        "variables": [],
        "tags": ["navigation", "v1", "default"],
    },
    "api_agent_v1": {
        "name": "API Agent Prompt",
        "category": "api_agent",
        "is_default": True,
        "is_global": True,
        "template_text": """You are the API & Integrations Expert.
1. Use `get_api_documentation` to understand available endpoints.
2. Use `query_company_api` to execute read-only GET requests.
Return data clearly formatted. If not found, use transfer tools.
CRITICAL RULE: No text before tool call. ONLY return the tool call.""",
        "variables": [],
        "tags": ["api", "v1", "default"],
    },
    "scraper_agent_v1": {
        "name": "Scraper Agent Prompt",
        "category": "scraper_agent",
        "is_default": True,
        "is_global": True,
        "template_text": """You are the Web Scraper Expert.

COMPANY WEBSITE: {website}

WORKFLOW:
1. If user asks about pricing/features and doesn't provide URL, fetch: {website}
2. Use `read_website_page` to fetch content and links.
3. If info not on page, follow relevant links (pricing, plans, features, about).
4. RETRY: If content missing, retry with `force_refresh=True`.
5. Answer based ONLY on page content.
6. If still not found, call `transfer_to_knowledge`.

FALLBACK: If `read_website_page` fails, call `transfer_to_knowledge`. No text when transferring.

CRITICAL RULE: No text before tool call. ONLY return the tool call.""",
        "variables": [
            PromptVariable(name="website", description="Company website URL", default_value="", required=True, example="https://example.com"),
        ],
        "tags": ["scraper", "v1", "default"],
    },
}


async def ensure_prompt_indexes():
    await db.prompt_templates.create_index("category")
    await db.prompt_templates.create_index([("company_id", 1), ("category", 1)])
    await db.prompt_templates.create_index("is_default")
    await db.prompt_templates.create_index("is_active")
    await db.prompt_templates.create_index([("company_id", 1), ("is_active", 1)])
    await db.prompt_template_versions.create_index("template_id")
    await db.prompt_template_versions.create_index([("template_id", 1), ("version", -1)])
    await db.prompt_template_versions.create_index("is_active")
    await db.company_prompt_overrides.create_index("company_id")
    await db.company_prompt_overrides.create_index([("company_id", 1), ("template_id", 1)], unique=True)
    await db.company_prompt_overrides.create_index("is_active")


async def seed_default_prompts():
    for template_id, data in _SEED_TEMPLATES.items():
        existing = await db.prompt_templates.find_one({"id": template_id})
        if not existing:
            now = datetime.now(timezone.utc)
            template = PromptTemplate(
                id=template_id, name=data["name"], category=data["category"],
                is_default=data["is_default"], is_global=data["is_global"],
                template_text=data["template_text"], version=1, is_active=True,
                variables=data.get("variables", []), tags=data.get("tags", []),
                created_by="system", created_by_name="System",
                change_reason="Initial default prompt",
                created_at=now, updated_at=now,
            )
            await db.prompt_templates.insert_one(template.model_dump())
            version = PromptTemplateVersion(
                template_id=template_id, version=1,
                template_text=data["template_text"],
                created_at=now, created_by="system", created_by_name="System",
                change_reason="Initial default prompt", is_active=True,
            )
            await db.prompt_template_versions.insert_one(version.model_dump())
    logger.info("Default prompts seeded: %d", len(_SEED_TEMPLATES))


def interpolate_variables(
    template_text: str, variables: Dict[str, Any],
    prompt_variables: Optional[List[PromptVariable]] = None,
) -> tuple[str, List[str]]:
    if not prompt_variables:
        return template_text, []
    missing: List[str] = []
    var_map = {v.name: v for v in prompt_variables}

    def replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name in variables and variables[var_name] is not None:
            return str(variables[var_name])
        elif var_name in var_map and var_map[var_name].default_value is not None:
            return str(var_map[var_name].default_value)
        else:
            missing.append(var_name)
            return match.group(0)

    rendered = _VAR_PATTERN.sub(replace, template_text)
    return rendered, list(set(missing))


async def get_template(template_id: str) -> Optional[PromptTemplate]:
    doc = await db.prompt_templates.find_one({"id": template_id})
    if doc:
        doc.pop("_id", None)
        return PromptTemplate(**doc)
    return None


async def get_template_by_category(category: str, company_id: Optional[str] = None) -> Optional[PromptTemplate]:
    if company_id:
        override = await get_company_override(company_id, category)
        if override and override.is_active:
            base = await get_template(f"{category}_v1")
            if base:
                base.template_text = override.template_text
                base.id = f"{category}_override_{company_id}"
                return base
    if company_id:
        company_template = await db.prompt_templates.find_one(
            {"category": category, "company_id": company_id, "is_active": True}
        )
        if company_template:
            company_template.pop("_id", None)
            return PromptTemplate(**company_template)
    global_template = await db.prompt_templates.find_one(
        {"category": category, "is_default": True, "is_active": True}
    )
    if global_template:
        global_template.pop("_id", None)
        return PromptTemplate(**global_template)
    return None


async def list_templates(category: Optional[str] = None, company_id: Optional[str] = None, include_global: bool = True) -> List[PromptTemplate]:
    query: Dict[str, Any] = {"is_active": True}
    if category:
        query["category"] = category
    if company_id and include_global:
        query["$or"] = [{"company_id": company_id}, {"is_global": True}]
    elif company_id:
        query["company_id"] = company_id
    cursor = db.prompt_templates.find(query).sort("category", 1)
    templates = []
    async for doc in cursor:
        doc.pop("_id", None)
        templates.append(PromptTemplate(**doc))
    return templates


async def create_template(data: PromptTemplateCreate, created_by: Optional[str] = None, created_by_name: Optional[str] = None) -> PromptTemplate:
    template_id = f"{data.category}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    now = datetime.now(timezone.utc)
    template = PromptTemplate(
        id=template_id, name=data.name, description=data.description, category=data.category,
        template_text=data.template_text, variables=data.variables, is_active=True,
        is_default=data.is_default, is_global=data.company_id is None, company_id=data.company_id,
        version=1, created_by=created_by, created_by_name=created_by_name,
        change_reason=data.change_reason or "Created via Prompt Studio",
        tags=data.tags, created_at=now, updated_at=now,
    )
    await db.prompt_templates.insert_one(template.model_dump())
    version = PromptTemplateVersion(
        template_id=template_id, version=1, template_text=data.template_text,
        created_at=now, created_by=created_by, created_by_name=created_by_name,
        change_reason=data.change_reason or "Created via Prompt Studio", is_active=True,
    )
    await db.prompt_template_versions.insert_one(version.model_dump())
    return template


async def update_template(template_id: str, data: PromptTemplateUpdate, updated_by: Optional[str] = None, updated_by_name: Optional[str] = None) -> Optional[PromptTemplate]:
    current = await get_template(template_id)
    if not current:
        return None
    now = datetime.now(timezone.utc)
    archive = PromptTemplateVersion(
        template_id=template_id, version=current.version, template_text=current.template_text,
        created_at=now, created_by=updated_by, created_by_name=updated_by_name,
        change_reason=data.change_reason, is_active=False,
    )
    await db.prompt_template_versions.insert_one(archive.model_dump())
    update_data = {"updated_at": now, "version": current.version + 1}
    if data.template_text is not None:
        update_data["template_text"] = data.template_text
    if data.variables is not None:
        update_data["variables"] = [v.model_dump() for v in data.variables]
    if data.is_default is not None:
        update_data["is_default"] = data.is_default
    if data.tags is not None:
        update_data["tags"] = data.tags
    await db.prompt_templates.update_one({"id": template_id}, {"$set": update_data})
    new_version = PromptTemplateVersion(
        template_id=template_id, version=current.version + 1,
        template_text=data.template_text or current.template_text,
        created_at=now, created_by=updated_by, created_by_name=updated_by_name,
        change_reason=data.change_reason, is_active=True,
    )
    await db.prompt_template_versions.insert_one(new_version.model_dump())
    return await get_template(template_id)


async def delete_template(template_id: str) -> bool:
    result = await db.prompt_templates.update_one(
        {"id": template_id}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}}
    )
    return result.modified_count > 0


async def get_version_history(template_id: str) -> PromptTemplateVersionHistory:
    cursor = db.prompt_template_versions.find({"template_id": template_id}).sort("version", -1)
    versions = []
    async for doc in cursor:
        doc.pop("_id", None)
        versions.append(PromptTemplateVersion(**doc))
    return PromptTemplateVersionHistory(template_id=template_id, versions=versions, total_versions=len(versions))


async def rollback_to_version(template_id: str, target_version: int, rolled_back_by: Optional[str] = None, rolled_back_by_name: Optional[str] = None, change_reason: Optional[str] = None) -> Optional[PromptTemplate]:
    current = await get_template(template_id)
    if not current:
        return None
    target_doc = await db.prompt_template_versions.find_one({"template_id": template_id, "version": target_version})
    if not target_doc:
        return None
    now = datetime.now(timezone.utc)
    archive = PromptTemplateVersion(
        template_id=template_id, version=current.version, template_text=current.template_text,
        created_at=now, created_by=rolled_back_by, created_by_name=rolled_back_by_name,
        change_reason=change_reason or f"Rolling back from v{current.version}", is_active=False,
    )
    await db.prompt_template_versions.insert_one(archive.model_dump())
    rollback_text = target_doc["template_text"]
    new_version = PromptTemplateVersion(
        template_id=template_id, version=current.version + 1, template_text=rollback_text,
        created_at=now, created_by=rolled_back_by, created_by_name=rolled_back_by_name,
        change_reason=change_reason or f"Rolled back to v{target_version}", is_active=True,
    )
    await db.prompt_template_versions.insert_one(new_version.model_dump())
    await db.prompt_templates.update_one(
        {"id": template_id},
        {"$set": {"template_text": rollback_text, "version": current.version + 1, "updated_at": now}},
    )
    return await get_template(template_id)


async def get_version_diff(template_id: str, version_a: int, version_b: int) -> Optional[PromptDiffResponse]:
    doc_a = await db.prompt_template_versions.find_one({"template_id": template_id, "version": version_a})
    doc_b = await db.prompt_template_versions.find_one({"template_id": template_id, "version": version_b})
    if not doc_a or not doc_b:
        return None
    text_a = doc_a["template_text"]
    text_b = doc_b["template_text"]
    from difflib import unified_diff
    diff = list(unified_diff(text_a.splitlines(), text_b.splitlines(), fromfile=f"v{version_a}", tofile=f"v{version_b}", lineterm=""))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    unchanged = [l[1:] for l in diff if l.startswith(" ")]
    return PromptDiffResponse(version_a=version_a, version_b=version_b, template_text_a=text_a, template_text_b=text_b, added_lines=added, removed_lines=removed, unchanged_lines=unchanged)


async def get_company_override(company_id: str, category: str) -> Optional[CompanyPromptOverride]:
    doc = await db.company_prompt_overrides.find_one({
        "company_id": company_id, "template_id": {"$regex": f"^{category}"}, "is_active": True,
    })
    if doc:
        doc.pop("_id", None)
        return CompanyPromptOverride(**doc)
    return None


async def create_or_update_company_override(company_id: str, template_id: str, template_text: str, variables_override: Optional[Dict[str, str]] = None, created_by: Optional[str] = None, change_reason: Optional[str] = None) -> CompanyPromptOverride:
    now = datetime.now(timezone.utc)
    existing = await db.company_prompt_overrides.find_one({"company_id": company_id, "template_id": template_id})
    if existing:
        await db.company_prompt_overrides.update_one(
            {"company_id": company_id, "template_id": template_id},
            {"$set": {"template_text": template_text, "variables_override": variables_override or {}, "updated_at": now, "change_reason": change_reason, "is_active": True}},
        )
    else:
        override = CompanyPromptOverride(
            id=f"{company_id}_{template_id}", company_id=company_id, template_id=template_id,
            template_text=template_text, variables_override=variables_override or {},
            created_at=now, updated_at=now, created_by=created_by, change_reason=change_reason,
        )
        await db.company_prompt_overrides.insert_one(override.model_dump())
    doc = await db.company_prompt_overrides.find_one({"company_id": company_id, "template_id": template_id})
    doc.pop("_id", None)
    return CompanyPromptOverride(**doc)


async def delete_company_override(company_id: str, template_id: str) -> bool:
    result = await db.company_prompt_overrides.update_one(
        {"company_id": company_id, "template_id": template_id},
        {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count > 0


async def render_prompt_for_company(category: str, company_data: Dict[str, Any], company_id: Optional[str] = None) -> RenderedPrompt:
    template = await get_template_by_category(category, company_id)
    if not template:
        return RenderedPrompt(template_id="unknown", version=0, rendered_text="", variables_used={}, was_interpolated=False, company_id=company_id)
    variables = {
        "company_name": company_data.get("name", ""),
        "company_description": company_data.get("description", ""),
        "company_industry": company_data.get("industry", ""),
        "company_website": company_data.get("website", ""),
        "agent_tone": company_data.get("brand_tone", "professional"),
        "agent_style": company_data.get("voice_style", "conversational"),
        "primary_language": company_data.get("primary_language", "en"),
        "current_date": company_data.get("current_date", ""),
        "current_time": company_data.get("current_time", ""),
    }
    custom_vars = company_data.get("custom_prompt_variables", {})
    variables.update(custom_vars)
    if company_id:
        override = await get_company_override(company_id, category)
        if override and override.variables_override:
            variables.update(override.variables_override)
    rendered_text, missing = interpolate_variables(template.template_text, variables, template.variables)
    if missing:
        logger.warning("Prompt template %s missing variables: %s", template.id, missing)
    return RenderedPrompt(
        template_id=template.id, version=template.version, rendered_text=rendered_text,
        variables_used=variables, was_interpolated=True, company_id=company_id,
        is_custom=template.company_id is not None,
    )


async def preview_prompt(data: PromptPreviewRequest) -> PromptPreviewResponse:
    prompt_vars = [PromptVariable(name=k, default_value=v) for k, v in data.variables.items()]
    rendered_text, missing = interpolate_variables(data.template_text, data.variables, prompt_vars)
    warnings = []
    if missing:
        warnings.append(f"Missing variables (kept as placeholders): {', '.join(missing)}")
    return PromptPreviewResponse(rendered_text=rendered_text, missing_variables=missing, warnings=warnings)
