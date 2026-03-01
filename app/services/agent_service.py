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
from app.services import knowledge_service, chain_service
from app.services.blockchain import detect, evm, bitcoin, prices

logger = logging.getLogger(__name__)

# ── Gemini Client ─────────────────────────────────────────────────────
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# ── Tool Definitions (Gemini Function Declarations) ──────────────────

TOOLS = [
    types.Tool(function_declarations=[
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
    ])
]


# ── System Prompt Builder ─────────────────────────────────────────────

def _build_system_prompt(company: dict, knowledge_context: str = "") -> str:
    company_name = company.get("name", "the company")
    brand_tone = company.get("brand_tone", "professional and helpful")
    description = company.get("description", "")
    company_type = company.get("company_type", "")

    base_prompt = f"""You are a senior customer support agent for {company_name}. \
You respond with expertise, empathy, and clarity.

COMPANY CONTEXT:
- Name: {company_name}
- Type: {company_type}
- Description: {description}
- Tone: {brand_tone}

YOUR BEHAVIOR:
1. You are warm, professional, and knowledgeable.
2. You answer questions based on the company's documentation and knowledge base.
3. For crypto companies: when a customer provides a transaction hash or wallet address, \
use your tools to look up real on-chain data and diagnose issues.
4. ALWAYS explain things in plain language. Assume the customer may not be technical.
5. When you find an issue, explain WHAT happened, WHY it happened, and WHAT TO DO about it.
6. If you don't know or can't find information, say so honestly. Never fabricate data.
7. Present monetary values with USD equivalents when possible.
8. For security-related issues (mixer interactions, unlimited approvals, drainer patterns), \
flag them clearly with appropriate urgency.

TOOL USAGE:
- When you see a string that looks like a transaction hash (0x... followed by 64 hex chars, \
or 64 hex chars without 0x for Bitcoin), use lookup_transaction.
- When you see a wallet address (0x... followed by 40 hex chars, or a Bitcoin address), \
use lookup_wallet.
- After getting transaction data, if the customer has a problem, use diagnose_problem \
to run the data through the diagnosis engine.
- You can chain tools: first lookup, then diagnose.

RESPONSE FORMAT:
- Use clear, structured responses with sections when appropriate.
- Use bullet points for lists of issues or steps.
- Include relevant data points (confirmations, gas, fees) to support your explanations.
- End with a clear recommendation or next step.
"""

    if knowledge_context:
        base_prompt += f"""
COMPANY KNOWLEDGE BASE (use this to answer questions):
{knowledge_context}

When answering, prefer information from the knowledge base above. \
Cite specific documents or policies when relevant.
"""

    return base_prompt


# ── Tool Execution ────────────────────────────────────────────────────

async def _execute_tool(name: str, args: dict) -> dict:
    """Execute a tool call and return the result."""
    try:
        if name == "lookup_transaction":
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

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.exception(f"Tool execution error: {name}")
        return {"error": f"Tool '{name}' failed: {str(e)}"}


# ── Conversation Management ──────────────────────────────────────────

async def _load_conversation(company_id: str, session_id: str) -> list[dict]:
    """Load conversation history from MongoDB."""
    convo = await db.widget_conversations.find_one({
        "company_id": company_id,
        "session_id": session_id,
    })
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


# ── Main Chat Function ───────────────────────────────────────────────

async def chat(company_id: str, session_id: str, user_message: str) -> dict:
    """
    Process a chat message from the widget.

    Flow:
    1. Load conversation history
    2. Search company knowledge base (RAG)
    3. Build messages array with system prompt + history + new message
    4. Send to Gemini with tools
    5. If tool call → execute, feed result back → get final response
    6. Save conversation, return response
    """
    # 1. Load company info
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return {
            "reply": "Sorry, I couldn't find the company configuration. Please contact support.",
            "sources": [],
            "blockchain_data": None,
        }

    # 2. Load conversation history
    history = await _load_conversation(company_id, session_id)

    # 3. RAG — search knowledge base
    knowledge_context = ""
    sources = []
    try:
        # Use company's user_id for knowledge search
        user_id = company.get("user_id", "")
        if user_id:
            search_result = await knowledge_service.search_knowledge(
                user_id, user_message, limit=3, threshold=0.5
            )
            if search_result.get("results"):
                knowledge_pieces = []
                for r in search_result["results"]:
                    knowledge_pieces.append(f"[{r['title']}]: {r['content']}")
                    sources.append({
                        "title": r["title"],
                        "score": r["score"],
                    })
                knowledge_context = "\n\n---\n\n".join(knowledge_pieces)
    except Exception as e:
        logger.warning(f"Knowledge search failed: {e}")

    # 4. Build system prompt and messages
    system_prompt = _build_system_prompt(company, knowledge_context)

    # Convert history to Gemini format
    gemini_history = []
    for msg in history[-20:]:  # Keep last 20 messages for context
        role = "user" if msg["role"] == "user" else "model"
        gemini_history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])])
        )

    # Add new user message
    gemini_history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    # 5. Call Gemini
    client = _get_client()
    blockchain_data = None

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
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
                result = await _execute_tool(fc.name, args)

                # Track blockchain data for response
                if fc.name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                tool_results.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result,
                    )
                )

            # Add the model's tool call and tool results to history
            gemini_history.append(response.candidates[0].content)
            gemini_history.append(
                types.Content(role="user", parts=tool_results)
            )

            # Call Gemini again with tool results
            response = client.models.generate_content(
                model="gemini-2.0-flash",
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

    # 6. Save conversation
    history.append({"role": "user", "content": user_message, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    history.append({"role": "assistant", "content": reply, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    await _save_conversation(company_id, session_id, history)

    return {
        "reply": reply,
        "sources": sources,
        "blockchain_data": blockchain_data,
    }
