from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from documind.chat import service
from documind.common.config import get_settings
from documind.identity.session import CurrentPrincipal

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class CreateTurn(BaseModel):
    workspace_id: UUID
    question: str = Field(min_length=1)
    conversation_id: UUID | None = None


class TurnCreated(BaseModel):
    turn_id: UUID
    conversation_id: UUID
    stream_url: str


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    created_at: datetime


@router.post("/turns", response_model=TurnCreated, status_code=201)
async def create_turn(p: CurrentPrincipal, body: CreateTurn):
    question = body.question.strip()[: get_settings().max_question_chars]
    ref = await service.create_turn(p, body.workspace_id, question, body.conversation_id)
    return TurnCreated(turn_id=ref.turn_id, conversation_id=ref.conversation_id,
                       stream_url=f"/v1/chat/turns/{ref.turn_id}/events")


@router.get("/turns/{turn_id}/events")
async def stream_events(p: CurrentPrincipal, turn_id: UUID):
    events = await service.open_stream(p, turn_id)
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


@router.post("/turns/{turn_id}/cancel", status_code=202)
async def cancel(p: CurrentPrincipal, turn_id: UUID) -> dict:
    await service.cancel_turn(p, turn_id)
    return {"status": "cancel_requested"}


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(p: CurrentPrincipal, workspace_id: UUID):
    return await service.list_conversations(p, workspace_id)


@router.get("/conversations/{conversation_id}")
async def get_conversation(p: CurrentPrincipal, conversation_id: UUID) -> dict:
    return await service.get_conversation(p, conversation_id)
