import asyncio
from app.core.database import db

async def main():
    try:
        # get any email that has a conversation or ticket
        chat = await db.email_tickets.find_one({"company_id": "1e1bccc0-40a5-4700-a5e2-0dd55cddb75c"})
        print(chat)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
