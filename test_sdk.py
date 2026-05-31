import asyncio
from app.services.sdk_service import get_conversation_history

async def main():
    try:
        res = await get_conversation_history("1e1bccc0-40a5-4700-a5e2-0dd55cddb75c", "test@example.com")
        print(res)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
