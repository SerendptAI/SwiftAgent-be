"""
AI Agent Service — Gemini integration with function calling for crypto support.

Uses Google Gemini with native tool use to:
1. Search company knowledge base (RAG) for document-based answers
2. Look up blockchain transactions and wallets on-chain
3. Diagnose common crypto transaction issues

Conversation history is stored in MongoDB.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from google import genai
from google.genai import types

from app.core.config import settings
from app.core.database import db
from app.services import knowledge_service, chain_service, stroll_index_service, memory_service
from app.services.stroll_index_service import extract_navigation_steps, reconstruct_navigation_guide
from app.services.blockchain import detect, evm, bitcoin, prices
from app.models.memory_models import WorkingMemory

logger = logging.getLogger(__name__)

# gemini client
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_knowledge_base",
                description=(
                    "Search the company's knowledge base and documentation for relevant information. "
                    "Use this when a customer asks a question that might be answered by company "
                    "documentation, FAQs, policies, or guides. Do NOT use this for simple greetings, "
                    "small talk, or follow-up questions where you already have the context."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "query": types.Schema(
                            type="STRING",
                            description="The search query to find relevant documents. Use the customer's question or a refined version of it.",
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="lookup_transaction",
                description=(
                    "Look up a blockchain transaction by its hash. Use this when a customer "
                    "pastes a transaction hash (tx hash). Returns transaction details including "
                    "status, gas used, value, confirmations, and more."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "hash": types.Schema(
                            type="STRING",
                            description="The transaction hash to look up",
                        ),
                        "chain": types.Schema(
                            type="STRING",
                            description=(
                                "Optional blockchain chain name: ethereum, bsc, polygon, "
                                "arbitrum, base, avalanche, or bitcoin. If not provided, "
                                "the chain will be auto-detected."
                            ),
                        ),
                    },
                    required=["hash"],
                ),
            ),
            types.FunctionDeclaration(
                name="lookup_wallet",
                description=(
                    "Look up a blockchain wallet/address. Use this when a customer pastes "
                    "a wallet address. Returns balance, transaction count, recent activity."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "address": types.Schema(
                            type="STRING",
                            description="The wallet address to look up",
                        ),
                        "chain": types.Schema(
                            type="STRING",
                            description=(
                                "Optional blockchain chain name: ethereum, bsc, polygon, "
                                "arbitrum, base, avalanche, or bitcoin. If not provided, "
                                "the chain will be auto-detected."
                            ),
                        ),
                    },
                    required=["address"],
                ),
            ),
            types.FunctionDeclaration(
                name="diagnose_problem",
                description=(
                    "Diagnose issues with a transaction. Use this AFTER looking up a "
                    "transaction to analyze what went wrong. Checks for common failure modes "
                    "like low gas, reverted contracts, missing deposits, suspicious activity, etc."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "tx_data": types.Schema(
                            type="STRING",
                            description="JSON string of the transaction data from lookup_transaction",
                        ),
                        "customer_complaint": types.Schema(
                            type="STRING",
                            description="Description of the customer's issue in plain text",
                        ),
                    },
                    required=["tx_data", "customer_complaint"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_dashboard_navigation",
                description=(
                    "Get the full navigation report of the customer's dashboard. "
                    "Returns a detailed map of all pages, their interactive elements, screenshots, "
                    "and how they connect. Use this when a customer asks how to find something, "
                    "where something is, or how to navigate to a specific page or setting in the dashboard. "
                    "After reading the report, you MUST respond with a ```navigation_steps JSON block "
                    "listing the ordered steps."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "query": types.Schema(
                            type="STRING",
                            description="The feature, page, or setting the user is looking for",
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_full_dashboard_documentation",
                description=(
                    "Retrieve the complete documentation for the entire dashboard. Use this tool "
                    "when the customer asks for 'all documentation', 'the full manual', 'everything about the dashboard', "
                    "or wants a complete overview of all features. "
                    "After calling this tool, you MUST respond with the returned navigation_steps JSON block exactly as provided, "
                    "which the frontend will use to render the complete visual guide."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={},
                ),
            ),
            types.FunctionDeclaration(
                name="scrape_documentation_link",
                description=(
                    "Scrape an external documentation link and ingest it into the company's knowledge base. "
                    "Use this tool when a user provides a URL to documentation (like Notion, Gitbook, etc.) "
                    "and asks you to 'read', 'learn', 'scrape', or 'ingest' it. "
                    "After it succeeds, you can use search_knowledge_base to answer questions about it."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "url": types.Schema(
                            type="STRING",
                            description="The URL of the documentation to scrape",
                        ),
                    },
                    required=["url"],
                ),
            ),
        ]
    )
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
}


# system prompt builder


def _build_system_prompt(company: dict, memory_context: str = "") -> str:
    company_name = company.get("name", "the company")
    brand_tone = company.get("brand_tone", "professional and helpful")
    description = company.get("description", "")
    company_type = company.get("company_type", "")

    memory_section = ""
    if memory_context:
        memory_section = f"""
MEMORY CONTEXT:
{memory_context}
"""

    return f"""You are a senior customer support agent for {company_name}. \
You respond with expertise, empathy, and clarity.

COMPANY CONTEXT:
- Name: {company_name}
- Type: {company_type}
- Description: {description}
- Tone: {brand_tone}{memory_section}

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
- Use get_dashboard_navigation when the customer asks "where is X?", "how do I find X?", \
"how do I navigate to X?", or similar navigation questions about the dashboard.
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

NAVIGATION GUIDE FORMAT:
When you receive a navigation report from the get_dashboard_navigation tool, identify the \
correct sequence of pages the user needs to visit. Then respond with:
1. A ```navigation_steps fenced code block containing a JSON array of steps in order:
   [{"page_id": "<id>", "instruction": "<natural language step>", "element_selector": "<css selector of element to click>"}]
   - page_id MUST match a page ID from the report
   - element_selector should be the selector of the element to click on that page (for highlighting)
   - Only include pages that are part of the path, in order
2. After the JSON block, write a brief conversational summary of the steps.

If you used the get_full_dashboard_documentation tool, the tool will return a pre-formatted \
`navigation_steps` JSON block. You MUST output this EXACT JSON block inside a ```navigation_steps \
fenced block without modifying it.

Only reference pages and elements that exist in the navigation report. Never invent pages or UI elements.

RESPONSE FORMAT:
- Match your response length to the complexity of the question.
- For simple greetings or casual messages, respond briefly and naturally (1-2 sentences).
- Only use structured responses (sections, bullet points) for complex or technical questions.
- Keep answers concise and to the point. Avoid unnecessary preamble or filler.
"""


# tool execution


async def _execute_tool(name: str, args: dict, company: dict = None) -> dict:
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
                            "page_id": step.page_title, 
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
            },
        },
        upsert=True,
    )


# main chat function


async def chat(company_id: str, session_id: str, user_message: str, user_id: str = None) -> dict:
    """
    Process a chat message from the widget.

    Flow:
    1. Load conversation history
    2. Load working memory context (if user_id provided)
    3. Build messages array with system prompt + history + new message
    4. Send to Gemini with tools (including search_knowledge_base)
    5. If tool call → execute, feed result back → get final response
    6. Save conversation, save working memory, extract facts
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

    # load memory context
    memory_str = ""
    if user_id:
        memory_data = await memory_service.load_memory(user_id)
        memory_str = memory_data.get("memory_context", "") if memory_data else ""

    # build system prompt and messages
    system_prompt = _build_system_prompt(company, memory_str)

    # Convert history to Gemini format
    gemini_history = []
    for msg in history[-10:]:  # Keep last 10 messages for context
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
        )

    # Add new user message
    gemini_history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    # call gemini
    client = _get_client()
    blockchain_data = None
    sources = []

    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=gemini_history,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=TOOLS,
                temperature=0.3,
            ),
        )

        # Handle tool calls (may need multiple rounds)
        max_tool_rounds = 3
        for _ in range(max_tool_rounds):
            # Check if there are function calls in the response
            function_calls = []
            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)

            if not function_calls:
                break

            # Execute all tool calls
            tool_results = []
            for fc in function_calls:
                args = dict(fc.args) if fc.args else {}
                result = await _execute_tool(fc.name, args, company=company)

                # Track blockchain data for response
                if fc.name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                # Track knowledge sources
                if fc.name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append(
                            {
                                "title": r.get("title", ""),
                                "score": r.get("score", 0),
                            }
                        )

                tool_results.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result,
                    )
                )

            # Add the model's tool call and tool results to history
            gemini_history.append(response.candidates[0].content)
            gemini_history.append(types.Content(role="user", parts=tool_results))

            # Call Gemini again with tool results
            response = client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=TOOLS,
                    temperature=0.3,
                ),
            )

        # Extract final text response
        reply = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    reply += part.text

        if not reply:
            reply = "I apologize, but I wasn't able to generate a response. Could you please rephrase your question?"

    except Exception as e:
        logger.exception("Gemini API error")
        reply = (
            "I'm sorry, I'm experiencing a temporary issue. "
            "Please try again in a moment, or contact our support team directly."
        )

    # save conversation
    history.append(
        {
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    history.append(
        {
            "role": "assistant",
            "content": reply,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    await _save_conversation(company_id, session_id, history)

    # save working memory and extract facts
    if user_id:
        working_memory = WorkingMemory(
            user_id=user_id,
            company_id=company_id,
            session_id=session_id,
            messages=history[-4:],
        )
        await memory_service.save_working_memory(working_memory)
        extracted_facts = await memory_service.extract_facts(user_message, reply)
        if extracted_facts:
            await memory_service.add_facts(user_id, extracted_facts)

    return {
        "reply": reply,
        "sources": sources,
        "blockchain_data": blockchain_data,
    }


# streaming chat (SSE)


async def chat_stream(company_id: str, session_id: str, user_message: str, user_id: str = None):
    """
    Async generator that streams the Gemini agent chat flow as events.

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

    # load memory context
    memory_str = ""
    if user_id:
        memory_data = await memory_service.load_memory(user_id)
        memory_str = memory_data.get("memory_context", "") if memory_data else ""

    # build system prompt and messages
    system_prompt = _build_system_prompt(company, memory_str)

    # Convert history to Gemini format
    gemini_history = []
    for msg in history[-10:]:
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
        )

    # Add new user message
    gemini_history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    client = _get_client()
    blockchain_data = None
    sources = []

    try:
        yield {"type": "thinking", "message": "Thinking…"}

        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=gemini_history,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=TOOLS,
                temperature=0.3,
            ),
        )

        # Handle tool calls (may need multiple rounds)
        max_tool_rounds = 3
        nav_report_data = None  # store report data for reconstruction

        for _ in range(max_tool_rounds):
            function_calls = []
            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)

            if not function_calls:
                break

            # Execute all tool calls
            tool_results = []
            for fc in function_calls:
                # emit tool event with friendly label
                label = TOOL_STAGE_LABELS.get(fc.name, "Working…")
                yield {"type": "thinking", "message": label}
                yield {"type": "tool", "name": fc.name, "label": label}

                args = dict(fc.args) if fc.args else {}
                result = await _execute_tool(fc.name, args, company=company)

                if fc.name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                if fc.name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append(
                            {
                                "title": r.get("title", ""),
                                "score": r.get("score", 0),
                            }
                        )

                # capture nav report data for later reconstruction
                if (
                    fc.name == "get_dashboard_navigation"
                    and isinstance(result, dict)
                    and result.get("found")
                ):
                    company_id_val = company.get("id", "")
                    nav_report_data = await stroll_index_service.generate_navigation_report(
                        company_id_val
                    )

                tool_results.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result,
                    )
                )

            # Add the model's tool call and tool results to history
            gemini_history.append(response.candidates[0].content)
            gemini_history.append(types.Content(role="user", parts=tool_results))

            yield {"type": "thinking", "message": "Preparing response…"}

            # Call Gemini again with tool results
            response = client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=TOOLS,
                    temperature=0.3,
                ),
            )

        # Extract final text response
        reply = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    reply += part.text

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
        logger.exception("Gemini API error during streaming chat")
        reply = (
            "I'm sorry, I'm experiencing a temporary issue. "
            "Please try again in a moment, or contact our support team directly."
        )

    # save conversation
    history.append(
        {
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    history.append(
        {
            "role": "assistant",
            "content": reply,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    await _save_conversation(company_id, session_id, history)

    # save working memory and extract facts
    if user_id:
        working_memory = WorkingMemory(
            user_id=user_id,
            company_id=company_id,
            session_id=session_id,
            messages=history[-4:],
        )
        await memory_service.save_working_memory(working_memory)
        extracted_facts = await memory_service.extract_facts(user_message, reply)
        if extracted_facts:
            await memory_service.add_facts(user_id, extracted_facts)

    # emit final events
    yield {"type": "text", "content": reply}

    if sources or blockchain_data:
        yield {"type": "sources", "sources": sources, "blockchain_data": blockchain_data}

    yield {"type": "done"}
