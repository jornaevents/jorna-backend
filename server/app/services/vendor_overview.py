"""Everything the vendor web app's Overview opens with, in one request.

Overview asked for five things — the vendor, their bookings, leads, inbox and
pipeline — as five requests, each paying its own round trip and its own
auth lookup. This answers all five at once, each section exactly what its
own route returns, by calling the same service functions:

- vendor: GET /vendors/me
- bookings: GET /bookings/vendor/{vendor_id}?limit=100
- leads: GET /leads
- conversations: GET /conversations
- pipeline: GET /leads/pipeline

A section that fails comes back null rather than failing the whole page:
Overview already shows each card on its own, and a broken inbox shouldn't
blank the dashboard. The failure is logged, and the session rolled back so
the sections after it still read. Only "not a vendor" fails the request,
as it does /vendors/me.

How long each section took goes back with it (`timings`, milliseconds), and
the route repeats them as a Server-Timing header, so whether one request
beats five can be read in the browser rather than guessed.
"""

import logging
import time
from typing import Callable

from sqlalchemy.orm import Session

from app.services.booking_service import get_vendor_bookings
from app.services.contract_service import list_leads
from app.services.conversation_service import list_conversations
from app.services.pipeline_service import list_pipeline
from app.services.vendor_service import get_my_vendor

logger = logging.getLogger(__name__)

BOOKINGS_LIMIT = 100


def vendor_overview(*, caller_user_id: str, db: Session) -> dict:
    """Raises VendorError(404) when the caller has no vendor profile."""
    timings: dict[str, float] = {}

    def timed(name: str, read: Callable[[], object]):
        start = time.perf_counter()
        try:
            return read()
        except Exception:
            logger.exception("vendor overview: %s failed", name)
            db.rollback()
            return None
        finally:
            timings[name] = round((time.perf_counter() - start) * 1000, 1)

    start = time.perf_counter()
    vendor = get_my_vendor(user_id=caller_user_id, db=db)
    timings["vendor"] = round((time.perf_counter() - start) * 1000, 1)
    vendor_id = vendor["vendor_id"]

    return {
        "vendor": vendor,
        "bookings": timed(
            "bookings",
            lambda: get_vendor_bookings(
                vendor_id=vendor_id, caller_user_id=caller_user_id, limit=BOOKINGS_LIMIT, offset=0, db=db,
            ),
        ),
        "leads": timed("leads", lambda: list_leads(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)),
        "conversations": timed("conversations", lambda: list_conversations(caller_user_id=caller_user_id, db=db)),
        "pipeline": timed("pipeline", lambda: list_pipeline(caller_user_id=caller_user_id, db=db)),
        "timings": timings,
    }


def server_timing(timings: dict[str, float]) -> str:
    """The Server-Timing header for those timings: `bookings;dur=12.3, …`."""
    return ", ".join(f"{name};dur={ms}" for name, ms in timings.items())
