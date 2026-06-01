"""
AI Agent Service — Anthropic Claude integration with tool use for crypto support.

Uses Claude with native tool use to:
1. Search company knowledge base (RAG) for document-based answers
2. Look up blockchain transactions and wallets on-chain
3. Diagnose common crypto transaction issues

Conversation history is stored in MongoDB.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

import anthropic

from app.core.config import settings
from app.core.database import db
from app.core.validators import validate_email
from app.core.utils import get_random_avatar
from app.services import knowledge_service, chain_service, stroll_index_service, memory_service, company_email_service, page_reader_service, integration_service
from app.services.stroll_index_service import extract_navigation_steps, reconstruct_navigation_guide
from app.services.blockchain import detect, evm, bitcoin, prices
from app.models.memory_models import WorkingMemory
from app.services.attachment_service import format_for_anthropic, extract_attachment_content, build_persistable_attachments

logger = logging.getLogger(__name__)

# anthropic client
_client = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


MODEL = settings.ANTHROPIC_MODEL


async def generate_chat_title(first_message: str) -> str:
    """Intelligently generate a short title for a chat based on the first message."""
    try:
        client = _get_client()
        response = await client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=15,
            temperature=0.7,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate a very short, concise title (max 5 words) summarizing this user query: '{first_message}'. Return ONLY the raw title text, nothing else, no quotes or prefixes."
                }
            ]
        )
        title = response.content[0].text.strip(' "\'')
        if not title:
            return "New Chat"
        if len(title) > 60:
            title = title[:57] + "..."
        return title
    except Exception as e:
        logger.error(f"Failed to generate chat title: {e}")
        return "New Chat"


# tool definitions (anthropic tool-use format)

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the company's knowledge base and documentation for relevant information. "
            "Use this when a customer asks a question that might be answered by company "
            "documentation, FAQs, policies, or guides. Do NOT use this for simple greetings, "
            "small talk, or follow-up questions where you already have the context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant documents. Use the customer's question or a refined version of it.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_transaction",
        "description": (
            "Look up a blockchain transaction by its hash. Use this when a customer "
            "pastes a transaction hash (tx hash). Returns transaction details including "
            "status, gas used, value, confirmations, and more."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hash": {
                    "type": "string",
                    "description": "The transaction hash to look up",
                },
                "chain": {
                    "type": "string",
                    "description": (
                        "Optional blockchain chain name: ethereum, bsc, polygon, "
                        "arbitrum, base, avalanche, or bitcoin. If not provided, "
                        "the chain will be auto-detected."
                    ),
                },
            },
            "required": ["hash"],
        },
    },
    {
        "name": "lookup_wallet",
        "description": (
            "Look up a blockchain wallet/address. Use this when a customer pastes "
            "a wallet address. Returns balance, transaction count, recent activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "The wallet address to look up",
                },
                "chain": {
                    "type": "string",
                    "description": (
                        "Optional blockchain chain name: ethereum, bsc, polygon, "
                        "arbitrum, base, avalanche, or bitcoin. If not provided, "
                        "the chain will be auto-detected."
                    ),
                },
            },
            "required": ["address"],
        },
    },
    {
        "name": "diagnose_problem",
        "description": (
            "Diagnose issues with a transaction. Use this AFTER looking up a "
            "transaction to analyze what went wrong. Checks for common failure modes "
            "like low gas, reverted contracts, missing deposits, suspicious activity, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tx_data": {
                    "type": "string",
                    "description": "JSON string of the transaction data from lookup_transaction",
                },
                "customer_complaint": {
                    "type": "string",
                    "description": "Description of the customer's issue in plain text",
                },
            },
            "required": ["tx_data", "customer_complaint"],
        },
    },
    {
        "name": "get_dashboard_navigation",
        "description": (
            "Access the visual dashboard navigation system. Returns a report containing "
            "page maps, interactive elements, AND ACTUAL SCREENSHOTS. Use this tool "
            "whenever a user asks to 'see' something, asks for 'screenshots', or asks "
            "'how to' do anything in the dashboard. This tool is the ONLY way you can "
            "provide the visual guide and screenshots the user is requesting. "
            "After reading the report, you MUST respond with a ```navigation_steps JSON block "
            "listing the ordered steps to trigger the visual display."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The feature, page, or setting the user is looking for",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_support_ticket",
        "description": (
            "Create a support ticket when you cannot resolve the customer's issue and it "
            "requires human intervention from the company's support team. Before using this "
            "tool, you MUST: 1) Attempt to resolve the issue using other tools first, "
            "2) Ask the customer for their email address, 3) Get confirmation they want "
            "to create a ticket. Provide a clear summary of the issue and what was tried."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_email": {
                    "type": "string",
                    "description": "The customer's email address for ticket correspondence",
                },
                "customer_name": {
                    "type": "string",
                    "description": "The customer's name, if provided",
                },
                "subject": {
                    "type": "string",
                    "description": "A brief subject line summarizing the issue (max 100 chars)",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "A detailed summary of the issue for the support team. Include: "
                        "what the customer's problem is, what you tried, and why it couldn't be resolved."
                    ),
                },
            },
            "required": ["customer_email", "subject", "summary"],
        },
    },
    {
        "name": "get_full_dashboard_documentation",
        "description": (
            "Retrieve the complete documentation for the entire dashboard. Use this tool "
            "when the customer asks for 'all documentation', 'the full manual', 'everything about the dashboard', "
            "or wants a complete overview of all features. "
            "After calling this tool, you MUST respond with the returned navigation_steps JSON block exactly as provided, "
            "which the frontend will use to render the complete visual guide."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "scrape_documentation_link",
        "description": (
            "Scrape an external documentation link and ingest it into the company's knowledge base. "
            "Use this tool when a user provides a URL to documentation (like Notion, Gitbook, etc.) "
            "and asks you to 'read', 'learn', 'scrape', or 'ingest' it. "
            "After it succeeds, you can use search_knowledge_base to answer questions about it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the documentation to scrape",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_website_page",
        "description": (
            "Read the contents of a specific website URL. "
            "Returns the visible text on the page and a list of links found on that page. "
            "Use this to answer questions by reading the user's current page, or to follow "
            "links to find more information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the page to read",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_api_documentation",
        "description": (
            "Retrieve the API documentation for a company's registered internal API. "
            "Call this BEFORE using query_company_api to understand what endpoints are "
            "available, what parameters they accept, and what they return. "
            "If the customer's question might be answerable by looking up company data, call this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "integration_name": {
                    "type": "string",
                    "description": (
                        "Optional: name of a specific integration. "
                        "If omitted, returns a summary of ALL available integrations."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_company_api",
        "description": (
            "Query one of the company's registered internal APIs to verify or look up "
            "customer data. This tool makes READ-ONLY (GET) requests. Use it when a "
            "customer asks you to verify an order, check a balance, confirm a shipment, "
            "or look up any record that the company has exposed via their API. "
            "IMPORTANT: Before calling this tool, first call get_api_documentation "
            "to understand available endpoints and required parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "integration_name": {
                    "type": "string",
                    "description": "The name of the registered API integration to use",
                },
                "endpoint_name": {
                    "type": "string",
                    "description": "The specific endpoint to call (from the API documentation)",
                },
                "path_params": {
                    "type": "object",
                    "description": "Path parameters to substitute in the URL, e.g. {\"order_id\": \"ORD-12345\"}",
                },
                "query_params": {
                    "type": "object",
                    "description": "Optional query string parameters",
                },
            },
            "required": ["integration_name", "endpoint_name"],
        },
    },
]


# user-friendly labels for each tool, streamed as thinking stages
TOOL_STAGE_LABELS = {
    "search_knowledge_base": "Searching knowledge base…",
    "lookup_transaction": "Looking up transaction…",
    "lookup_wallet": "Looking up wallet…",
    "diagnose_problem": "Diagnosing transaction issue…",
    "get_dashboard_navigation": "Searching dashboard navigation…",
    "get_full_dashboard_documentation": "Generating complete dashboard documentation…",
    "scrape_documentation_link": "Scraping documentation link…",
    "create_support_ticket": "Creating support ticket…",
    "read_website_page": "Reading website page…",
    "get_api_documentation": "Loading API documentation…",
    "query_company_api": "Querying company API…",
}

# Multi-stage stream text flows for each tool (Agent-specific)
# Agent 001: Bank records and payment status
# Agent 007: Website search
# Agent 047: Dashboard feature location
# Agent 626: Cryptocurrency transaction analysis
TOOL_STAGES = {
    # Agent 001: lookup_transaction - Bank record analysis
    "lookup_transaction": [
        "AGENT IS SEARCHING BANK RECORDS",
        "ACCESSING BANK RECORDS",
        "CHECKING PAYMENT STATUS",
        "VERIFYING TRANSACTION DETAILS",
        "RECORDS FOUND",
    ],
    # Agent 001: lookup_wallet - Payment verification
    "lookup_wallet": [
        "AGENT IS SEARCHING PAYMENT RECORDS",
        "CONNECTING TO PAYMENT SYSTEM",
        "VERIFYING WALLET INFORMATION",
        "PROCESSING REFUND STATUS",
        "PAYMENT INFORMATION FOUND",
    ],
    # Agent 001 & 007: search_knowledge_base
    "search_knowledge_base": [
        "AGENT IS SEARCHING",
        "SCANNING KNOWLEDGE BASE",
        "EXTRACTING RELEVANT INFORMATION",
        "INFORMATION FOUND",
    ],
    # Agent 626: lookup_transaction - Blockchain analysis
    "lookup_transaction_crypto": [
        "AGENT IS SEARCHING",
        "CONNECTING TO BLOCKCHAIN",
        "ANALYZING HASHCODE",
        "VERIFYING TRANSACTION",
        "TRANSACTION FOUND",
    ],
    # Agent 626: lookup_wallet - Wallet analysis
    "lookup_wallet_crypto": [
        "AGENT IS SEARCHING",
        "CONNECTING TO BLOCKCHAIN",
        "ANALYZING WALLET ADDRESS",
        "RETRIEVING TRANSACTION HISTORY",
        "WALLET INFORMATION FOUND",
    ],
    # Agent 626: diagnose_problem
    "diagnose_problem": [
        "AGENT IS SEARCHING",
        "ANALYZING TRANSACTION DETAILS",
        "IDENTIFYING POTENTIAL ISSUES",
        "ISSUES IDENTIFIED",
    ],
    # Agent 007: scrape_documentation_link
    "scrape_documentation_link": [
        "AGENT IS SEARCHING",
        "CRAWLING WEBSITE",
        "SCANNING PAGES",
        "EXTRACTING DOCUMENTATION",
        "DOCUMENTATION FOUND",
    ],
    # Agent 047: get_dashboard_navigation
    "get_dashboard_navigation": [
        "AGENT IS SEARCHING",
        "LOCATING FEATURE IN DASHBOARD",
        "MAPPING NAVIGATION PATHS",
        "FEATURE FOUND",
        "GENERATING DIRECTIONS",
    ],
    # Agent 047: get_full_dashboard_documentation
    "get_full_dashboard_documentation": [
        "AGENT IS SEARCHING",
        "SCANNING ENTIRE DASHBOARD",
        "MAPPING ALL FEATURES",
        "CAPTURING INTERFACE DETAILS",
        "DASHBOARD DOCUMENTATION FOUND",
    ],
    # Fallback for create_support_ticket
    "create_support_ticket": [
        "AGENT IS PREPARING",
        "CREATING SUPPORT TICKET",
        "TICKET CREATED SUCCESSFULLY",
    ],
    "read_website_page": [
        "AGENT IS SEARCHING",
        "LOADING WEBPAGE",
        "READING PAGE CONTENT",
        "EXTRACTING RELEVANT DATA",
    ],
    "get_api_documentation": [
        "AGENT IS SEARCHING",
        "LOADING API DOCUMENTATION",
        "DOCUMENTATION LOADED",
    ],
    "query_company_api": [
        "AGENT IS VERIFYING",
        "CONNECTING TO COMPANY API",
        "QUERYING DATA",
        "VERIFYING RECORDS",
        "DATA RETRIEVED",
    ],
}


# Helper function to emit multi-stage streams for tools
def _get_tool_stages(tool_name: str) -> list[str]:
    """Get the multi-stage stream text for a given tool."""
    return TOOL_STAGES.get(tool_name, [f"Processing {tool_name}..."])


# system prompt builder


def _build_system_prompt(company: dict, memory_context: str = "", page_url: str = "", integration_count: int = 0) -> str:
    company_name = company.get("name", "the company")
    brand_tone = company.get("brand_tone", "professional and helpful")
    description = company.get("description", "")
    company_type = company.get("company_type", "")

    memory_section = ""
    if memory_context:
        memory_section = f"\nMEMORY CONTEXT (from previous conversations):\n{memory_context}\n"
        
    resolved_url = page_url if page_url else company.get("website", "")
    context_section = ""
    if resolved_url:
        location_type = "is currently viewing this URL" if page_url else "is visiting the company website"
        context_section = f"""
CURRENT USER LOCATION:
The customer {location_type}: {resolved_url}

If they ask a question that might be answered by the page they are on, use the `read_website_page` tool on that URL to read it.
If the answer isn't there, you can use `read_website_page` on the links returned by the tool to search deeper into the website.
"""

    return f"""You are a senior customer support agent for {company_name}. \
You respond with expertise, empathy, and clarity.
{context_section}

COMPANY CONTEXT:
- Name: {company_name}
- Type: {company_type}
- Description: {description}
- Tone: {brand_tone}
{memory_section}

YOUR BEHAVIOR:
1. You are warm, professional, and knowledgeable.
2. You answer questions based on the company's documentation and knowledge base. \
Use the search_knowledge_base tool to find relevant information when the customer asks \
a question that might be answered by company documentation.
3. For crypto companies: when a customer provides a transaction hash or wallet address, \
use your tools to look up real on-chain data and diagnose issues.
4. ALWAYS explain things in plain language. Assume the customer may not be technical.
5. When you find an issue, explain WHAT happened, WHY it happened, and WHAT TO DO about it.
6. If you don't know or can't find information, say so honestly. Never fabricate data.
7. Present monetary values with USD equivalents when possible.
8. For security-related issues (mixer interactions, unlimited approvals, drainer patterns), \
flag them clearly with appropriate urgency.

TOOL USAGE:
- Use search_knowledge_base when the customer asks about company policies, features, pricing, \
FAQs, how-to guides, or anything that might be in the company documentation.
- COMPULSORY: You MUST ALWAYS use get_dashboard_navigation when the customer asks "how to", \
"where is X?", "how do I find X?", "show me X", "show me the screenshot", or any question \
involving steps or navigation in the dashboard. You must NEVER claim you cannot show screenshots; \
instead, use this tool and output the visual guide.
- Use get_full_dashboard_documentation when the customer asks for the full manual or all documentation for the dashboard.
- Use scrape_documentation_link when the customer gives you a URL and asks you to learn or scrape the documentation.
- When you see a string that looks like a transaction hash (0x... followed by 64 hex chars, \
or 64 hex chars without 0x for Bitcoin), use lookup_transaction.
- When you see a wallet address (0x... followed by 40 hex chars, or a Bitcoin address), \
use lookup_wallet.
- After getting transaction data, if the customer has a problem, use diagnose_problem \
to run the data through the diagnosis engine.
- You can chain tools: first lookup, then diagnose.
- Do NOT use any tools for simple greetings or small talk.
- If the customer includes a URL or link in their message, you MUST use the `read_website_page` \
tool to read its contents and use the information to inform your response. Do NOT ignore links.

NAVIGATION GUIDE FORMAT:
When you receive a navigation report from the get_dashboard_navigation tool, identify the \
correct sequence of pages the user needs to visit. Then respond with:
1. A ```navigation_steps fenced code block containing a JSON array of steps in order:
   [{{\"page_id\": \"<id>\", \"instruction\": \"<natural language step>\", \"element_selector\": \"<css selector of element to click>\"}}]
   - page_id MUST match a page ID from the report
   - element_selector should be the selector of the element to click on that page (for highlighting)
   - Only include pages that are part of the path, in order
2. After the JSON block, write a brief conversational summary of the steps.

If you used the get_full_dashboard_documentation tool, the tool will return a pre-formatted \
`navigation_steps` JSON block. You MUST output this EXACT JSON block inside a ```navigation_steps \
fenced block without modifying it.

CRITICAL VISUALIZATION RULE:
By outputting the ```navigation_steps JSON block, the frontend will automatically render an interactive visual guide with screenshots and highlighted elements for the user. Therefore:
1. If a user asks to "see", "show screenshots", wants visual directions, or asks ANY "how to" or step-by-step question, you MUST use the get_dashboard_navigation tool (or get_full_dashboard_documentation) and output the navigation_steps block. This is COMPULSORY.
2. DO NOT claim you cannot show images.
3. NEVER provide text-only step-by-step navigation directions. Always provide the visual guide.

Only reference pages and elements that exist in the navigation report. Never invent pages or UI elements.

RESPONSE FORMAT:
- Match your response length to the complexity of the question.
- For simple greetings or casual messages, respond briefly and naturally (1-2 sentences).
- Only use structured responses (sections, bullet points) for complex or technical questions.
- Keep answers concise and to the point. Avoid unnecessary preamble or filler.

TICKET ESCALATION:
- If you genuinely cannot resolve a customer's issue after trying available tools, offer to \
create a support ticket so the company's human team can help.
- You MUST ask for the customer's email address before creating a ticket.
- You MUST get the customer's confirmation before creating the ticket.
- After creating the ticket, tell the customer the ticket ID and that the team will follow up via email.
- Never create a ticket without the customer's explicit consent.
{f'''
COMPANY API INTEGRATIONS:
This company has {integration_count} registered internal API(s) you can query to verify customer data.
- Use `get_api_documentation` FIRST to see what endpoints are available
- Then use `query_company_api` to make read-only (GET) requests to look up or verify data
- Only use these when a customer asks you to verify, check, or look up specific data
- NEVER modify data — these are read-only lookups only
''' if integration_count > 0 else ''}
"""


# tool execution (shared with gemini service)


async def _execute_tool(name: str, args: dict, company: dict = None, session_id: str | None = None) -> dict:
    """Execute a tool call and return the result."""
    try:
        if name == "search_knowledge_base":
            query = args.get("query", "")
            if not company:
                return {"error": "Company context not available"}
            user_id = company.get("user_id", "")
            company_id = company.get("id", "")
            if not user_id:
                return {"results": [], "message": "No knowledge base configured"}
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

        elif name == "lookup_transaction":
            tx_hash = args.get("hash", "")
            chain = args.get("chain")

            if not chain:
                detected = detect.detect_hash(tx_hash)
                if detected["type"] == "unknown":
                    return {"error": f"Could not recognize hash format: {tx_hash}"}
                chain = detected["chain"]

                # For EVM hashes, probe for the right chain
                if chain != "bitcoin" and len(detected.get("possible_chains", [])) > 1:
                    probed = await evm.probe_transaction_chain(tx_hash)
                    if probed:
                        chain = probed

            if chain == "bitcoin":
                result = await bitcoin.get_transaction(tx_hash)
            else:
                result = await evm.get_transaction(chain, tx_hash)

            # Add USD price if possible
            if result and "error" not in result:
                price_data = await prices.get_price(chain)
                if "price_usd" in price_data:
                    native_val = result.get("value_native") or result.get("total_output_btc", 0)
                    result["value_usd"] = prices.convert_to_usd(native_val, price_data["price_usd"])
                    fee_native = result.get("tx_fee_native") or result.get("fee_btc", 0)
                    result["fee_usd"] = prices.convert_to_usd(fee_native, price_data["price_usd"])

            return result or {"error": "Transaction not found"}

        elif name == "lookup_wallet":
            address = args.get("address", "")
            chain = args.get("chain")

            if not chain:
                detected = detect.detect_hash(address)
                if detected["type"] == "unknown":
                    return {"error": f"Could not recognize address format: {address}"}
                chain = detected["chain"]

                if chain != "bitcoin":
                    probed = await evm.probe_address_chain(address)
                    if probed:
                        chain = probed

            if chain == "bitcoin":
                result = await bitcoin.get_wallet(address)
            else:
                result = await evm.get_wallet(chain, address)

            # Add USD balance
            if result and "error" not in result:
                price_data = await prices.get_price(chain)
                if "price_usd" in price_data:
                    native_bal = result.get("balance_native") or result.get("balance_btc", 0)
                    result["balance_usd"] = prices.convert_to_usd(
                        native_bal, price_data["price_usd"]
                    )

            return result or {"error": "Address not found"}

        elif name == "diagnose_problem":
            tx_data_str = args.get("tx_data", "{}")
            complaint = args.get("customer_complaint", "")

            try:
                tx_data = json.loads(tx_data_str) if isinstance(tx_data_str, str) else tx_data_str
            except json.JSONDecodeError:
                tx_data = {}

            return await chain_service.diagnose_transaction(tx_data, complaint)

        elif name == "get_dashboard_navigation":
            query = args.get("query", "")
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
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

        elif name == "get_full_dashboard_documentation":
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            full_docs = await stroll_index_service.get_all_navigation_steps(company_id)
            if full_docs and full_docs.steps:
                return {
                    "found": True,
                    "message": "Please output the following navigation_steps JSON block to the user so the frontend can render it.",
                    "navigation_steps": [
                        {
                            "page_id": step.page_title, # Note: get_all_navigation_steps returns FindFeatureResult. 
                            "instruction": step.instruction,
                            "element_selector": step.highlight.selector if step.highlight else None
                        }
                        for step in full_docs.steps
                    ]
                }
            return {
                "found": False,
                "message": "No dashboard documentation available. A stroll has not been run yet.",
            }

        elif name == "scrape_documentation_link":
            url = args.get("url", "")
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            user_id = company.get("user_id", "")
            
            # Lazy import to avoid circular dependency
            from app.services.documentation_scraper_service import scrape_and_ingest_docs
            success = await scrape_and_ingest_docs(url, company_id, user_id)
            if success:
                return {"success": True, "message": f"Successfully scraped and ingested documentation from {url}."}
            return {"error": f"Failed to scrape documentation from {url}."}

        elif name == "create_support_ticket":
            if not company:
                return {"error": "Company context not available"}
            customer_email = args.get("customer_email", "")
            customer_name = args.get("customer_name")
            subject = args.get("subject", "Support request")
            summary = args.get("summary", "")
            company_id = company.get("id", "")

            email_valid, email_error = validate_email(customer_email)
            if not email_valid:
                return {"error": f"Invalid email address: {email_error}"}

            # check if company has email configured
            if not company.get("email_slug"):
                return {
                    "error": "Email ticketing is not configured for this company. "
                    "Please ask the customer to contact support directly."
                }

            try:
                # Use session_id from function parameter (passed from chat context)
                ticket = await company_email_service.create_ticket(
                    company_id=company_id,
                    customer_email=customer_email,
                    subject=subject,
                    chat_summary=summary,
                    chat_session_id=session_id,
                    customer_name=customer_name,
                )
                return {
                    "success": True,
                    "ticket_id": ticket["id"],
                    "message": f"Ticket #{ticket['id']} created. The support team will follow up at {customer_email}.",
                }
            except Exception as e:
                logger.exception("Failed to create support ticket")
                return {"error": f"Failed to create ticket: {str(e)}"}

        elif name == "read_website_page":
            url = args.get("url", "")
            if not url:
                return {"error": "No URL provided to read."}
            result = await page_reader_service.read_website_page(url)
            return result

        elif name == "get_api_documentation":
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            intg_name = args.get("integration_name")
            docs = await integration_service.get_api_documentation(company_id, intg_name)
            return {"documentation": docs}

        elif name == "query_company_api":
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            result = await integration_service.execute_get_request(
                company_id=company_id,
                integration_name=args.get("integration_name", ""),
                endpoint_name=args.get("endpoint_name", ""),
                path_params=args.get("path_params"),
                query_params=args.get("query_params"),
            )
            return result

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.exception(f"Tool execution error: {name}")
        return {"error": f"Tool '{name}' failed: {str(e)}"}


# conversation management


async def _load_conversation(company_id: str, session_id: str) -> list[dict]:
    """Load conversation history from MongoDB."""
    convo = await db.widget_conversations.find_one(
        {
            "company_id": company_id,
            "session_id": session_id,
        }
    )
    if convo:
        return convo.get("messages", [])
    return []


async def _save_conversation(company_id: str, session_id: str, messages: list[dict]):
    """Save/update conversation in MongoDB."""
    await db.widget_conversations.update_one(
        {"company_id": company_id, "session_id": session_id},
        {
            "$set": {
                "messages": messages,
                "updated_at": datetime.now(tz=timezone.utc),
                "seen": False,
            },
            "$setOnInsert": {
                "id": str(uuid4()),
                "company_id": company_id,
                "session_id": session_id,
                "created_at": datetime.now(tz=timezone.utc),
                "avatar": get_random_avatar(),
            },
        },
        upsert=True,
    )


# main chat function


async def chat(company_id: str, session_id: str, user_message: str, user_id: str = None, page_url: str = None) -> dict:
    """
    Process a chat message from the widget using Anthropic Claude.

    Flow:
    1. Load conversation history
    2. Build messages array with system prompt + history + new message
    3. Send to Claude with tools (including search_knowledge_base)
    4. If tool call → execute, feed result back → get final response
    5. Save conversation, return response
    """
    # load company info
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return {
            "reply": "Sorry, I couldn't find the company configuration. Please contact support.",
            "sources": [],
            "blockchain_data": None,
        }

    # load conversation history
    history = await _load_conversation(company_id, session_id)

    memory_context = await memory_service.load_memory_context(
        company_id=company_id,
        user_id=user_id,
        session_id=session_id,
    )
    memory_str = memory_service.format_memory_context(memory_context)

    working_mem = memory_context.working_memory
    if not working_mem:
        working_mem = WorkingMemory(session_id=session_id)
        if user_id:
            working_mem.identified_user = True

    # build system prompt and messages
    integration_count = await integration_service.get_active_integration_count(company_id)
    system_prompt = _build_system_prompt(company, memory_str, page_url, integration_count)

    # convert history to Claude format
    claude_messages = []
    for msg in history[-10:]:  # keep last 10 messages for context
        role = "user" if msg["role"] == "user" else "assistant"
        claude_messages.append({"role": role, "content": msg["content"]})

    # add new user message
    claude_messages.append({"role": "user", "content": user_message})

    # call claude
    client = _get_client()
    blockchain_data = None
    sources = []

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=claude_messages,
            temperature=0.3,
        )

        # handle tool-use loop (may need multiple rounds)
        max_tool_rounds = 3
        nav_report_data = None
        for _ in range(max_tool_rounds):
            # check if Claude wants to use tools
            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if not tool_use_blocks:
                break

            # add assistant response (with tool_use blocks) to messages
            claude_messages.append({"role": "assistant", "content": response.content})

            # execute all tool calls and build tool_result messages
            tool_results = []
            for block in tool_use_blocks:
                result = await _execute_tool(block.name, block.input, company=company, session_id=session_id)

                # track blockchain data for response
                if block.name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                # track knowledge sources
                if block.name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append(
                            {
                                "title": r.get("title", ""),
                                "score": r.get("score", 0),
                            }
                        )

                # capture nav report data for later reconstruction
                if (
                    block.name == "get_dashboard_navigation"
                    and isinstance(result, dict)
                    and result.get("found")
                ):
                    company_id_val = company.get("id", "")
                    nav_report_data = await stroll_index_service.generate_navigation_report(
                        company_id_val
                    )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )

            # add tool results and call Claude again
            claude_messages.append({"role": "user", "content": tool_results})

            response = await client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOLS,
                messages=claude_messages,
                temperature=0.3,
            )

        # extract final text response
        reply = ""
        for block in response.content:
            if hasattr(block, "text"):
                reply += block.text

        if not reply:
            reply = "I apologize, but I wasn't able to generate a response. Could you please rephrase your question?"

    except Exception as e:
        logger.error(f"Anthropic API error: {e}")
        raise e

    # extract navigation_steps from reply and reconstruct guide
    guide_dump = None
    if "nav_report_data" in locals() and nav_report_data:
        nav_steps, reply = extract_navigation_steps(reply)
        if nav_steps:
            guide = reconstruct_navigation_guide(nav_steps, nav_report_data["page_lookup"])
            if guide:
                guide_dump = guide.model_dump()

    # save conversation
    history.append(
        {
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )

    assistant_msg = {
        "role": "assistant",
        "content": reply,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    if guide_dump:
        assistant_msg["navigation_guide"] = guide_dump
    history.append(assistant_msg)

    await _save_conversation(company_id, session_id, history)

    working_mem.context_window = history[-10:]
    await memory_service.save_working_memory(working_mem)

    return {
        "reply": reply,
        "sources": sources,
        "blockchain_data": blockchain_data,
        "navigation_guide": guide_dump,
    }


# streaming chat (SSE)


async def chat_stream(company_id: str, session_id: str, user_message: str, user_id: str = None, page_url: str = None, attachments: list = None):
    """
    Async generator that streams the agent chat flow as events.

    Yields dicts with a "type" key:
        thinking  – status update for the frontend loader
        tool      – a tool is being invoked (name + friendly label)
        text      – final agent reply text
        sources   – knowledge-base sources & blockchain data (if any)
        error     – friendly error message
        done      – stream complete
    """
    # load company info
    company = await db.companies.find_one({"id": company_id})
    if not company:
        yield {"type": "error", "message": "Company not found. Please contact support."}
        yield {"type": "done"}
        return

    yield {"type": "thinking", "message": "Reading your message…"}

    # load conversation history
    history = await _load_conversation(company_id, session_id)

    memory_context = await memory_service.load_memory_context(
        company_id=company_id,
        user_id=user_id,
        session_id=session_id,
    )
    memory_str = memory_service.format_memory_context(memory_context)

    working_mem = memory_context.working_memory
    if not working_mem:
        working_mem = WorkingMemory(session_id=session_id)
        if user_id:
            working_mem.identified_user = True

    # build system prompt and messages
    integration_count = await integration_service.get_active_integration_count(company_id)
    system_prompt = _build_system_prompt(company, memory_str, page_url, integration_count)

    claude_messages = []
    for msg in history[-10:]:
        role = "user" if msg["role"] == "user" else "assistant"
        # Rebuild multimodal content from persisted attachments if present
        msg_attachments = msg.get("attachments", [])
        if role == "user" and msg_attachments:
            att_blocks = format_for_anthropic(msg_attachments)
            content = [{"type": "text", "text": msg["content"]}] + att_blocks
        else:
            content = msg["content"]
        claude_messages.append({"role": role, "content": content})

    # Process current message attachments
    enriched_attachments = []
    if attachments:
        enriched_attachments = await extract_attachment_content(attachments)
        att_blocks = format_for_anthropic(enriched_attachments)
        user_content = [{"type": "text", "text": user_message}] + att_blocks
    else:
        user_content = user_message
    claude_messages.append({"role": "user", "content": user_content})

    client = _get_client()
    blockchain_data = None
    sources = []

    try:
        yield {"type": "thinking", "message": "Thinking…"}

        response = await client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=claude_messages,
            temperature=0.3,
        )

        # tool-use loop
        max_tool_rounds = 3
        nav_report_data = None  # store report data for reconstruction

        for _ in range(max_tool_rounds):
            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if not tool_use_blocks:
                break

            claude_messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                # emit multi-stage stream for this tool
                tool_name = block.name
                stages = _get_tool_stages(tool_name)
                
                # Emit all stages for this tool
                for stage_message in stages:
                    yield {"type": "thinking", "message": stage_message}
                
                # Emit tool metadata event
                label = TOOL_STAGE_LABELS.get(tool_name, "Working…")
                yield {"type": "tool", "name": tool_name, "label": label}

                result = await _execute_tool(tool_name, block.input, company=company, session_id=session_id)
                
                if tool_name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                if tool_name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append(
                            {
                                "title": r.get("title", ""),
                                "score": r.get("score", 0),
                            }
                        )

                # capture nav report data for later reconstruction
                if (
                    tool_name == "get_dashboard_navigation"
                    and isinstance(result, dict)
                    and result.get("found")
                ):
                    company_id_val = company.get("id", "")
                    nav_report_data = await stroll_index_service.generate_navigation_report(
                        company_id_val
                    )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )

            claude_messages.append({"role": "user", "content": tool_results})

            yield {"type": "thinking", "message": "Preparing response…"}

            response = await client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOLS,
                messages=claude_messages,
                temperature=0.3,
            )

        # extract final text
        reply = ""
        for block in response.content:
            if hasattr(block, "text"):
                reply += block.text

        if not reply:
            reply = "I apologize, but I wasn't able to generate a response. Could you please rephrase your question?"

        # extract navigation_steps from reply and reconstruct guide
        if nav_report_data:
            nav_steps, reply = extract_navigation_steps(reply)
            if nav_steps:
                guide = reconstruct_navigation_guide(nav_steps, nav_report_data["page_lookup"])
                if guide:
                    yield {"type": "navigation_guide", "guide": guide.model_dump()}

    except Exception as e:
        logger.error(f"Anthropic API error during streaming chat: {e}")
        raise e

    # save conversation
    user_msg_doc = {
        "role": "user",
        "content": user_message,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    if enriched_attachments:
        user_msg_doc["attachments"] = build_persistable_attachments(enriched_attachments)
    history.append(user_msg_doc)

    assistant_msg = {
        "role": "assistant",
        "content": reply,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    if "guide" in locals() and guide:
        assistant_msg["navigation_guide"] = guide.model_dump()
    history.append(assistant_msg)

    await _save_conversation(company_id, session_id, history)

    working_mem.context_window = history[-10:]
    await memory_service.save_working_memory(working_mem)

    # emit final events
    yield {"type": "text", "content": reply}

    if sources or blockchain_data:
        yield {"type": "sources", "sources": sources, "blockchain_data": blockchain_data}

    yield {"type": "done"}
