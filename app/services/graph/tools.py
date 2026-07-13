import json
import logging
from typing import Optional

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from app.services import (
    knowledge_service,
    stroll_index_service,
    integration_service,
    page_reader_service,
)
from app.core.database import db

logger = logging.getLogger(__name__)


def _get_state(config: RunnableConfig) -> dict:
    return config.get("configurable", {}).get("state", {})


@tool
async def search_knowledge_base(query: str, config: RunnableConfig) -> dict:
    """Search the company knowledge base for answers to user questions. Use this for policies, pricing, guides, etc."""
    state = _get_state(config)
    company_id = state.get("company_id")
    user_id = state.get("user_id")

    if not company_id or not user_id:
        return {"error": "Company context not available"}

    search_result = await knowledge_service.search_knowledge(
        user_id, query, limit=3, threshold=0.5, company_id=company_id
    )
    results = search_result.get("results", [])
    if not results:
        return {"results": [], "message": "No relevant documents found"}
    return {
        "results": [
            {"title": r["title"], "content": r["content"], "score": r["score"]}
            for r in results
        ]
    }


@tool
async def scrape_documentation_link(url: str, config: RunnableConfig) -> dict:
    """Scrape a URL and add its contents to the knowledge base so you can search it later."""
    state = _get_state(config)
    company_id = state.get("company_id")
    user_id = state.get("user_id")

    if not company_id or not user_id:
        return {"error": "Company context not available"}

    from app.services.documentation_scraper_service import scrape_and_ingest_docs

    success = await scrape_and_ingest_docs(url, company_id, user_id)
    if success:
        return {"success": True, "message": f"Successfully scraped and ingested documentation from {url}."}
    return {"error": f"Failed to scrape documentation from {url}."}


@tool
async def get_dashboard_navigation(query: str, config: RunnableConfig) -> dict:
    """Get the visual navigation path and elements on the dashboard. Use this FIRST when a user asks 'how do I find...', 'where is...', or wants a visual guide/screenshots."""
    state = _get_state(config)
    company_id = state.get("company_id")

    if not company_id:
        return {"error": "Company context not available"}

    report_data = await stroll_index_service.generate_navigation_report(company_id)
    if report_data:
        return {
            "found": True,
            "navigation_report": report_data["report"],
        }
    return {
        "found": False,
        "message": "No dashboard navigation data available. A stroll has not been run yet.",
    }


@tool
async def get_full_dashboard_documentation(config: RunnableConfig) -> dict:
    """Get the complete step-by-step documentation for the entire dashboard. Use this when asked to output the complete navigation_steps JSON block."""
    state = _get_state(config)
    company_id = state.get("company_id")

    if not company_id:
        return {"error": "Company context not available"}

    full_docs = await stroll_index_service.get_all_navigation_steps(company_id)
    if full_docs and full_docs.steps:
        return {
            "found": True,
            "message": "Please output the following navigation_steps JSON block to the user so the frontend can render it.",
            "navigation_steps": [
                {
                    "page_id": step.page_title,
                    "instruction": step.instruction,
                    "element_selector": step.highlight.selector if step.highlight else None,
                }
                for step in full_docs.steps
            ],
        }
    return {
        "found": False,
        "message": "No dashboard documentation available. A stroll has not been run yet.",
    }


@tool
async def get_api_documentation(integration_name: Optional[str], config: RunnableConfig) -> dict:
    """Get documentation for external API integrations. Call without an argument to see available APIs, then call with the name to get specific docs."""
    state = _get_state(config)
    company_id = state.get("company_id")

    if not company_id:
        return {"error": "Company context not available"}

    docs = await integration_service.get_api_documentation(company_id, integration_name)
    return {"documentation": docs}


@tool
async def query_company_api(
    integration_name: str, endpoint_name: str, path_params: Optional[dict], query_params: Optional[dict], config: RunnableConfig
) -> dict:
    """Execute a read-only GET request against a company's API integration to look up real-time data."""
    state = _get_state(config)
    company_id = state.get("company_id")

    if not company_id:
        return {"error": "Company context not available"}

    result = await integration_service.execute_get_request(
        company_id=company_id,
        integration_name=integration_name,
        endpoint_name=endpoint_name,
        path_params=path_params,
        query_params=query_params,
    )
    return result


@tool
async def read_website_page(url: str, config: RunnableConfig) -> dict:
    """Read and extract text from a public website URL. Use this for general scraping of links."""
    if not url:
        return {"error": "No URL provided to read."}
    result = await page_reader_service.read_website_page(url)
    return result


# Map to lookup tools by name
ALL_TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "scrape_documentation_link": scrape_documentation_link,
    "get_dashboard_navigation": get_dashboard_navigation,
    "get_full_dashboard_documentation": get_full_dashboard_documentation,
    "get_api_documentation": get_api_documentation,
    "query_company_api": query_company_api,
    "read_website_page": read_website_page,
}
