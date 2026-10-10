"""Electronic signatures.

URS-007 / 21 CFR 11.200: every signature re-checks BOTH components (user ID + password).
URS-007 / 21 CFR 11.50 : the stored signature carries printed name, UTC date/time and meaning.
URS-009               : the signer's role must be allowed for that signature.
Failed attempts are written to the audit trail and committed before the error is raised.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import AuthFailed, Forbidden, Invalid
from ..models import Signature, User
from ..security import verify_password
from . import audit

MEANINGS = ("PERFORMED", "VERIFIED", "REVIEWED", "APPROVED", "REJECTED")


def sign(db: Session, session_user: User, *, username: str, password: str, meaning: str,
         entity: str, entity_id: int, batch_id: int | None = None,
         allowed_roles: list[str] | None = None, must_be_session_user: bool = False,
         not_user_id: int | None = None) -> Signature:
    if meaning not in MEANINGS:
        raise Invalid(f"Unknown signature meaning '{meaning}'. Allowed: {', '.join(MEANINGS)}")

    signer = db.scalar(select(User).where(User.username == username))
    if signer is None or not signer.active or not verify_password(password, signer.password_hash):
        audit.log(db, session_user, "SIGNATURE_FAILED", entity, entity_id,
                  new=f"attempted user={username}, meaning={meaning}",
                  reason="invalid user ID or password", batch_id=batch_id)
        db.commit()   # keep the evidence even though the request fails
        raise AuthFailed("Signature failed: invalid user ID or password.")

    if must_be_session_user and signer.id != session_user.id:
        raise Forbidden(f"A {meaning} signature must be given by the logged-in user ({session_user.username}).")
    if not_user_id is not None and signer.id == not_user_id:
        # URS-006: 4-eyes principle. The verifier can never be the performer.
        raise Forbidden("Verifier must be a different person from the performer (4-eyes principle).")
    if allowed_roles and signer.role not in allowed_roles:
        raise Forbidden(f"Role {signer.role} may not sign as {meaning} here (allowed: {', '.join(allowed_roles)}).")

    sig = Signature(user_id=signer.id, username=signer.username, full_name=signer.full_name, meaning=meaning,
                    utc_ts=clock.utcnow(), entity=entity, entity_id=entity_id, batch_id=batch_id)
    db.add(sig)
    db.flush()
    audit.log(db, signer, "SIGNED", entity, entity_id, field="signature",
              new=f"{signer.full_name} ({signer.username}) | {meaning} | sig#{sig.id}", batch_id=batch_id)
    return sig


def serialize(s: Signature) -> dict:
    return {"id": s.id, "user_id": s.user_id, "username": s.username, "full_name": s.full_name,
            "meaning": s.meaning, "utc_ts": clock.iso(s.utc_ts), "entity": s.entity, "entity_id": s.entity_id}
