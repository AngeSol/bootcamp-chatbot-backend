import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

BASE_DIR = Path(__file__).parent

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
# The key only ever lives here, on the server: it is never returned to the client.
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
PREVIEW_LENGTH = 60
MAX_TOKENS = 1024

# Models the user may pick. Defined server-side only (.env): the client's choice is always re-checked.
DEFAULT_MODELS = "anthropic/claude-sonnet-4-5-20250929,openai/gpt-4o"
ALLOWED_MODELS = [
    m.strip() for m in os.getenv("RODIUMAI_MODELS", DEFAULT_MODELS).split(",") if m.strip()
]
if not ALLOWED_MODELS:
    raise RuntimeError("RODIUMAI_MODELS must list at least one model.")
DEFAULT_MODEL = ALLOWED_MODELS[0]

# Roles stored in the DB.
LLM_ROLES = {"user", "assistant"}  # roles the LLM understands
NOTE_ROLE = "note"  # the student's private note: displayed in the chat, never sent to the LLM
NOTIFICATION_ROLE = "system-notification"
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

# The system prompt lives in its own file so it can be edited (and compared) without touching the code.
_prompt_path = Path(os.getenv("SYSTEM_PROMPT_FILE", "prompts/system.md"))
if not _prompt_path.is_absolute():
    _prompt_path = BASE_DIR / _prompt_path
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()

app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation has none


class ModelsResponse(BaseModel):
    models: list[str]
    default: str


class ChatRequest(BaseModel):
    conversation_id: int
    message: str = Field(min_length=1, max_length=4000)
    model: str | None = None  # None -> DEFAULT_MODEL


class NoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # The DB history is not what the LLM sees: this is the single place where it is converted.
    # - user / assistant      -> sent as is.
    # - note                  -> FILTERED: it is private to the student, the LLM must never see it.
    # - system-notification   -> FILTERED: UI-only message, meaningless (and noisy) for the LLM.
    # - any unknown role      -> FILTERED too, so a future role can never leak into the prompt by accident.
    history = []
    for m in rows:
        if m.role in LLM_ROLES:
            history.append({"role": m.role, "content": m.content})
        # NOTE_ROLE, NOTIFICATION_ROLE and unknown roles are deliberately skipped.
    return history


def event(payload: dict) -> str:
    # One JSON event per line (NDJSON): easy to parse on the client with a simple buffer split.
    return json.dumps(payload, ensure_ascii=False) + "\n"


class LLMStreamError(Exception):
    pass


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first *user* message for the preview
    # (notes can now come first, so seq 1 is no longer guaranteed to be the user's).
    first_user_seq = (
        select(func.min(Message.seq))
        .where(Message.conversation_id == Conversation.id, Message.role == "user")
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(
            Message,
            and_(Message.conversation_id == Conversation.id, Message.seq == first_user_seq),
        )
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/models")
def list_models() -> ModelsResponse:
    return ModelsResponse(models=ALLOWED_MODELS, default=DEFAULT_MODEL)


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(
    conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    # A note is stored like any other message, but no LLM call is made.
    rows = load_messages(db, conversation_id)
    note = Message(
        conversation_id=conversation_id,
        seq=rows[-1].seq + 1 if rows else 1,
        role=NOTE_ROLE,
        content=req.content,
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="The conversation was updated concurrently, please retry."
        )
    db.refresh(note)
    return MessageResponse(
        seq=note.seq, role=note.role, content=note.content, created_at=note.created_at
    )


async def stream_reply(
    conversation_id: int,
    next_seq: int,
    dialogue_count: int,
    user_text: str,
    messages: list[dict],
    model: str,
):
    # Events sent to the client, one JSON per line:
    #   {"type": "delta", "content": "..."}                       a chunk of the answer
    #   {"type": "done", "notification": ..., "usage": ...}       everything is saved
    #   {"type": "error", "detail": "..."}                        nothing was saved
    #
    # The turn (user message + full answer) is written in ONE commit, only once the stream ended
    # normally. If anything fails or the client disconnects (Stop button) before that, nothing is
    # written: the DB never holds a user message without its answer, nor a half answer.
    parts: list[str] = []
    usage = None
    finished = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                RODIUMAI_URL,
                headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": MAX_TOKENS,
                    "stream": True,
                    # If the API never returns a `usage` field while streaming, uncomment:
                    # "stream_options": {"include_usage": True},
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        finished = True
                        break
                    chunk = json.loads(payload)
                    if "error" in chunk:
                        raise LLMStreamError(str(chunk["error"]))
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        parts.append(delta)
                        yield event({"type": "delta", "content": delta})
    except (httpx.HTTPError, json.JSONDecodeError, LLMStreamError):
        yield event({"type": "error", "detail": "L'appel au LLM a échoué. Rien n'a été enregistré, tu peux réessayer."})
        return

    reply = "".join(parts)
    if not finished or not reply:
        # The stream ended without [DONE] (connection cut) or produced nothing: don't save.
        yield event({"type": "error", "detail": "La réponse a été interrompue. Rien n'a été enregistré, tu peux réessayer."})
        return

    notification = None
    try:
        # A dedicated session: the request's one may already be closed once the response is streaming.
        with SessionLocal() as db:
            db.add_all([
                Message(conversation_id=conversation_id, seq=next_seq, role="user", content=user_text),
                Message(conversation_id=conversation_id, seq=next_seq + 1, role="assistant", content=reply),
            ])
            if dialogue_count % NOTIFICATION_EVERY == 0:
                notification = NOTIFICATION_TEXT
                db.add(Message(
                    conversation_id=conversation_id,
                    seq=next_seq + 2,
                    role=NOTIFICATION_ROLE,
                    content=notification,
                ))
            db.commit()
    except IntegrityError:
        # Another request wrote these seq numbers while we were streaming.
        yield event({"type": "error", "detail": "La conversation a été modifiée entre-temps, réessaie."})
        return

    yield event({"type": "done", "notification": notification, "usage": usage})


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    # Everything that can be rejected is checked BEFORE streaming starts, so the client
    # gets a real HTTP error (400 / 404) rather than an error buried in the stream.
    model = req.model or DEFAULT_MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"Model '{model}' is not allowed.")

    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    # seq follows every stored row (notes and notifications included) so it stays unique.
    next_seq = rows[-1].seq + 1 if rows else 1

    # The LLM is stateless: resend the system prompt + the whole (filtered) conversation each turn.
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": req.message},
    ]

    # Count only the dialogue (user + assistant), so notes and notifications don't shift the multiples of 10.
    dialogue_count = len(history) + 2

    return StreamingResponse(
        stream_reply(req.conversation_id, next_seq, dialogue_count, req.message, messages, model),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
