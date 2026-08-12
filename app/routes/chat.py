from fastapi import APIRouter, HTTPException, Request

from app.errors import NoProviderAvailable
from app.schemas import ChatRequest, ChatResponse

router = APIRouter()


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    gateway = request.app.state.gateway
    try:
        return await gateway.chat(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NoProviderAvailable as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "all_providers_failed",
                "message": str(e),
                "attempts": [a.model_dump(mode="json") for a in e.attempts],
            },
        ) from e
