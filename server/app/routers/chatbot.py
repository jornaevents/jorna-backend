"""Router for the chatbot bundle-builder flow.

Exposes two endpoints:
  POST /chatbot/start  — begin a new chatbot session
  POST /chatbot/step   — process a step and return the next prompt
"""

from fastapi import APIRouter

from app.models.chatbot_schemas import StepRequest, StepResponse
from app.services.chatbot_service import get_initial_step, process_step

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.post("/start", response_model=StepResponse, summary="Start a new chatbot session")
def chatbot_start():
    """Return the initial Step 0 prompt, helper buttons, and empty state."""
    return get_initial_step()


@router.post("/step", response_model=StepResponse, summary="Process a chatbot step")
async def chatbot_step(body: StepRequest):
    """Accept the current step, user input, and state.

    Returns the next step prompt, helper buttons, updated state, and
    optionally a generated bundle.  Off-script inputs are handled by
    Llama 3.3 via the LLM fallback service.
    """
    return await process_step(
        current_step=body.current_step,
        user_input=body.user_input,
        selected_values=body.selected_values,
        state=body.state,
    )
