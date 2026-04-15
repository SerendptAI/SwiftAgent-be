import asyncio
from app.core.database import db

async def main():
    await db.users.delete_many({"email": "abdulabiola21@gmail.com"})
    print("Deleted")
    
asyncio.run(main())
