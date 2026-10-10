"""Demo-only helpers. Disabled when EBR_DEMO_MODE=0."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import clock, config
from ..db import get_db
from ..deps import current_user
from ..errors import Forbidden
from ..models import User
from ..services import audit

router = APIRouter(prefix="/api/demo", tags=["demo"])


class AdvanceIn(BaseModel):
    minutes: int = Field(gt=0, le=240)


@router.post("/advance-clock")
def advance_clock(body: AdvanceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Simulate time passing (e.g. to reach the next 30-min IPC). Never present in a real system."""
    if not config.DEMO_MODE:
        raise Forbidden("Demo mode is off.")
    old = clock.utcnow()
    clock.advance(body.minutes)
    audit.log(db, user, "DEMO_CLOCK_ADVANCED", "system", None, field="server_time",
              old=clock.iso(old), new=clock.iso(clock.utcnow()), reason=f"demo: +{body.minutes} min")
    db.commit()
    return {"server_utc": clock.iso(clock.utcnow())}
