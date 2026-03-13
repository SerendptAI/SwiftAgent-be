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
from app.services import knowledge_service, chain_service
from app.services.blockchain import detect, evm, bitcoin, prices

logger = logging.getLogger(__name__)

# anthropic client
_client = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


MODEL = "claude-sonnet-4-20250514"


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
]


# system prompt builder

def _build_system_prompt(company: dict) -> str:
    company_name = company.get("name", "the company")
    brand_tone = company.get("brand_tone", "professional and helpful")
    description = company.get("description", "")
    company_type = company.get("company_type", "")

    return f"""You are a senior customer support agent for {company_name}. \
You respond with expertise, empathy, and clarity.

COMPANY CONTEXT:
- Name: {company_name}
- Type: {company_type}
- Description: {description}
- Tone: {brand_tone}

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
- When you see a string that looks like a transaction hash (0x... followed by 64 hex chars, \
or 64 hex chars without 0x for Bitcoin), use lookup_transaction.
- When you see a wallet address (0x... followed by 40 hex chars, or a Bitcoin address), \
use lookup_wallet.
- After getting transaction data, if the customer has a problem, use diagnose_problem \
to run the data through the diagnosis engine.
- You can chain tools: first lookup, then diagnose.
- Do NOT use any tools for simple greetings or small talk.

RESPONSE FORMAT:
- Match your response length to the complexity of the question.
- For simple greetings or casual messages, respond briefly and naturally (1-2 sentences).
- Only use structured responses (sections, bullet points) for complex or technical questions.
- Keep answers concise and to the point. Avoid unnecessary preamble or filler.
"""


# tool execution (shared with gemini service)

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


# conversation management

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

async def chat(company_id: str, session_id: str, user_message: str) -> dict:
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

    # build system prompt and messages
    system_prompt = _build_system_prompt(company)

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
        for _ in range(max_tool_rounds):
            # check if Claude wants to use tools
            tool_use_blocks = [
                block for block in response.content
                if block.type == "tool_use"
            ]

            if not tool_use_blocks:
                break

            # add assistant response (with tool_use blocks) to messages
            claude_messages.append({"role": "assistant", "content": response.content})

            # execute all tool calls and build tool_result messages
            tool_results = []
            for block in tool_use_blocks:
                result = await _execute_tool(block.name, block.input, company=company)

                # track blockchain data for response
                if block.name in ("lookup_transaction", "lookup_wallet", "diagnose_problem"):
                    blockchain_data = result

                # track knowledge sources
                if block.name == "search_knowledge_base" and isinstance(result, dict):
                    for r in result.get("results", []):
                        sources.append({
                            "title": r.get("title", ""),
                            "score": r.get("score", 0),
                        })

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

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
        logger.exception("Anthropic API error")
        reply = (
            "I'm sorry, I'm experiencing a temporary issue. "
            "Please try again in a moment, or contact our support team directly."
        )

    # save conversation
    history.append({"role": "user", "content": user_message, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    history.append({"role": "assistant", "content": reply, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    await _save_conversation(company_id, session_id, history)

    return {
        "reply": reply,
        "sources": sources,
        "blockchain_data": blockchain_data,
    }
