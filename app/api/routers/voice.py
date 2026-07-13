"""
Voice call WebSocket router — real-time text-only agent responses.

Protocol (JSON messages over WebSocket):
  Client → Server:
    {"type": "start", "session_id": "..."}     — begin call session
    {"type": "user_text", "text": "..."}       — transcribed text from frontend
    {"type": "end"}                              — hang up

  Server → Client:
    {"type": "status", "status": "..."}       — thinking / ready / ended
    {"type": "reply_text", "text": "..."}     — agent text reply
    {"type": "error", "message": "..."}       — error occurred
"""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.database import db
from app.services.graph.executor import chat_stream_graph
from app.core.plan_enforcement import enforce_chat_limit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice"])


@router.websocket("/{company_id}/call")
async def voice_call(websocket: WebSocket, company_id: str):
    """Handle a real-time call session over WebSocket that returns text-only replies.

    TTS/audio synthesis is now the frontend's responsibility. This endpoint only
    accepts messages and returns `reply_text` and `status` events.
    """
    await websocket.accept()

    # validate company exists
    company = await db.companies.find_one({"id": company_id})
    if not company:
        await websocket.send_json({"type": "error", "message": "Company not found"})
        await websocket.close()
        return

    session_id = None
    call_start_time = None

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "start":
                # Plan enforcement: check chat limit instead of voice minutes
                try:
                    await enforce_chat_limit(company)
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": getattr(e, "detail", str(e))})
                    await websocket.close()
                    return

                # begin a new call session
                session_id = msg.get("session_id", "")
                call_start_time = datetime.now(tz=timezone.utc)

                # record the call in database
                await db.calls.insert_one(
                    {
                        "company_id": company_id,
                        "session_id": session_id,
                        "timestamp": call_start_time,
                    }
                )

                await websocket.send_json({"type": "status", "status": "ready"})
                logger.info(f"Call started: company={company_id}, session={session_id}")

            elif msg_type == "user_text":
                # frontend provides the transcribed text
                text = msg.get("text")

                if not isinstance(text, str):
                    logger.warning(f"Invalid text data type received: {type(text)}")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Invalid text data: expected a string",
                        }
                    )
                    continue

                if not text.strip():
                    await websocket.send_json({"type": "status", "status": "ready"})
                    continue

                if not session_id:
                    await websocket.send_json(
                        {"type": "error", "message": "Voice session not started. Send a 'start' message before sending text."}
                    )
                    continue

                # agent — process through existing chat pipeline
                await websocket.send_json({"type": "status", "status": "thinking"})
                
                reply = ""
                agents_to_try = [company.get("ai_provider", "anthropic")]
                if agents_to_try[0] == "anthropic" or not company.get("ai_provider"):
                    agents_to_try.extend(["openrouter", "gemini"])
                
                for provider in agents_to_try:
                    try:
                        async for event in chat_stream_graph(
                            company_id=company_id,
                            session_id=session_id,
                            message=text,
                            agent_provider=provider
                        ):
                            if event.get("type") == "text":
                                reply += event.get("content", "")
                        
                        if reply:
                            break
                    except Exception as e:
                        logger.warning(f"Voice agent ({provider}) failed: {e}")
                        
                if not reply:
                    reply = "I'm sorry, I'm having trouble right now. Please try again."

                # send text reply (frontend will handle TTS/audio)
                await websocket.send_json({"type": "reply_text", "text": reply})

                try:
                    await websocket.send_json({"type": "status", "status": "ready"})
                except Exception:
                    logger.info("Client disconnected before ready status could be sent")
                    break

            elif msg_type == "end":
                # user hung up
                logger.info(f"Call ended: company={company_id}, session={session_id}")
                await websocket.send_json({"type": "status", "status": "ended"})
                await websocket.close()
                break

    except WebSocketDisconnect:
        logger.info(f"Call disconnected: company={company_id}, session={session_id}")
    except Exception as e:
        logger.exception(f"Call error: {e}")
        try:
            await websocket.send_json(
                {"type": "error", "message": "An unexpected error occurred during the call. Please try reconnecting."}
            )
            await websocket.close()
        except Exception:
            pass
    finally:
        if session_id and call_start_time:
            # Calculate duration and update the call record
            duration_secs = (datetime.now(tz=timezone.utc) - call_start_time).total_seconds()
            await db.calls.update_one(
                {"company_id": company_id, "session_id": session_id},
                {"$set": {"duration_seconds": duration_secs}}
            )
