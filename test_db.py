import asyncio
from app.core.database import db

async def main():
    docs = await db.users.find({"email": "abdulabiola21@gmail.com"}).to_list(None)
    for d in docs:
        print(d)

asyncio.run(main())
