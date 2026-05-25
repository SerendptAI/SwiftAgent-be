"""
Integration service — CRUD for company API integrations + secure GET execution.

Companies register their internal APIs. Keys are Fernet-encrypted at rest and
decrypted only when the agent invokes the `query_company_api` tool.

All external calls are **GET-only** with SSRF protection and response size caps.
"""

import ipaddress
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.core.database import db
from app.core.encryption import encrypt, decrypt

logger = logging.getLogger(__name__)

# Constants

_REQUEST_TIMEOUT = 5  # seconds
_MAX_RESPONSE_BYTES = 10_240  # 10 KB — prevent token flooding
_BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
]

COLLECTION = "company_integrations"


# Helpers

def _is_private_url(url: str) -> bool:
    """Check if a URL resolves to a private/internal IP (SSRF protection)."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return True
        # Simple check — block obvious private hostnames
        if hostname in ("localhost", "0.0.0.0"):
            return True
        try:
            addr = ipaddress.ip_address(hostname)
            return any(addr in net for net in _BLOCKED_IP_NETWORKS)
        except ValueError:
            # hostname is not an IP literal — allow DNS-resolved names
            return False
    except Exception:
        return True


def _sanitize_doc(doc: dict) -> dict:
    """Strip internal fields before returning to the API caller."""
    doc.pop("_id", None)
    doc.pop("api_key_encrypted", None)
    return doc


# CRUD

async def create_integration(company_id: str, data: dict) -> dict:
    """Create a new API integration. Encrypts the raw API key."""
    raw_key = data.pop("api_key")

    now = datetime.now(tz=timezone.utc)
    doc = {
        "id": str(uuid4()),
        "company_id": company_id,
        "name": data["name"],
        "base_url": data["base_url"],
        "api_key_encrypted": encrypt(raw_key),
        "auth_header": data.get("auth_header", "Authorization"),
        "auth_prefix": data.get("auth_prefix", "Bearer"),
        "documentation": data.get("documentation", ""),
        "endpoints": [ep if isinstance(ep, dict) else ep.model_dump() for ep in data.get("endpoints", [])],
        "active": True,
        "created_at": now,
        "updated_at": None,
    }

    await db[COLLECTION].insert_one(doc)
    logger.info("Created integration %s for company %s", doc["id"], company_id)

    return _sanitize_doc(doc)


async def list_integrations(company_id: str) -> list[dict]:
    """List all active integrations for a company (no keys exposed)."""
    cursor = db[COLLECTION].find(
        {"company_id": company_id, "active": True},
        {"_id": 0, "api_key_encrypted": 0},
    ).sort("created_at", -1)
    return await cursor.to_list(length=50)


async def get_integration(company_id: str, integration_id: str) -> dict | None:
    """Get a single integration (no key exposed)."""
    doc = await db[COLLECTION].find_one(
        {"company_id": company_id, "id": integration_id, "active": True},
        {"_id": 0, "api_key_encrypted": 0},
    )
    return doc


async def update_integration(company_id: str, integration_id: str, data: dict) -> dict | None:
    """Partial update. Re-encrypts the API key if a new one is provided."""
    update_fields: dict = {}

    for field in ("name", "base_url", "auth_header", "auth_prefix", "documentation"):
        if field in data and data[field] is not None:
            update_fields[field] = data[field]

    if "endpoints" in data and data["endpoints"] is not None:
        update_fields["endpoints"] = [
            ep if isinstance(ep, dict) else ep.model_dump() for ep in data["endpoints"]
        ]

    if "api_key" in data and data["api_key"] is not None:
        update_fields["api_key_encrypted"] = encrypt(data["api_key"])

    if not update_fields:
        return await get_integration(company_id, integration_id)

    update_fields["updated_at"] = datetime.now(tz=timezone.utc)

    result = await db[COLLECTION].update_one(
        {"company_id": company_id, "id": integration_id, "active": True},
        {"$set": update_fields},
    )

    if result.matched_count == 0:
        return None

    logger.info("Updated integration %s for company %s", integration_id, company_id)
    return await get_integration(company_id, integration_id)


async def delete_integration(company_id: str, integration_id: str) -> bool:
    """Soft-delete (deactivate) an integration."""
    result = await db[COLLECTION].update_one(
        {"company_id": company_id, "id": integration_id, "active": True},
        {"$set": {"active": False, "updated_at": datetime.now(tz=timezone.utc)}},
    )
    if result.modified_count > 0:
        logger.info("Deactivated integration %s for company %s", integration_id, company_id)
        return True
    return False

# Agent helpers

async def get_integration_summaries(company_id: str) -> list[dict]:
    """
    Return lightweight summaries of all active integrations for the agent.

    Only includes name + endpoint list — no keys, no docs.
    Loaded on-demand via the `get_api_documentation` tool.
    """
    cursor = db[COLLECTION].find(
        {"company_id": company_id, "active": True},
        {"_id": 0, "name": 1, "endpoints": 1},
    )
    return await cursor.to_list(length=50)


async def get_api_documentation(company_id: str, integration_name: str | None = None) -> str:
    """
    Return the documentation text for agent consumption.

    If integration_name is given, returns that integration's docs + endpoints.
    Otherwise returns a summary of all available integrations.
    """
    if integration_name:
        doc = await db[COLLECTION].find_one(
            {"company_id": company_id, "name": integration_name, "active": True},
            {"_id": 0, "name": 1, "documentation": 1, "endpoints": 1, "base_url": 1},
        )
        if not doc:
            return f"No integration named '{integration_name}' found."

        lines = [f"## {doc['name']}", f"Base URL: {doc['base_url']}", ""]
        if doc.get("documentation"):
            lines.append(doc["documentation"])
            lines.append("")
        lines.append("### Available Endpoints (GET only):")
        for ep in doc.get("endpoints", []):
            lines.append(f"- **{ep['name']}**: `GET {ep['path']}`")
            lines.append(f"  {ep['description']}")
            if ep.get("query_params"):
                lines.append(f"  Default query params: {json.dumps(ep['query_params'])}")
        return "\n".join(lines)

    # Return summary of all integrations
    integrations = await list_integrations(company_id)
    if not integrations:
        return "No API integrations are registered for this company."

    lines = ["# Available API Integrations", ""]
    for intg in integrations:
        lines.append(f"## {intg['name']}")
        for ep in intg.get("endpoints", []):
            lines.append(f"- **{ep['name']}**: {ep['description']}")
        lines.append("")
    lines.append("Call `get_api_documentation` with a specific integration name for full docs.")
    return "\n".join(lines)


async def get_active_integration_count(company_id: str) -> int:
    """Return the count of active integrations for system prompt hinting."""
    return await db[COLLECTION].count_documents(
        {"company_id": company_id, "active": True}
    )


# Secure GET execution

async def execute_get_request(
    company_id: str,
    integration_name: str,
    endpoint_name: str,
    path_params: dict | None = None,
    query_params: dict | None = None,
) -> dict:
    """
    Execute a read-only GET request against a company's registered API.

    Security guardrails:
    - GET method only (enforced here, never POST/PUT/DELETE)
    - SSRF protection (block private/internal IPs)
    - 5-second timeout
    - 10 KB response body cap
    - Audit logging of every call
    """
    # 1. Fetch integration with encrypted key
    doc = await db[COLLECTION].find_one(
        {"company_id": company_id, "name": integration_name, "active": True},
    )
    if not doc:
        return {"error": f"Integration '{integration_name}' not found or inactive."}

    # 2. Find the target endpoint
    endpoint = None
    for ep in doc.get("endpoints", []):
        if ep["name"] == endpoint_name:
            endpoint = ep
            break
    if not endpoint:
        available = [ep["name"] for ep in doc.get("endpoints", [])]
        return {
            "error": f"Endpoint '{endpoint_name}' not found in '{integration_name}'.",
            "available_endpoints": available,
        }

    # 3. Build the full URL
    path = endpoint["path"]
    if path_params:
        for key, value in path_params.items():
            path = path.replace(f"{{{key}}}", str(value))

    # Check for unsubstituted path params
    if "{" in path:
        return {"error": f"Missing path parameters in '{path}'. Provide all required path_params."}

    full_url = f"{doc['base_url']}{path}"

    # 4. SSRF check
    if _is_private_url(full_url):
        return {"error": "Cannot make requests to internal/private addresses."}

    # 5. Decrypt API key and build headers
    try:
        raw_key = decrypt(doc["api_key_encrypted"])
    except ValueError as e:
        return {"error": str(e)}

    auth_header = doc.get("auth_header", "Authorization")
    auth_prefix = doc.get("auth_prefix", "Bearer")
    auth_value = f"{auth_prefix} {raw_key}".strip() if auth_prefix else raw_key

    headers = {
        auth_header: auth_value,
        "User-Agent": "SwiftAgent/1.0",
        "Accept": "application/json",
    }

    # Merge endpoint-specific headers
    if endpoint.get("headers"):
        headers.update(endpoint["headers"])

    # 6. Merge query params (endpoint defaults + caller overrides)
    merged_params = {}
    if endpoint.get("query_params"):
        merged_params.update(endpoint["query_params"])
    if query_params:
        merged_params.update(query_params)

    # 7. Execute the request
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=False,  # no redirects — prevents SSRF bypass
        ) as client:
            response = await client.get(full_url, headers=headers, params=merged_params)

        logger.info(
            "Integration API call: company=%s integration=%s endpoint=%s status=%d",
            company_id, integration_name, endpoint_name, response.status_code,
        )

        # 8. Parse and cap the response
        body = response.text[:_MAX_RESPONSE_BYTES]

        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            result = {"raw_response": body}

        return {
            "status_code": response.status_code,
            "data": result,
        }

    except httpx.TimeoutException:
        logger.warning(
            "Integration API timeout: company=%s integration=%s endpoint=%s",
            company_id, integration_name, endpoint_name,
        )
        return {"error": "The API request timed out (5s limit). The external service may be slow."}

    except httpx.RequestError as e:
        logger.error(
            "Integration API request error: company=%s integration=%s error=%s",
            company_id, integration_name, str(e),
        )
        return {"error": f"Failed to connect to the API: {str(e)}"}
