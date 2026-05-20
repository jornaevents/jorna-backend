"""Router for the chatbot bundle-builder flow.

Exposes two endpoints:
  POST /chatbot/start  — begin a new chatbot session
  POST /chatbot/step   — process a step and return the next prompt
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.chatbot_schemas import BundleRequest, MultiBundleResponse, StepRequest, StepResponse
from app.services.chatbot_service import generate_bundle_from_request, generate_multi_bundle, get_initial_step, process_step

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.post("/bundles", response_model=MultiBundleResponse, summary="Generate 3 bundle options to compare")
def chatbot_multi_bundle(body: BundleRequest, db: Session = Depends(get_db)):
    """Returns 3 bundle options (Budget, Top Rated, Balanced) for users who
    aren't sure what they want. All inputs are optional — omit anything you
    don't know yet. Pick an option and pass its state to POST /chatbot/step
    to keep refining."""
    return generate_multi_bundle(body, db=db)


@router.post("/bundle", response_model=StepResponse, summary="Generate a bundle from user selections in one shot")
def chatbot_bundle(body: BundleRequest, db: Session = Depends(get_db)):
    """Accept all user inputs at once — needed categories, already booked,
    budget, date/date range, guest count, style — all optional.
    Returns a bundle immediately without requiring a multi-step flow.
    Use the returned state with POST /chatbot/step to refine the bundle."""
    return generate_bundle_from_request(body, db=db)


@router.post("/start", response_model=StepResponse, summary="Start a new chatbot session")
def chatbot_start():
    """Return the initial Step 0 prompt, helper buttons, and empty state."""
    return get_initial_step()


@router.post("/step", response_model=StepResponse, summary="Process a chatbot step")
async def chatbot_step(body: StepRequest, db: Session = Depends(get_db)):
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
        db=db,
    )
