from uuid import UUID

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.database import DbSession
from app.models import User
from app.services import ApiKeyDep
from app.services.fit_import_service import import_fit_file

router = APIRouter()

# Single activity FIT files are a few MB at most; larger uploads are rejected
MAX_FIT_BYTES = 25 * 1024 * 1024


class FitImportResponse(BaseModel):
    status: str
    record_id: UUID | None = None
    start_time: str | None = None
    reason: str | None = None


@router.post("/users/{user_id}/import/fit")
def import_fit(
    user_id: UUID,
    file: UploadFile,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> FitImportResponse:
    """Import one FIT or TCX activity file as a workout of the user (synchronous)."""
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    data = file.file.read(MAX_FIT_BYTES + 1)
    if len(data) > MAX_FIT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="FIT file too large")

    result = import_fit_file(db, user_id, data)
    return FitImportResponse(
        status=result.status,
        record_id=result.record_id,
        start_time=result.start.isoformat() if result.start else None,
        reason=result.reason,
    )
