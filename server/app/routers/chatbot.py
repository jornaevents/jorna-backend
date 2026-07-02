"""Router for the chatbot bundle-builder flow.

Exposes two endpoints:
  POST /chatbot/start  — begin a new chatbot session
  POST /chatbot/step   — process a step and return the next prompt

All routes are rate-limited tighter than the global default because the
bundle/step paths can fan out into LLM calls (OpenRouter) — an abuse vector
with real per-request cost.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.models.chatbot_schemas import BundleRequest, MultiBundleResponse, StepRequest, StepResponse
from app.services.chatbot_service import generate_bundle_from_request, generate_multi_bundle, get_initial_step, process_step

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.post("/bundles", response_model=MultiBundleResponse, summary="Generate 3 bundle options to compare")
@limiter.limit("5/minute")
def chatbot_multi_bundle(
    request: Request,
    body: BundleRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns 3 bundle options (Budget, Top Rated, Balanced) persisted as draft
    bundles in the DB.  Each option includes a bundle_id — call
    POST /bundles/{bundle_id}/select to keep one and discard the other two.
    Vendor notifications are held until the user selects a bundle."""
    return generate_multi_bundle(body, db=db, user_id=current_user.user_id)


@router.post("/bundle", response_model=StepResponse, summary="Generate a bundle from user selections in one shot")
@limiter.limit("5/minute")
def chatbot_bundle(
    request: Request,
    body: BundleRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept all user inputs at once — needed categories, already booked,
    budget, date/date range, guest count, style — all optional.
    Returns a bundle immediately without requiring a multi-step flow.
    Use the returned state with POST /chatbot/step to refine the bundle."""
    return generate_bundle_from_request(body, db=db)


@router.post("/start", response_model=StepResponse, summary="Start a new chatbot session")
@limiter.limit("20/minute")
def chatbot_start(request: Request):
    """Return the initial Step 0 prompt, helper buttons, and empty state."""
    return get_initial_step()


@router.post("/step", response_model=StepResponse, summary="Process a chatbot step")
@limiter.limit("20/minute")
async def chatbot_step(
    request: Request,
    body: StepRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept the current step, user input, and state.

    Returns the next step prompt, helper buttons, updated state, and
    optionally a generated bundle.  Off-script inputs are handled by
    Llama 3.3 via the LLM fallback service.

    When the user confirms a bundle, bundle_id and booking_ids are returned
    so the frontend can redirect to the bundle page.
    """
    return await process_step(
        current_step=body.current_step,
        user_input=body.user_input,
        selected_values=body.selected_values,
        state=body.state,
        db=db,
        user_id=current_user.user_id,
    )
