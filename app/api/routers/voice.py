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
from datetime import datetime
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.database import db
from app.services import anthropic_agent_service

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

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "start":
                # begin a new call session
                session_id = msg.get("session_id", "")

                # record the call in database
                await db.calls.insert_one({
                    "company_id": company_id,
                    "session_id": session_id,
                    "timestamp": datetime.utcnow(),
                })

                await websocket.send_json({"type": "status", "status": "ready"})
                logger.info(f"Call started: company={company_id}, session={session_id}")

            elif msg_type == "user_text":
                # frontend provides the transcribed text
                text = msg.get("text")

                if not isinstance(text, str):
                    logger.warning(f"Invalid text data type received: {type(text)}")
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid text data: expected a string",
                    })
                    continue

                if not text.strip():
                    await websocket.send_json({"type": "status", "status": "ready"})
                    continue

                if not session_id:
                    await websocket.send_json({"type": "error", "message": "Session not started"})
                    continue

                # agent — process through existing chat pipeline
                await websocket.send_json({"type": "status", "status": "thinking"})
                try:
                    result = await anthropic_agent_service.chat(company_id, session_id, text)
                    reply = result.get("reply", "I'm sorry, I couldn't generate a response.")
                except Exception as e:
                    logger.error(f"Agent chat failed: {e}")
                    reply = "I'm sorry, I'm having trouble right now. Please try again."

                # send text reply (frontend will handle TTS/audio)
                await websocket.send_json({"type": "reply_text", "text": reply})

                try:
                    await websocket.send_json({"type": "status", "status": "ready"})
                except Exception:
                    logger.info("Client disconnected before ready status could be sent")
                    return

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
            await websocket.send_json({"type": "error", "message": "Internal server error"})
            await websocket.close()
        except Exception:
            pass
