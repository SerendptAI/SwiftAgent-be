from app.core.database import db

async def get_billing_details(company_id: str, user_id: str) -> dict:
    company = await db.companies.find_one({"id": company_id, "user_id": user_id})
    if not company:
        return None
    
    # Returning a mock plan and mock saved card for UI demonstration
    # In a real app, this would query Stripe or another payment provider
    return {
        "present_plan": company.get("plan_name", "yellow pill"),
        "saved_cards": [
            {
                "brand": "mastercard",
                "last4": "4563"
            }
        ]
    }

async def subscribe(company_id: str, user_id: str, plan_name: str) -> dict:
    company = await db.companies.find_one({"id": company_id, "user_id": user_id})
    if not company:
        return None
        
    await db.companies.update_one(
        {"id": company_id, "user_id": user_id},
        {"$set": {"plan_name": plan_name}}
    )
    
    return await get_billing_details(company_id, user_id)
