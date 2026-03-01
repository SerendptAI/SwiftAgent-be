"""
Problem diagnosis engine for crypto transactions.
Analyzes transaction data against known failure modes and returns
structured verdicts with severity, explanation, and recommended action.
"""
from app.services.blockchain import evm


# Known mixer/flagged contract addresses (Tornado Cash)
KNOWN_MIXER_ADDRESSES = {
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xdd4c48c0b24039969fc16d1cdf626eab821d3384",
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",
    "0xfd8610d20aa15b7b2e3be39b396a1bc3516c7144",
    "0xf60dd140cff0706bae9cd734ac3683696dceb001",
    "0xd96f2b1ef156653bce43c1134f22be2110e6380e",
}


async def diagnose_transaction(tx_data: dict, customer_complaint: str = "") -> dict:
    """
    Run a transaction through all known failure mode checks.
    Returns a structured diagnosis with issues found.
    """
    issues = []
    complaint_lower = customer_complaint.lower()

    status = tx_data.get("status", "unknown")
    chain = tx_data.get("chain", "unknown")

    # pending / stuck transactions
    if status == "pending":
        issues.append({
            "issue": "Transaction is pending",
            "severity": "warning",
            "explanation": (
                "This transaction has been broadcast to the network but has not "
                "been included in a block yet."
            ),
            "action": "Wait for miners/validators to pick it up. If stuck for too long, consider speeding up or cancelling.",
        })

        # Check gas price vs current network rate
        if chain != "bitcoin":
            current_gas = await evm.get_gas_price(chain)
            if current_gas:
                tx_gas = tx_data.get("gas_price_gwei", 0)
                network_gas = current_gas.get("gas_price_gwei", 0)
                if tx_gas > 0 and network_gas > 0 and tx_gas < network_gas * 0.7:
                    issues.append({
                        "issue": "Low gas price",
                        "severity": "critical",
                        "explanation": (
                            f"This transaction's gas price ({tx_gas:.2f} Gwei) is significantly "
                            f"lower than the current network rate ({network_gas:.2f} Gwei). "
                            "Miners/validators prioritize higher-paying transactions."
                        ),
                        "action": (
                            "Speed up the transaction by resubmitting with the same nonce but "
                            f"a higher gas price (at least {network_gas:.2f} Gwei). "
                            "Most wallets have a 'Speed Up' button for this."
                        ),
                    })

    # failed transactions
    if status == "failed":
        gas_used = tx_data.get("gas_used", 0)
        gas_limit = tx_data.get("gas_limit", 0)

        # Out of gas
        if gas_used > 0 and gas_limit > 0 and gas_used >= gas_limit * 0.99:
            issues.append({
                "issue": "Out of gas",
                "severity": "critical",
                "explanation": (
                    f"The transaction used all {gas_used:,} gas units (limit was {gas_limit:,}). "
                    "The operation required more gas than was allocated."
                ),
                "action": (
                    "Retry the transaction with a higher gas limit. "
                    f"Try setting the gas limit to at least {int(gas_limit * 1.5):,}. "
                    "Note: you were still charged the gas fee for this failed attempt."
                ),
            })
        elif gas_used > 0:
            # Reverted by contract
            if tx_data.get("is_contract_interaction"):
                issues.append({
                    "issue": "Reverted by smart contract",
                    "severity": "critical",
                    "explanation": (
                        "The smart contract rejected this transaction. Common reasons: "
                        "insufficient token balance, slippage too high on a swap, "
                        "token approval missing, or a contract-specific condition wasn't met."
                    ),
                    "action": (
                        "Check that you have enough tokens, that you've approved the "
                        "contract to spend your tokens, and that your slippage tolerance "
                        "is appropriate. Then retry."
                    ),
                })
            else:
                issues.append({
                    "issue": "Transaction failed",
                    "severity": "critical",
                    "explanation": "The transaction failed during execution.",
                    "action": "Review the transaction parameters and retry.",
                })

        # Slippage hint for swap-related complaints
        if any(word in complaint_lower for word in ["swap", "dex", "uniswap", "pancakeswap", "slippage", "trade"]):
            issues.append({
                "issue": "Possible slippage failure",
                "severity": "info",
                "explanation": (
                    "If this was a token swap, the price may have moved beyond your "
                    "slippage tolerance between when you submitted and when it was processed."
                ),
                "action": (
                    "Increase your slippage tolerance slightly (e.g., from 0.5% to 1-2%) "
                    "and retry. For volatile tokens, you may need 3-5%."
                ),
            })

    # missing deposits
    if any(word in complaint_lower for word in ["deposit", "credit", "missing", "not received", "not showing", "didn't arrive"]):
        if status == "success" or status == "confirmed":
            confirmations = tx_data.get("confirmations", 0)
            required = tx_data.get("confirmations_required", 6)

            if confirmations < required:
                issues.append({
                    "issue": "Insufficient confirmations",
                    "severity": "warning",
                    "explanation": (
                        f"The transaction has {confirmations} confirmations but the exchange "
                        f"typically requires {required} before crediting your account."
                    ),
                    "action": (
                        f"Wait for the transaction to reach {required} confirmations. "
                        f"Currently at {confirmations}/{required}."
                    ),
                })
            else:
                issues.append({
                    "issue": "Transaction confirmed but not credited",
                    "severity": "warning",
                    "explanation": (
                        f"The transaction has {confirmations} confirmations (more than the "
                        f"{required} required). The exchange should have credited your account."
                    ),
                    "action": (
                        "Contact the exchange support with the transaction hash. "
                        "The deposit may need manual review."
                    ),
                })

        # Check if sent to a contract address
        to_addr = tx_data.get("to_address", "")
        if tx_data.get("is_contract_interaction") and tx_data.get("input_data_length", 0) == 0:
            issues.append({
                "issue": "Sent to a contract address",
                "severity": "critical",
                "explanation": (
                    "The destination address appears to be a smart contract, not a regular wallet. "
                    "If you sent tokens directly to a token contract address, "
                    "recovery may be difficult or impossible."
                ),
                "action": (
                    "Contact the contract owner or project team. If the contract has a "
                    "recovery function, they may be able to help retrieve the funds."
                ),
            })

    # wrong network detection
    if any(word in complaint_lower for word in ["wrong network", "wrong chain", "sent on", "bsc instead", "polygon instead"]):
        issues.append({
            "issue": "Possible wrong network",
            "severity": "critical",
            "explanation": (
                f"The transaction was found on {chain}. If you intended to send on a "
                "different network, the funds are on the wrong chain. "
                "The assets are NOT lost — they exist on this network."
            ),
            "action": (
                f"To access the funds, the recipient needs to connect their wallet to the "
                f"{chain} network. If this is an exchange deposit, contact exchange support "
                "to recover cross-chain deposits."
            ),
        })

    # suspicious activity flags
    from_addr = tx_data.get("from_address", "").lower()
    to_addr_lower = tx_data.get("to_address", "").lower()

    # Tornado Cash / mixer interaction
    if to_addr_lower in KNOWN_MIXER_ADDRESSES or from_addr in KNOWN_MIXER_ADDRESSES:
        issues.append({
            "issue": "Interaction with known mixer contract",
            "severity": "critical",
            "explanation": (
                "This transaction involves a known cryptocurrency mixer (Tornado Cash). "
                "These contracts are sanctioned by OFAC and interaction with them "
                "may result in account restrictions on regulated exchanges."
            ),
            "action": (
                "If you interacted with this contract unknowingly, preserve all records. "
                "Contact exchange compliance if your account is restricted."
            ),
        })

    # Unlimited token approval (large input data to an approval function)
    if tx_data.get("is_contract_interaction") and tx_data.get("input_data_length", 0) > 0:
        # Check for approve function signature (0x095ea7b3)
        input_data = tx_data.get("input_data", "")
        if isinstance(input_data, str) and input_data.startswith("0x095ea7b3"):
            # Check if approval amount is max uint256 (unlimited)
            if "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff" in input_data.lower():
                issues.append({
                    "issue": "Unlimited token approval detected",
                    "severity": "warning",
                    "explanation": (
                        "This transaction grants unlimited spending approval to a smart contract. "
                        "While common for DEXes, this is risky if the contract is malicious — "
                        "it could drain all tokens of that type from your wallet."
                    ),
                    "action": (
                        "Review the approved contract at the block explorer. If you don't "
                        "recognize it, revoke the approval immediately using a tool like "
                        "revoke.cash."
                    ),
                })

    # general explanations
    if any(word in complaint_lower for word in ["gas", "fee", "why did i pay"]):
        issues.append({
            "issue": "Gas fee explanation",
            "severity": "info",
            "explanation": (
                "Gas is the fee paid to network validators for processing your transaction. "
                "You pay gas even if the transaction fails, because validators still used "
                "computational resources to attempt it. Gas = gas_used × gas_price."
            ),
            "action": "This is normal blockchain behavior. No action needed.",
        })

    if any(word in complaint_lower for word in ["slow", "taking long", "how long", "waiting"]):
        issues.append({
            "issue": "Transaction timing explanation",
            "severity": "info",
            "explanation": (
                f"Transaction speed depends on network congestion and the gas price paid. "
                f"On {chain}, typical confirmation times vary from seconds (L2s) to minutes (Ethereum/Bitcoin)."
            ),
            "action": (
                "If the transaction is pending, you can speed it up by sending a replacement "
                "transaction with the same nonce but higher gas price."
            ),
        })

    if not issues:
        if status == "success" or status == "confirmed":
            issues.append({
                "issue": "No issues detected",
                "severity": "info",
                "explanation": "The transaction completed successfully with no anomalies detected.",
                "action": "No action needed. The transaction is confirmed on-chain.",
            })
        else:
            issues.append({
                "issue": "Unable to determine issue",
                "severity": "info",
                "explanation": f"Transaction status is '{status}'. More information may be needed.",
                "action": "Provide additional details about the issue you're experiencing.",
            })

    # Determine overall severity
    severities = [i["severity"] for i in issues]
    if "critical" in severities:
        overall = "critical"
    elif "warning" in severities:
        overall = "warning"
    else:
        overall = "info"

    return {
        "overall_severity": overall,
        "issues_found": len(issues),
        "issues": issues,
        "tx_summary": {
            "chain": chain,
            "status": status,
            "tx_hash": tx_data.get("tx_hash", ""),
            "from": tx_data.get("from_address", ""),
            "to": tx_data.get("to_address", ""),
        },
    }
