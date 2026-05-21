"""
AI Agent Service — OpenRouter integration with tool use for crypto support.

Uses the OpenAI-compatible SDK pointed at https://openrouter.ai/api/v1 to access
any model available on OpenRouter (Claude, GPT, Gemini, Llama, etc.).

Conversation history is stored in MongoDB.
Same tool set and SSE streaming contract as anthropic_agent_service.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.database import db
from app.core.validators import validate_email
from app.core.utils import get_random_avatar
from app.services import (
    knowledge_service,
    chain_service,
    stroll_index_service,
    company_email_service,
    page_reader_service,
)
from app.services.stroll_index_service import extract_navigation_steps, reconstruct_navigation_guide
from app.services.blockchain import detect, evm, bitcoin, prices
from app.services import memory_service
from app.models.memory_models import WorkingMemory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client (lazy singleton)
# ---------------------------------------------------------------------------

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": settings.FRONTEND_URL,
                "X-OpenRouter-Title": "SwiftAgent",
            },
        )
    return _client


MODEL = settings.OPENROUTER_MODEL

# Tool definitions (OpenAI function-calling format)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search the company's knowledge base and documentation for relevant information. "
                "Use this when a customer asks a question that might be answered by company "
                "documentation, FAQs, policies, or guides. Do NOT use this for simple greetings, "
                "small talk, or follow-up questions where you already have the context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to find relevant documents.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_transaction",
            "description": (
                "Look up a blockchain transaction by its hash. Use this when a customer "
                "pastes a transaction hash (tx hash). Returns transaction details including "
                "status, gas used, value, confirmations, and more."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {"type": "string", "description": "The transaction hash to look up"},
                    "chain": {
                        "type": "string",
                        "description": (
                            "Optional blockchain chain name: ethereum, bsc, polygon, "
                            "arbitrum, base, avalanche, or bitcoin."
                        ),
                    },
                },
                "required": ["hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_wallet",
            "description": (
                "Look up a blockchain wallet/address. Returns balance, transaction count, "
                "recent activity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "The wallet address to look up"},
                    "chain": {
                        "type": "string",
                        "description": "Optional blockchain chain name.",
                    },
                },
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_problem",
            "description": (
                "Diagnose issues with a transaction. Use AFTER looking up a transaction to "
                "analyze what went wrong. Checks for common failure modes like low gas, "
                "reverted contracts, missing deposits, suspicious activity, etc."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_dashboard_navigation",
            "description": (
                "Access the visual dashboard navigation system. Returns a report containing "
                "page maps, interactive elements, AND ACTUAL SCREENSHOTS. Use this tool "
                "whenever a user asks to 'see' something, asks for 'screenshots', or asks "
                "'how to' do anything in the dashboard."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The feature, page, or setting the user is looking for",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_dashboard_documentation",
            "description": (
                "Retrieve the complete documentation for the entire dashboard. Use when the "
                "customer asks for 'all documentation', 'the full manual', or 'everything about "
                "the dashboard'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scrape_documentation_link",
            "description": (
                "Scrape an external documentation link and ingest it into the company's knowledge "
                "base. Use when a user provides a URL and asks you to 'read', 'learn', 'scrape', "
                "or 'ingest' it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL of the documentation to scrape"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_support_ticket",
            "description": (
                "Create a support ticket when you cannot resolve the customer's issue. "
                "You MUST ask for the customer's email and get confirmation before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_email": {"type": "string", "description": "Customer's email address"},
                    "customer_name": {"type": "string", "description": "Customer's name, if provided"},
                    "subject": {"type": "string", "description": "Brief subject line (max 100 chars)"},
                    "summary": {
                        "type": "string",
                        "description": "Detailed summary of the issue for the support team",
                    },
                },
                "required": ["customer_email", "subject", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_website_page",
            "description": (
                "Read the contents of a specific website URL. "
                "Returns the visible text on the page and a list of links found on that page. "
                "Use this to answer questions by reading the user's current page, or to follow "
                "links to find more information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL of the page to read"}
                },
                "required": ["url"],
            },
        },
    },
]

# Tool stage labels for the SSE stream

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
}

# Helper function to emit multi-stage streams for tools
def _get_tool_stages(tool_name: str) -> list[str]:
    """Get the multi-stage stream text for a given tool."""
    return TOOL_STAGES.get(tool_name, [f"Processing {tool_name}..."])


# System prompt


def _build_system_prompt(company: dict, memory_context: str = "", page_url: str = "") -> str:
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
2. Answer questions based on the company's documentation. Use search_knowledge_base when relevant.
3. For crypto companies: when a customer provides a tx hash or wallet address, use tools to look \
up real on-chain data and diagnose issues.
4. ALWAYS explain things in plain language. Assume the customer may not be technical.
5. When you find an issue, explain WHAT happened, WHY it happened, and WHAT TO DO about it.
6. If you don't know or can't find information, say so honestly. Never fabricate data.
7. Present monetary values with USD equivalents when possible.
8. For security-related issues (mixer interactions, unlimited approvals, drainer patterns), \
flag them clearly with appropriate urgency.

TOOL USAGE:
- Use search_knowledge_base when the customer asks about company policies, features, pricing, \
FAQs, how-to guides, or anything that might be in the company documentation.
- COMPULSORY: Use get_dashboard_navigation for ANY "how to", "where is X?", "show me X", \
or navigation question in the dashboard.
- Use get_full_dashboard_documentation when the customer asks for the full manual.
- Use scrape_documentation_link when the customer gives you a URL to learn from.
- Use lookup_transaction for transaction hashes; lookup_wallet for wallet addresses.
- After getting transaction data, use diagnose_problem to analyze what went wrong.
- Do NOT use any tools for simple greetings or small talk.

NAVIGATION GUIDE FORMAT:
When you receive a navigation report from get_dashboard_navigation, respond with:
1. A ```navigation_steps fenced code block containing a JSON array of steps:
   [{{"page_id": "<id>", "instruction": "<step>", "element_selector": "<css selector>"}}]
2. A brief conversational summary after the JSON block.

RESPONSE FORMAT:
- Match response length to question complexity.
- For simple greetings, respond briefly (1-2 sentences).
- Use structured responses only for complex/technical questions.

TICKET ESCALATION:
- Offer a support ticket only after trying available tools and failing to resolve the issue.
- Always ask for email and confirmation before creating a ticket.
"""

# Tool executor (shared logic, identical to anthropic service)
async def _execute_tool(name: str, args: dict, company: dict = None, session_id: str | None = None) -> dict:
    """Execute a named tool and return its result dict."""
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
                if chain != "bitcoin" and len(detected.get("possible_chains", [])) > 1:
                    probed = await evm.probe_transaction_chain(tx_hash)
                    if probed:
                        chain = probed
            result = await (bitcoin.get_transaction(tx_hash) if chain == "bitcoin" else evm.get_transaction(chain, tx_hash))
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
            result = await (bitcoin.get_wallet(address) if chain == "bitcoin" else evm.get_wallet(chain, address))
            if result and "error" not in result:
                price_data = await prices.get_price(chain)
                if "price_usd" in price_data:
                    native_bal = result.get("balance_native") or result.get("balance_btc", 0)
                    result["balance_usd"] = prices.convert_to_usd(native_bal, price_data["price_usd"])
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
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            report_data = await stroll_index_service.generate_navigation_report(company_id)
            if report_data:
                return {"found": True, "navigation_report": report_data["report"]}
            return {"found": False, "message": "No dashboard navigation data available."}

        elif name == "get_full_dashboard_documentation":
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            full_docs = await stroll_index_service.get_all_navigation_steps(company_id)
            if full_docs and full_docs.steps:
                return {
                    "found": True,
                    "message": "Please output the following navigation_steps JSON block.",
                    "navigation_steps": [
                        {
                            "page_id": step.page_title,
                            "instruction": step.instruction,
                            "element_selector": step.highlight.selector if step.highlight else None,
                        }
                        for step in full_docs.steps
                    ],
                }
            return {"found": False, "message": "No dashboard documentation available."}

        elif name == "scrape_documentation_link":
            url = args.get("url", "")
            if not company:
                return {"error": "Company context not available"}
            company_id = company.get("id", "")
            user_id = company.get("user_id", "")
            from app.services.documentation_scraper_service import scrape_and_ingest_docs
            success = await scrape_and_ingest_docs(url, company_id, user_id)
            if success:
                return {"success": True, "message": f"Successfully scraped and ingested docs from {url}."}
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
            if not company.get("email_slug"):
                return {"error": "Email ticketing is not configured for this company."}
            try:
                ticket = await company_email_service.create_ticket(
                    company_id=company_id,
                    customer_email=customer_email,
                    subject=subject,
                    chat_summary=summary,
                    chat_session_id=args.get("_session_id"),
                    customer_name=customer_name,
                )
                return {
                    "success": True,
                    "ticket_id": ticket["id"],
                    "message": f"Ticket #{ticket['id']} created. The team will follow up at {customer_email}.",
                }
            except Exception:
                logger.exception("Failed to create support ticket")
                return {"error": "Failed to create ticket"}

        elif name == "read_website_page":
            url = args.get("url", "")
            if not url:
                return {"error": "No URL provided to read."}
            result = await page_reader_service.read_website_page(url)
            return result

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception:
        logger.exception(f"Tool execution error: {name}")
        return {"error": f"Tool '{name}' failed unexpectedly"}


# Conversation persistence
async def _load_conversation(company_id: str, session_id: str) -> list[dict]:
    convo = await db.widget_conversations.find_one(
        {"company_id": company_id, "session_id": session_id}
    )
    return convo.get("messages", []) if convo else []


async def _save_conversation(company_id: str, session_id: str, messages: list[dict]):
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


# History helpers
def _history_to_openai(history: list[dict], max_msgs: int = 10) -> list[dict]:
    """Convert stored history to OpenAI messages format."""
    msgs = []
    for msg in history[-max_msgs:]:
        role = "user" if msg["role"] == "user" else "assistant"
        msgs.append({"role": role, "content": msg["content"]})
    return msgs


# Non-streaming chat
async def chat(company_id: str, session_id: str, user_message: str, user_id: str = None, page_url: str = None) -> dict:
    """Process a chat message and return a complete response dict."""
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return {"reply": "Sorry, company not found.", "sources": [], "blockchain_data": None}

    history = await _load_conversation(company_id, session_id)
    memory_context = await memory_service.load_memory_context(
        company_id=company_id, user_id=user_id, session_id=session_id
    )
    memory_str = memory_service.format_memory_context(memory_context)

    working_mem = memory_context.working_memory
    if not working_mem:
        working_mem = WorkingMemory(session_id=session_id)
        if user_id:
            working_mem.identified_user = True

    system_prompt = _build_system_prompt(company, memory_str, page_url)
    messages = [{"role": "system", "content": system_prompt}]
    messages += _history_to_openai(history)
    messages.append({"role": "user", "content": user_message})

    client = _get_client()
    blockchain_data = None
    sources = []
    guide_dump = None

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
            temperature=0.3,
        )

        nav_report_data = None
        max_tool_rounds = 3

        for _ in range(max_tool_rounds):
            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                break

            # append assistant message with tool_calls
            messages.append(msg)

            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                result = await _execute_tool(fn_name, args, company=company, session_id=session_id)

                if fn_name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result
                if fn_name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append({"title": r.get("title", ""), "score": r.get("score", 0)})
                if fn_name == "get_dashboard_navigation" and isinstance(result, dict) and result.get("found"):
                    nav_report_data = await stroll_index_service.generate_navigation_report(company.get("id", ""))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

            response = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=4096,
                temperature=0.3,
            )

        reply = response.choices[0].message.content or ""
        if not reply:
            reply = "I apologize, but I wasn't able to generate a response. Could you please rephrase?"

        if nav_report_data:
            nav_steps, reply = extract_navigation_steps(reply)
            if nav_steps:
                guide = reconstruct_navigation_guide(nav_steps, nav_report_data["page_lookup"])
                if guide:
                    guide_dump = guide.model_dump()

    except Exception as e:
        logger.error(f"OpenRouter API error: {e}")
        reply = "I'm sorry, I'm experiencing a temporary issue. Please try again in a moment."

    # persist conversation
    history.append({"role": "user", "content": user_message, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    assistant_msg = {"role": "assistant", "content": reply, "timestamp": datetime.now(tz=timezone.utc).isoformat()}
    if guide_dump:
        assistant_msg["navigation_guide"] = guide_dump
    history.append(assistant_msg)

    await _save_conversation(company_id, session_id, history)
    working_mem.context_window = history[-10:]
    await memory_service.save_working_memory(working_mem)

    return {"reply": reply, "sources": sources, "blockchain_data": blockchain_data, "navigation_guide": guide_dump}


# Streaming chat (SSE)


async def chat_stream(company_id: str, session_id: str, user_message: str, user_id: str = None, page_url: str = None):
    """
    Async generator yielding dicts with a "type" key:
        thinking  – status update for the frontend loader
        tool      – a tool is being invoked
        text      – final agent reply text
        sources   – knowledge-base sources & blockchain data
        navigation_guide – reconstructed visual guide
        error     – friendly error message
        done      – stream complete
    """
    company = await db.companies.find_one({"id": company_id})
    if not company:
        yield {"type": "error", "message": "Company not found. Please contact support."}
        yield {"type": "done"}
        return

    yield {"type": "thinking", "message": "Reading your message…"}

    history = await _load_conversation(company_id, session_id)
    memory_context = await memory_service.load_memory_context(
        company_id=company_id, user_id=user_id, session_id=session_id
    )
    memory_str = memory_service.format_memory_context(memory_context)

    working_mem = memory_context.working_memory
    if not working_mem:
        working_mem = WorkingMemory(session_id=session_id)
        if user_id:
            working_mem.identified_user = True

    system_prompt = _build_system_prompt(company, memory_str, page_url)
    messages = [{"role": "system", "content": system_prompt}]
    messages += _history_to_openai(history)
    messages.append({"role": "user", "content": user_message})

    client = _get_client()
    blockchain_data = None
    sources = []
    reply = ""
    guide = None

    try:
        yield {"type": "thinking", "message": "Thinking…"}

        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
            temperature=0.3,
        )

        nav_report_data = None
        max_tool_rounds = 3

        for _ in range(max_tool_rounds):
            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                break

            messages.append(msg)

            for tc in tool_calls:
                fn_name = tc.function.name
                stages = _get_tool_stages(fn_name)
                
                # Emit all stages for this tool
                for stage_message in stages:
                    yield {"type": "thinking", "message": stage_message}
                
                # Emit tool metadata event
                label = TOOL_STAGE_LABELS.get(fn_name, "Working…")
                yield {"type": "tool", "name": fn_name, "label": label}

                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                result = await _execute_tool(fn_name, args, company=company, session_id=session_id)
                
                if fn_name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result
                if fn_name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append({"title": r.get("title", ""), "score": r.get("score", 0)})
                if fn_name == "get_dashboard_navigation" and isinstance(result, dict) and result.get("found"):
                    nav_report_data = await stroll_index_service.generate_navigation_report(company.get("id", ""))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

            yield {"type": "thinking", "message": "Preparing response…"}

            response = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=4096,
                temperature=0.3,
            )

        reply = response.choices[0].message.content or ""
        if not reply:
            reply = "I apologize, but I wasn't able to generate a response. Could you please rephrase?"

        if nav_report_data:
            nav_steps, reply = extract_navigation_steps(reply)
            if nav_steps:
                guide = reconstruct_navigation_guide(nav_steps, nav_report_data["page_lookup"])
                if guide:
                    yield {"type": "navigation_guide", "guide": guide.model_dump()}

    except Exception as e:
        logger.error(f"OpenRouter API error during streaming chat: {e}")
        reply = "I'm sorry, I'm experiencing a temporary issue. Please try again in a moment."

    # persist conversation
    history.append({"role": "user", "content": user_message, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    assistant_msg = {"role": "assistant", "content": reply, "timestamp": datetime.now(tz=timezone.utc).isoformat()}
    if guide:
        assistant_msg["navigation_guide"] = guide.model_dump()
    history.append(assistant_msg)

    await _save_conversation(company_id, session_id, history)
    working_mem.context_window = history[-10:]
    await memory_service.save_working_memory(working_mem)

    yield {"type": "text", "content": reply}
    if sources or blockchain_data:
        yield {"type": "sources", "sources": sources, "blockchain_data": blockchain_data}
    yield {"type": "done"}
