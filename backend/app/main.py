import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Any

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from supabase import create_client

from .priority import raise_priority_for_repeat_report
from .settings import Settings, get_settings

logger = logging.getLogger(__name__)
MAX_REQUEST_BODY_BYTES = 30 * 1024 * 1024


class _RequestBodyTooLarge(BaseException):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status_code: int, detail: str) -> None:
            body = json.dumps({"detail": detail}).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                await reject(status.HTTP_400_BAD_REQUEST, "Content-Length must be a valid integer")
                return
            if declared_length < 0:
                await reject(status.HTTP_400_BAD_REQUEST, "Content-Length cannot be negative")
                return
            if declared_length > self.max_bytes:
                await reject(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "Request body is too large; reduce the total size of the evidence images.",
                )
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await reject(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "Request body is too large; reduce the total size of the evidence images.",
            )


def cleanup_failed_complaint_submission(
    supabase: Any,
    complaint_id: str,
    master_issue_id: str | None,
    uploaded_storage_paths: list[str],
) -> None:
    complaint_removed = False
    try:
        supabase.table("complaints").delete().eq("id", complaint_id).execute()
        complaint_removed = True
    except Exception:
        logger.exception("Could not remove complaint after failed submission")

    if master_issue_id and complaint_removed:
        try:
            supabase.table("master_issues").delete().eq("id", master_issue_id).execute()
        except Exception:
            logger.exception("Could not remove master issue after failed submission")

    for storage_path in uploaded_storage_paths:
        try:
            supabase.storage.from_("complaint-evidence").remove([storage_path])
        except Exception:
            logger.exception("Could not remove evidence after failed submission")


class ComplaintCategory(StrEnum):
    ROAD_DAMAGE = "road_damage"
    GARBAGE = "garbage"
    WATER_LEAKAGE = "water_leakage"
    DRAINAGE = "drainage"
    STREETLIGHT = "streetlight"
    SANITATION = "sanitation"
    OTHER = "other"


class ComplaintSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ComplaintCategory
    description: str = Field(min_length=10, max_length=5000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    location_label: str = Field(min_length=1, max_length=300)
    is_anonymous: bool

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise ValueError("Description must contain at least 10 meaningful characters")
        return normalized

    @field_validator("location_label")
    @classmethod
    def normalize_location_label(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Location must contain at least one non-whitespace character")
        return normalized


class ComplaintReceipt(BaseModel):
    complaint_id: str
    public_reference: str
    tracking_token: str
    status: str
    emergency_flag: bool


class TrackingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_token: str = Field(min_length=32, max_length=128)


class TrackingResponse(BaseModel):
    public_reference: str
    category: str
    status: str
    submitted_at: str
    updated_at: str
    history: list["StatusHistoryEntry"] = Field(default_factory=list)
    master_issue_reference: str | None = None
    master_issue_status: str | None = None
    report_count: int | None = None


class StatusHistoryEntry(BaseModel):
    status: str
    note: str | None
    created_at: str


class PublicIssue(BaseModel):
    id: str
    public_reference: str
    category: str
    title: str
    priority: str
    status: str
    report_count: int
    action_count: int
    created_at: str
    updated_at: str
    resolved_at: str | None


class PublicDashboardSummary(BaseModel):
    open_master_issues: int
    duplicate_reports: int
    critical_issues: int
    resolved_issues: int


class AuthenticatedUser(BaseModel):
    firebase_uid: str
    email: str | None
    display_name: str | None
    role: str
    is_staff: bool
    department_id: str | None = None


class StaffStatusUpdate(BaseModel):
    status: ComplaintCategory | str
    note: str | None = Field(default=None, max_length=1000)


class StaffAssignment(BaseModel):
    department_id: str
    officer_firebase_uid: str | None = None


class DuplicateDecision(BaseModel):
    decision: str
    note: str | None = Field(default=None, max_length=1000)


class MasterIssueMerge(BaseModel):
    complaint_id: str
    reason: str | None = Field(default=None, max_length=1000)


class PriorityUpdate(BaseModel):
    priority: str
    priority_score: float = Field(ge=0, le=100)
    factors: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=1000)


class EmergencyReviewUpdate(BaseModel):
    review_status: str
    review_note: str | None = Field(default=None, max_length=1000)


class EscalationCreate(BaseModel):
    master_issue_id: str
    level: int = Field(ge=1, le=5)
    reason: str = Field(min_length=3, max_length=2000)
    authority: str = Field(min_length=1, max_length=300)
    sla_hours: int | None = Field(default=None, gt=0, le=8760)
    due_at: datetime | None = None


class EscalationUpdate(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=1000)


class RootCausePayload(BaseModel):
    probable_cause: str | None = Field(default=None, max_length=2000)
    verified_cause: str | None = Field(default=None, max_length=2000)
    recurrence_interval_days: int | None = Field(default=None, ge=0)
    repair_materials: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)


class FinancialRecordPayload(BaseModel):
    expense_category: str = Field(min_length=1, max_length=200)
    amount: float = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    description: str | None = Field(default=None, max_length=2000)
    document_path: str | None = Field(default=None, max_length=1000)


class StaffComplaintSummary(BaseModel):
    id: str
    public_reference: str
    category: str
    description: str
    location_label: str | None
    status: str
    emergency_flag: bool
    submitted_at: str
    updated_at: str
    master_issue_id: str | None = None
    master_issue_reference: str | None = None
    master_issue_status: str | None = None
    priority: str | None = None
    assigned_department_id: str | None = None
    assigned_department_name: str | None = None
    report_count: int = 1
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_candidates: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)


COMPLAINT_STATUSES = {
    "submitted",
    "under_verification",
    "verified",
    "assigned",
    "in_progress",
    "resolved",
    "closed",
    "reopened",
    "rejected",
}
VALID_DUPLICATE_DECISIONS = {"duplicate", "potential", "new_issue", "rejected"}
STATUS_EDITOR_ROLES = {"officer", "supervisor", "administrator"}
ASSIGNMENT_MANAGER_ROLES = {"supervisor", "administrator"}
DUPLICATE_REVIEWER_ROLES = {"supervisor", "administrator"}
OPERATIONS_MANAGER_ROLES = {"supervisor", "administrator"}
AUDIT_ROLES = {"supervisor", "administrator", "auditor"}
VALID_ROLES = {"citizen", "officer", "supervisor", "administrator", "auditor"}
ESCALATION_STATUSES = {"open", "acknowledged", "in_progress", "resolved", "cancelled"}
EMERGENCY_REVIEW_STATUSES = {"pending", "verified", "not_emergency", "routed"}


EMERGENCY_TERMS = (
    "bomb",
    "explosion",
    "murder",
    "attack",
    "violent",
    "woman safety",
    "women safety",
    "life threatening",
    "dangerous collapse",
)
CRITICAL_SEVERITY_TERMS = ("collapse", "electrocution", "fire", "flood", "life threatening")
HIGH_SEVERITY_TERMS = ("dangerous", "blocked", "contamination", "injury", "overflow")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_COMPLAINT = 5
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (),
}


def matches_image_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return any(content.startswith(signature) for signature in IMAGE_SIGNATURES[content_type])


def build_supabase_client(settings: Settings) -> Any:
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def get_supabase(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    return build_supabase_client(settings)


def build_firebase_app(settings: Settings) -> firebase_admin.App:
    if not settings.firebase_project_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase authentication is not configured on the server",
        )
    try:
        return firebase_admin.get_app()
    except ValueError:
        if settings.firebase_service_account_file:
            service_account = credentials.Certificate(settings.firebase_service_account_file)
        elif settings.firebase_service_account_json:
            try:
                service_account = credentials.Certificate(json.loads(settings.firebase_service_account_json))
            except json.JSONDecodeError as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="FIREBASE_SERVICE_ACCOUNT_JSON must be valid single-line JSON",
                ) from error
        else:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Configure FIREBASE_SERVICE_ACCOUNT_FILE or FIREBASE_SERVICE_ACCOUNT_JSON",
            )
        return firebase_admin.initialize_app(
            service_account,
            {"projectId": settings.firebase_project_id},
        )


def get_authenticated_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> AuthenticatedUser:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication is required")
    try:
        build_firebase_app(settings)
        decoded = firebase_auth.verify_id_token(authorization[7:])
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from error

    firebase_uid = str(decoded["uid"])
    profile_result = (
        supabase.table("firebase_profiles")
        .select("firebase_uid,display_name,role,department_id")
        .eq("firebase_uid", firebase_uid)
        .limit(1)
        .execute()
    )
    profile = profile_result.data[0] if profile_result.data else {"role": "citizen"}
    role = str(profile["role"])
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account role is not configured",
        )
    return AuthenticatedUser(
        firebase_uid=firebase_uid,
        email=decoded.get("email"),
        display_name=profile.get("display_name") or decoded.get("name"),
        role=role,
        is_staff=role in {"officer", "supervisor", "administrator", "auditor"},
        department_id=profile.get("department_id"),
    )


def require_staff(user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)]) -> AuthenticatedUser:
    if not user.is_staff:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access is required")
    return user


def require_citizen(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role != "citizen":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Citizen complaint reporting is required")
    return user


def require_status_editor(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role not in STATUS_EDITOR_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Status editing permission is required")
    return user


def require_assignment_manager(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role not in ASSIGNMENT_MANAGER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Department assignment permission is required")
    return user


def require_duplicate_reviewer(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role not in DUPLICATE_REVIEWER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Duplicate review permission is required")
    return user


def require_operations_manager(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role not in OPERATIONS_MANAGER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operations management permission is required")
    return user


def require_audit_access(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    if user.role not in AUDIT_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Audit permission is required")
    return user


def tracking_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def detect_emergency(description: str) -> bool:
    normalized = description.casefold()
    return any(term in normalized for term in EMERGENCY_TERMS)


def text_similarity(first: str, second: str) -> float:
    first_words = set(first.casefold().split())
    second_words = set(second.casefold().split())
    union = first_words | second_words
    if not union:
        return 0
    return len(first_words & second_words) / len(union)


def _count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def calculate_priority(description: str, emergency_flag: bool) -> tuple[str, float, float]:
    normalized = description.casefold()
    if emergency_flag:
        return "critical", 95, 95
    critical_matches = sum(term in normalized for term in CRITICAL_SEVERITY_TERMS)
    high_matches = sum(term in normalized for term in HIGH_SEVERITY_TERMS)
    if critical_matches:
        score = min(90.0, 75.0 + critical_matches * 5)
        return "high", score, score
    if high_matches:
        score = min(70.0, 50.0 + high_matches * 5)
        return "medium", score, score
    return "low", 25, 25


def build_master_issue_title(category: ComplaintCategory, location_label: str | None) -> str:
    category_name = category.value.replace("_", " ").title()
    if location_label:
        return f"{category_name} near {location_label}"
    return category_name


def create_master_issue_for_complaint(
    supabase: Any,
    complaint: dict[str, Any],
    payload: ComplaintSubmission,
) -> dict[str, Any]:
    priority, priority_score, severity_score = calculate_priority(
        payload.description,
        complaint["emergency_flag"],
    )
    master_issue = (
        supabase.table("master_issues")
        .insert(
            {
                "category": payload.category.value,
                "title": build_master_issue_title(payload.category, payload.location_label),
                "description": payload.description,
                "centroid": complaint["location"],
                "status": "under_verification",
                "priority": priority,
                "priority_score": priority_score,
                "severity_score": severity_score,
            }
        )
        .execute()
    )
    if not master_issue.data:
        raise RuntimeError("Master issue could not be created")

    master_issue_row = master_issue.data[0]
    try:
        link = (
            supabase.table("complaint_master_links")
            .insert(
                {
                    "complaint_id": complaint["id"],
                    "master_issue_id": master_issue_row["id"],
                    "decision": "new_issue",
                    "confidence": 1,
                    "evidence": {"source": "initial_intake"},
                }
            )
            .execute()
        )
        if not link.data:
            raise RuntimeError("Complaint could not be linked to its master issue")

        status_history = (
            supabase.table("status_history")
            .insert(
                {
                    "complaint_id": complaint["id"],
                    "from_status": "submitted",
                    "to_status": "under_verification",
                    "note": "Initial master issue created during intake",
                }
            )
            .execute()
        )
        if not status_history.data:
            raise RuntimeError("Complaint status history could not be recorded")

        updated_complaint = (
            supabase.table("complaints")
            .update({"status": "under_verification"})
            .eq("id", complaint["id"])
            .execute()
        )
        if not updated_complaint.data:
            raise RuntimeError("Complaint status could not be updated")
        return master_issue_row
    except Exception:
        supabase.table("complaint_master_links").delete().eq(
            "master_issue_id", master_issue_row["id"]
        ).execute()
        supabase.table("master_issues").delete().eq(
            "id", master_issue_row["id"]
        ).execute()
        raise


def create_duplicate_candidates(
    supabase: Any,
    complaint: dict[str, Any],
    payload: ComplaintSubmission,
    master_issue_id: str,
) -> None:
    nearby = supabase.rpc(
        "find_nearby_master_issues",
        {
            "p_category": payload.category.value,
            "p_latitude": payload.latitude,
            "p_longitude": payload.longitude,
            "p_exclude_id": master_issue_id,
            "p_radius_meters": 500,
        },
    ).execute()
    candidates = []
    for candidate in nearby.data or []:
        location_similarity = max(
            0.0,
            1.0 - float(candidate["distance_meters"]) / 500.0,
        )
        similarity = text_similarity(payload.description, candidate["description"])
        combined_score = (similarity * 0.6) + (location_similarity * 0.4)
        if combined_score < 0.35:
            continue
        candidates.append(
            {
                "complaint_id": complaint["id"],
                "candidate_master_issue_id": candidate["id"],
                "image_similarity": None,
                "text_similarity": round(similarity, 5),
                "location_similarity": round(location_similarity, 5),
                "combined_score": round(combined_score, 5),
                "decision": "potential",
                "evidence": {
                    "method": "location_and_text_v1",
                    "distance_meters": round(float(candidate["distance_meters"]), 2),
                },
            }
        )
    if candidates:
        result = supabase.table("duplicate_candidates").insert(candidates).execute()
        if not result.data:
            raise RuntimeError("Duplicate candidates could not be stored")


def find_matching_master_issue(
    supabase: Any,
    payload: ComplaintSubmission,
) -> dict[str, Any] | None:
    nearby = supabase.rpc(
        "find_nearby_master_issues",
        {
            "p_category": payload.category.value,
            "p_latitude": payload.latitude,
            "p_longitude": payload.longitude,
            "p_exclude_id": None,
            "p_radius_meters": 500,
        },
    ).execute()
    best_match: dict[str, Any] | None = None
    best_score = 0.65
    for candidate in nearby.data or []:
        location_similarity = max(
            0.0,
            1.0 - float(candidate["distance_meters"]) / 500.0,
        )
        similarity = text_similarity(payload.description, candidate["description"])
        combined_score = (similarity * 0.6) + (location_similarity * 0.4)
        if combined_score >= best_score:
            best_score = combined_score
            best_match = {
                **candidate,
                "text_similarity": round(similarity, 5),
                "location_similarity": round(location_similarity, 5),
                "combined_score": round(combined_score, 5),
            }
    return best_match


def link_complaint_to_existing_issue(
    supabase: Any,
    complaint: dict[str, Any],
    match: dict[str, Any],
) -> None:
    link = (
        supabase.table("complaint_master_links")
        .insert(
            {
                "complaint_id": complaint["id"],
                "master_issue_id": match["id"],
                "decision": "duplicate",
                "confidence": match["combined_score"],
                "evidence": {
                    "method": "location_and_text_v1",
                    "distance_meters": round(float(match["distance_meters"]), 2),
                },
            }
        )
        .execute()
    )
    if not link.data:
        raise RuntimeError("Complaint could not be linked to the existing issue")

    candidate = (
        supabase.table("duplicate_candidates")
        .insert(
            {
                "complaint_id": complaint["id"],
                "candidate_master_issue_id": match["id"],
                "text_similarity": match["text_similarity"],
                "location_similarity": match["location_similarity"],
                "combined_score": match["combined_score"],
                "decision": "duplicate",
                "evidence": {"method": "location_and_text_v1"},
            }
        )
        .execute()
    )
    if not candidate.data:
        raise RuntimeError("Duplicate decision could not be recorded")

    history = (
        supabase.table("status_history")
        .insert(
            {
                "complaint_id": complaint["id"],
                "from_status": "submitted",
                "to_status": "under_verification",
                "note": "Linked to an existing master issue during intake",
            }
        )
        .execute()
    )
    if not history.data:
        raise RuntimeError("Complaint status history could not be recorded")

    updated = (
        supabase.table("complaints")
        .update({"status": "under_verification"})
        .eq("id", complaint["id"])
        .execute()
    )
    if not updated.data:
        raise RuntimeError("Complaint status could not be updated")

    issue = (
        supabase.table("master_issues")
        .select("priority,priority_score")
        .eq("id", match["id"])
        .limit(1)
        .execute()
    )
    if not issue.data:
        raise RuntimeError("Matched master issue could not be loaded for priority update")
    current_priority = issue.data[0]["priority"]
    current_score = float(issue.data[0]["priority_score"] or 0)
    next_priority, next_score = raise_priority_for_repeat_report(
        current_priority,
        current_score,
    )
    report_count = (
        supabase.table("complaint_master_links")
        .select("complaint_id", count="exact")
        .eq("master_issue_id", match["id"])
        .execute()
        .count
    ) or 1
    updated_at = datetime.now(timezone.utc).isoformat()
    supabase.table("master_issues").update({
        "priority": next_priority,
        "priority_score": next_score,
        "updated_at": updated_at,
    }).eq("id", match["id"]).execute()
    history = supabase.table("priority_history").insert({
        "master_issue_id": match["id"],
        "previous_score": current_score,
        "next_score": next_score,
        "previous_priority": current_priority,
        "next_priority": next_priority,
        "factors": {
            "source": "repeat_report",
            "report_count": report_count,
            "score_increment": 10,
        },
    }).execute()
    if not history.data:
        raise RuntimeError("Repeat-report priority history could not be recorded")


app = FastAPI(
    title="CivicLens API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/auth/me", response_model=AuthenticatedUser)
def get_current_user(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> AuthenticatedUser:
    return user


def write_audit_log(
    supabase: Any,
    user: AuthenticatedUser,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    supabase.table("audit_logs").insert({
        "actor_firebase_uid": user.firebase_uid,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metadata": metadata or {},
    }).execute()


def get_staff_complaint(supabase: Any, complaint_id: str) -> dict[str, Any]:
    result = (
        supabase.table("complaints")
        .select("id,public_reference,category,description,location_label,status,emergency_flag,submitted_at,updated_at,reporter_firebase_uid")
        .eq("id", complaint_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Complaint not found")
    complaint = result.data[0]
    link_result = (
        supabase.table("complaint_master_links")
        .select("master_issue_id")
        .eq("complaint_id", complaint_id)
        .limit(1)
        .execute()
    )
    if link_result.data:
        master_result = (
            supabase.table("master_issues")
            .select("id,public_reference,status,priority,assigned_department_id")
            .eq("id", link_result.data[0]["master_issue_id"])
            .limit(1)
            .execute()
        )
        if master_result.data:
            master = master_result.data[0]
            complaint.update({
                "master_issue_id": master["id"],
                "master_issue_reference": master["public_reference"],
                "master_issue_status": master["status"],
                "priority": master["priority"],
            })
            if master.get("assigned_department_id"):
                department_result = supabase.table("departments").select("name").eq(
                    "id", master["assigned_department_id"]
                ).limit(1).execute()
                if department_result.data:
                    complaint["assigned_department_id"] = master["assigned_department_id"]
                    complaint["assigned_department_name"] = department_result.data[0]["name"]
            if not complaint.get("assigned_department_id"):
                assignment_result = supabase.table("assignments").select(
                    "department_id"
                ).eq("master_issue_id", master["id"]).is_(
                    "unassigned_at", "null"
                ).order("assigned_at", desc=True).limit(1).execute()
                if assignment_result.data:
                    department_result = supabase.table("departments").select("name").eq(
                        "id", assignment_result.data[0]["department_id"]
                    ).limit(1).execute()
                    if department_result.data:
                        complaint["assigned_department_id"] = assignment_result.data[0]["department_id"]
                        complaint["assigned_department_name"] = department_result.data[0]["name"]
    complaint["report_count"] = (
        supabase.table("complaint_master_links")
        .select("complaint_id", count="exact")
        .eq("master_issue_id", complaint.get("master_issue_id", "00000000-0000-0000-0000-000000000000"))
        .execute()
        .count or 1
    )
    complaint["evidence"] = (
        supabase.table("complaint_images")
        .select("storage_path,content_type,byte_size,malware_scan_status,created_at")
        .eq("complaint_id", complaint_id)
        .order("created_at")
        .execute()
    ).data or []
    complaint["duplicate_candidates"] = (
        supabase.table("duplicate_candidates")
        .select("id,candidate_master_issue_id,text_similarity,location_similarity,combined_score,decision,created_at")
        .eq("complaint_id", complaint_id)
        .order("combined_score", desc=True)
        .execute()
    ).data or []
    complaint["history"] = (
        supabase.table("status_history")
        .select("from_status,to_status,note,changed_by_firebase_uid,created_at")
        .eq("complaint_id", complaint_id)
        .order("created_at")
        .execute()
    ).data or []
    return complaint


def get_staff_complaint_queue(supabase: Any, complaints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not complaints:
        return []
    complaint_ids = [item["id"] for item in complaints]
    links = supabase.table("complaint_master_links").select(
        "complaint_id,master_issue_id"
    ).in_("complaint_id", complaint_ids).execute().data or []
    master_ids = list({item["master_issue_id"] for item in links})
    masters = supabase.table("master_issues").select(
        "id,public_reference,status,priority,assigned_department_id"
    ).in_("id", master_ids).execute().data if master_ids else []
    master_by_id = {item["id"]: item for item in masters or []}
    assignments = supabase.table("assignments").select(
        "master_issue_id,department_id"
    ).in_("master_issue_id", master_ids).is_("unassigned_at", "null").order(
        "assigned_at", desc=True
    ).execute().data if master_ids else []
    assignment_by_master: dict[str, dict[str, Any]] = {}
    for assignment in assignments or []:
        assignment_by_master.setdefault(assignment["master_issue_id"], assignment)
    department_ids = list({
        item["assigned_department_id"]
        for item in masters or []
        if item.get("assigned_department_id")
    } | {
        item["department_id"]
        for item in assignments or []
        if item.get("department_id")
    })
    departments = supabase.table("departments").select("id,name").in_(
        "id", department_ids
    ).execute().data if department_ids else []
    department_by_id = {item["id"]: item["name"] for item in departments or []}
    links_by_complaint: dict[str, dict[str, Any]] = {
        item["complaint_id"]: item for item in links
    }
    report_counts: dict[str, int] = {}
    for item in links:
        report_counts[item["master_issue_id"]] = report_counts.get(item["master_issue_id"], 0) + 1
    evidence = supabase.table("complaint_images").select(
        "complaint_id,storage_path,content_type,byte_size,malware_scan_status,created_at"
    ).in_("complaint_id", complaint_ids).order("created_at").execute().data or []
    candidates = supabase.table("duplicate_candidates").select(
        "complaint_id,id,candidate_master_issue_id,text_similarity,location_similarity,combined_score,decision,created_at"
    ).in_("complaint_id", complaint_ids).order("combined_score", desc=True).execute().data or []
    history = supabase.table("status_history").select(
        "complaint_id,from_status,to_status,note,changed_by_firebase_uid,created_at"
    ).in_("complaint_id", complaint_ids).order("created_at").execute().data or []
    evidence_by_complaint: dict[str, list[dict[str, Any]]] = {}
    candidates_by_complaint: dict[str, list[dict[str, Any]]] = {}
    history_by_complaint: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        evidence_by_complaint.setdefault(item["complaint_id"], []).append(
            {key: value for key, value in item.items() if key != "complaint_id"}
        )
    for item in candidates:
        candidates_by_complaint.setdefault(item["complaint_id"], []).append(
            {key: value for key, value in item.items() if key != "complaint_id"}
        )
    for item in history:
        history_by_complaint.setdefault(item["complaint_id"], []).append(
            {key: value for key, value in item.items() if key != "complaint_id"}
        )
    summaries = []
    for complaint in complaints:
        item = dict(complaint)
        link = links_by_complaint.get(item["id"])
        master = master_by_id.get(link["master_issue_id"]) if link else None
        if master:
            item.update({
                "master_issue_id": master["id"],
                "master_issue_reference": master["public_reference"],
                "master_issue_status": master["status"],
                "priority": master["priority"],
            })
            assigned_department_id = master.get("assigned_department_id")
            if not assigned_department_id:
                assigned_department_id = assignment_by_master.get(
                    master["id"], {}
                ).get("department_id")
            item["assigned_department_id"] = assigned_department_id
            item["assigned_department_name"] = (
                department_by_id.get(assigned_department_id)
                if assigned_department_id else None
            )
            item["report_count"] = report_counts.get(master["id"], 1)
        else:
            item["report_count"] = 1
        item["evidence"] = evidence_by_complaint.get(item["id"], [])
        item["duplicate_candidates"] = candidates_by_complaint.get(item["id"], [])
        item["history"] = history_by_complaint.get(item["id"], [])
        summaries.append(item)
    return summaries


def notify_complaint_reporter(
    supabase: Any,
    complaint: dict[str, Any],
    title: str,
    body: str,
) -> None:
    reporter = complaint.get("reporter_firebase_uid")
    if not reporter:
        return
    supabase.table("notifications").insert({
        "recipient_firebase_uid": reporter,
        "complaint_id": complaint["id"],
        "title": title,
        "body": body,
    }).execute()


def ensure_staff_scope(
    supabase: Any,
    user: AuthenticatedUser,
    complaint: dict[str, Any],
) -> None:
    if user.role != "officer":
        return
    if not user.department_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Officer department is not configured")
    assignments = supabase.table("assignments").select("master_issue_id").eq(
        "department_id", user.department_id
    ).is_("unassigned_at", "null").execute()
    allowed = {item["master_issue_id"] for item in assignments.data or []}
    if complaint.get("master_issue_id") not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Complaint is outside your department queue")


@app.get("/api/v1/admin/complaints", response_model=list[StaffComplaintSummary])
def list_staff_complaints(
    status_filter: str | None = None,
    user: Annotated[AuthenticatedUser, Depends(require_staff)] = None,
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> list[StaffComplaintSummary]:
    query = supabase.table("complaints").select(
        "id,public_reference,category,description,location_label,status,emergency_flag,submitted_at,updated_at"
    ).order("updated_at", desc=True).limit(200)
    if status_filter:
        if status_filter not in COMPLAINT_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid complaint status")
        query = query.eq("status", status_filter)
    result = query.execute()
    summaries = get_staff_complaint_queue(supabase, result.data or [])
    if user.role == "officer":
        if not user.department_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Officer department is not configured")
        assignments = supabase.table("assignments").select("master_issue_id").eq(
            "department_id", user.department_id
        ).is_("unassigned_at", "null").execute()
        allowed = {item["master_issue_id"] for item in assignments.data or []}
        summaries = [item for item in summaries if item.get("master_issue_id") in allowed]
    return [StaffComplaintSummary(**item) for item in summaries]


@app.get("/api/v1/admin/complaints/{complaint_id}/evidence")
def get_staff_evidence(
    complaint_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> list[dict[str, Any]]:
    complaint = get_staff_complaint(supabase, complaint_id)
    ensure_staff_scope(supabase, user, complaint)
    result = supabase.table("complaint_images").select(
        "storage_path,content_type,byte_size,malware_scan_status,created_at"
    ).eq("complaint_id", complaint_id).execute()
    evidence = []
    for item in result.data or []:
        signed = supabase.storage.from_("complaint-evidence").create_signed_url(item["storage_path"], 300)
        evidence.append({**item, "signed_url": signed.get("signedURL") or signed.get("signedUrl")})
    return evidence


@app.patch("/api/v1/admin/duplicate-candidates/{candidate_id}", response_model=StaffComplaintSummary)
def review_duplicate_candidate(
    candidate_id: str,
    payload: DuplicateDecision,
    user: Annotated[AuthenticatedUser, Depends(require_duplicate_reviewer)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> StaffComplaintSummary:
    if payload.decision not in VALID_DUPLICATE_DECISIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid duplicate decision")
    candidate_result = supabase.table("duplicate_candidates").select(
        "id,complaint_id,candidate_master_issue_id"
    ).eq("id", candidate_id).limit(1).execute()
    if not candidate_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Duplicate candidate not found")
    candidate = candidate_result.data[0]
    complaint = get_staff_complaint(supabase, candidate["complaint_id"])
    ensure_staff_scope(supabase, user, complaint)
    previous_result = supabase.table("duplicate_candidates").select("decision").eq(
        "id", candidate_id
    ).limit(1).execute()
    previous_decision = previous_result.data[0]["decision"] if previous_result.data else None
    updated = supabase.table("duplicate_candidates").update({
        "decision": payload.decision,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_by_firebase_uid": user.firebase_uid,
    }).eq("id", candidate_id).execute()
    if not updated.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Duplicate decision could not be saved")
    supabase.table("duplicate_decision_history").insert({
        "candidate_id": candidate_id,
        "previous_decision": previous_decision,
        "next_decision": payload.decision,
        "note": payload.note,
    }).execute()
    if payload.decision == "duplicate":
        link = supabase.table("complaint_master_links").upsert({
            "complaint_id": candidate["complaint_id"],
            "master_issue_id": candidate["candidate_master_issue_id"],
            "decision": "duplicate",
            "confidence": 1,
            "evidence": {"reviewed": True, "note": payload.note},
        }).execute()
        if not link.data:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Duplicate link could not be saved")
    write_audit_log(supabase, user, "duplicate_candidate.reviewed", "duplicate_candidate", candidate_id, {
        "decision": payload.decision,
        "note": payload.note,
    })
    notify_complaint_reporter(
        supabase,
        complaint,
        "Duplicate review completed",
        f"Your complaint was reviewed as: {payload.decision.replace('_', ' ')}.",
    )
    return StaffComplaintSummary(**get_staff_complaint(supabase, candidate["complaint_id"]))


@app.get("/api/v1/notifications")
def list_notifications(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> list[dict[str, Any]]:
    result = supabase.table("notifications").select(
        "id,complaint_id,title,body,read_at,created_at"
    ).eq("recipient_firebase_uid", user.firebase_uid).order("created_at", desc=True).limit(50).execute()
    return result.data or []


@app.get("/api/v1/admin/duplicate-candidates/{candidate_id}")
def get_duplicate_candidate_detail(
    candidate_id: str,
    _: Annotated[AuthenticatedUser, Depends(require_duplicate_reviewer)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    result = supabase.table("duplicate_candidates").select(
        "id,complaint_id,candidate_master_issue_id,image_similarity,text_similarity,"
        "location_similarity,location_distance_meters,combined_score,decision,evidence,"
        "reviewed_by_firebase_uid,reviewed_at,created_at"
    ).eq("id", candidate_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Duplicate candidate not found")
    candidate = result.data[0]
    complaint = get_staff_complaint(supabase, candidate["complaint_id"])
    master = supabase.table("master_issues").select(
        "id,public_reference,category,title,description,status,priority,priority_score"
    ).eq("id", candidate["candidate_master_issue_id"]).limit(1).execute()
    history = supabase.table("duplicate_decision_history").select(
        "previous_decision,next_decision,note,changed_by,created_at"
    ).eq("candidate_id", candidate_id).order("created_at").execute()
    return {"candidate": candidate, "complaint": complaint, "candidate_issue": (master.data or [None])[0],
            "history": history.data or []}


@app.post("/api/v1/admin/master-issues/{master_issue_id}/merge")
def merge_master_issue_complaint(
    master_issue_id: str,
    payload: MasterIssueMerge,
    user: Annotated[AuthenticatedUser, Depends(require_duplicate_reviewer)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    target = supabase.table("master_issues").select("id").eq("id", master_issue_id).limit(1).execute()
    if not target.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master issue not found")
    current = supabase.table("complaint_master_links").select("master_issue_id").eq(
        "complaint_id", payload.complaint_id
    ).limit(1).execute()
    if not current.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Complaint link not found")
    previous = current.data[0]["master_issue_id"]
    if previous == master_issue_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Complaint is already linked to this issue")
    updated = supabase.table("complaint_master_links").update({
        "master_issue_id": master_issue_id, "decision": "duplicate", "linked_by": None,
    }).eq("complaint_id", payload.complaint_id).execute()
    if not updated.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Master issue merge could not be saved")
    supabase.table("master_issue_merge_history").insert({
        "complaint_id": payload.complaint_id, "from_master_issue_id": previous,
        "to_master_issue_id": master_issue_id, "action": "merge", "reason": payload.reason,
    }).execute()
    write_audit_log(supabase, user, "master_issue.merged", "complaint", payload.complaint_id,
                    {"from_master_issue_id": previous, "to_master_issue_id": master_issue_id})
    return {"complaint_id": payload.complaint_id, "master_issue_id": master_issue_id, "previous_master_issue_id": previous}


@app.post("/api/v1/admin/master-issues/{master_issue_id}/unmerge")
def unmerge_master_issue_complaint(
    master_issue_id: str,
    payload: MasterIssueMerge,
    user: Annotated[AuthenticatedUser, Depends(require_duplicate_reviewer)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    latest = supabase.table("master_issue_merge_history").select(
        "from_master_issue_id,to_master_issue_id"
    ).eq("complaint_id", payload.complaint_id).eq("to_master_issue_id", master_issue_id).eq(
        "action", "merge"
    ).order("created_at", desc=True).limit(1).execute()
    if not latest.data or not latest.data[0].get("from_master_issue_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No merge history is available to undo")
    previous = latest.data[0]["from_master_issue_id"]
    updated = supabase.table("complaint_master_links").update({
        "master_issue_id": previous, "decision": "potential",
    }).eq("complaint_id", payload.complaint_id).eq("master_issue_id", master_issue_id).execute()
    if not updated.data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Complaint is not linked to this issue")
    supabase.table("master_issue_merge_history").insert({
        "complaint_id": payload.complaint_id, "from_master_issue_id": master_issue_id,
        "to_master_issue_id": previous, "action": "unmerge", "reason": payload.reason,
    }).execute()
    write_audit_log(supabase, user, "master_issue.unmerged", "complaint", payload.complaint_id,
                    {"from_master_issue_id": master_issue_id, "to_master_issue_id": previous})
    return {"complaint_id": payload.complaint_id, "master_issue_id": previous, "restored": True}


@app.get("/api/v1/admin/master-issues/{master_issue_id}/priority-history")
def get_priority_history(
    master_issue_id: str,
    _: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> list[dict[str, Any]]:
    result = supabase.table("priority_history").select("*").eq(
        "master_issue_id", master_issue_id
    ).order("created_at", desc=True).limit(200).execute()
    return result.data or []


@app.patch("/api/v1/admin/master-issues/{master_issue_id}/priority")
def update_master_issue_priority(
    master_issue_id: str,
    payload: PriorityUpdate,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    if payload.priority not in {"critical", "high", "medium", "low"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid priority")
    issue = supabase.table("master_issues").select("priority,priority_score").eq(
        "id", master_issue_id
    ).limit(1).execute()
    if not issue.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master issue not found")
    now = datetime.now(timezone.utc).isoformat()
    updated = supabase.table("master_issues").update({
        "priority": payload.priority, "priority_score": payload.priority_score,
        "priority_override_reason": payload.reason, "updated_at": now,
    }).eq("id", master_issue_id).execute()
    if not updated.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Priority could not be updated")
    supabase.table("priority_history").insert({
        "master_issue_id": master_issue_id, "previous_score": issue.data[0]["priority_score"],
        "next_score": payload.priority_score, "previous_priority": issue.data[0]["priority"],
        "next_priority": payload.priority, "factors": payload.factors, "override_reason": payload.reason,
    }).execute()
    write_audit_log(supabase, user, "master_issue.priority_updated", "master_issue", master_issue_id,
                    {"priority": payload.priority, "score": payload.priority_score})
    return updated.data[0]


@app.get("/api/v1/admin/analytics/priority")
def get_priority_analytics(
    _: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    rows = supabase.table("master_issues").select("priority,priority_score,status,created_at").execute().data or []
    by_priority = {level: 0 for level in ("critical", "high", "medium", "low")}
    by_status: dict[str, int] = {}
    scores: list[float] = []
    for row in rows:
        by_priority[row["priority"]] = by_priority.get(row["priority"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        scores.append(float(row["priority_score"] or 0))
    return {"total_issues": len(rows), "by_priority": by_priority, "by_status": by_status,
            "average_score": round(sum(scores) / len(scores), 3) if scores else 0}


@app.get("/api/v1/admin/emergency-reviews")
def list_emergency_reviews(
    review_status: str | None = None,
    user: Annotated[AuthenticatedUser, Depends(require_staff)] = None,
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> list[dict[str, Any]]:
    query = supabase.table("emergency_reviews").select(
        "id,complaint_id,severity,review_status,assigned_reviewer_firebase_uid,review_note,reviewed_at,created_at"
    ).order("created_at", desc=True).limit(200)
    if review_status:
        if review_status not in EMERGENCY_REVIEW_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid emergency review status")
        query = query.eq("review_status", review_status)
    reviews = query.execute().data or []
    if not reviews:
        return []
    if user.role == "officer":
        scoped_reviews = []
        for review in reviews:
            try:
                complaint = get_staff_complaint(supabase, review["complaint_id"])
                ensure_staff_scope(supabase, user, complaint)
            except HTTPException as error:
                if error.status_code == status.HTTP_403_FORBIDDEN:
                    continue
                raise
            scoped_reviews.append(review)
        reviews = scoped_reviews
        if not reviews:
            return []
    complaint_ids = [item["complaint_id"] for item in reviews]
    complaints = supabase.table("complaints").select(
        "id,public_reference,category,description,status,submitted_at"
    ).in_("id", complaint_ids).execute().data or []
    by_id = {item["id"]: item for item in complaints}
    return [{**item, "complaint": by_id.get(item["complaint_id"])} for item in reviews]


@app.patch("/api/v1/admin/emergency-reviews/{review_id}")
def update_emergency_review(
    review_id: str,
    payload: EmergencyReviewUpdate,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    if payload.review_status not in EMERGENCY_REVIEW_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid emergency review status")
    now = datetime.now(timezone.utc).isoformat()
    update = {"review_status": payload.review_status, "review_note": payload.review_note,
              "reviewed_at": None if payload.review_status == "pending" else now,
              "assigned_reviewer_firebase_uid": user.firebase_uid}
    result = supabase.table("emergency_reviews").update(update).eq("id", review_id).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency review not found")
    supabase.table("complaints").update({
        "emergency_reviewed": payload.review_status != "pending", "updated_at": now
    }).eq("id", result.data[0]["complaint_id"]).execute()
    write_audit_log(supabase, user, "emergency_review.updated", "emergency_review", review_id,
                    {"status": payload.review_status})
    return result.data[0]


@app.get("/api/v1/admin/escalations")
def list_escalations(
    escalation_status: str | None = None,
    _: Annotated[AuthenticatedUser, Depends(require_staff)] = None,
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> list[dict[str, Any]]:
    query = supabase.table("escalations").select("*").order("escalated_at", desc=True).limit(200)
    if escalation_status:
        if escalation_status not in ESCALATION_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid escalation status")
        query = query.eq("status", escalation_status)
    return query.execute().data or []


@app.post("/api/v1/admin/escalations", status_code=status.HTTP_201_CREATED)
def create_escalation(
    payload: EscalationCreate,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    issue = supabase.table("master_issues").select("id").eq("id", payload.master_issue_id).limit(1).execute()
    if not issue.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master issue not found")
    due_at = payload.due_at
    if not due_at and payload.sla_hours:
        due_at = datetime.now(timezone.utc) + timedelta(hours=payload.sla_hours)
    record = {**payload.model_dump(exclude={"due_at"}), "due_at": due_at.isoformat() if due_at else None,
              "sla_due_at": due_at.isoformat() if due_at else None, "created_by": None,
              "created_by_firebase_uid": user.firebase_uid}
    result = supabase.table("escalations").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Escalation could not be created")
    write_audit_log(supabase, user, "escalation.created", "escalation", result.data[0]["id"], record)
    return result.data[0]


@app.patch("/api/v1/admin/escalations/{escalation_id}")
def update_escalation(
    escalation_id: str,
    payload: EscalationUpdate,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    if payload.status not in ESCALATION_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid escalation status")
    now = datetime.now(timezone.utc).isoformat()
    update: dict[str, Any] = {"status": payload.status, "updated_at": now}
    if payload.status == "acknowledged":
        update["acknowledged_at"] = now
    if payload.status == "resolved":
        update["resolved_at"] = now
        update["resolved_by_firebase_uid"] = user.firebase_uid
    result = supabase.table("escalations").update(update).eq("id", escalation_id).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")
    write_audit_log(supabase, user, "escalation.status_updated", "escalation", escalation_id,
                    {"status": payload.status, "note": payload.note})
    return result.data[0]


@app.get("/api/v1/admin/master-issues/{master_issue_id}/root-cause")
def list_root_causes(
    master_issue_id: str,
    _: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> list[dict[str, Any]]:
    return supabase.table("root_cause_records").select("*").eq(
        "master_issue_id", master_issue_id
    ).order("updated_at", desc=True).execute().data or []


@app.post("/api/v1/admin/master-issues/{master_issue_id}/root-cause", status_code=status.HTTP_201_CREATED)
def create_root_cause(
    master_issue_id: str,
    payload: RootCausePayload,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    issue = supabase.table("master_issues").select("id").eq("id", master_issue_id).limit(1).execute()
    if not issue.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master issue not found")
    record = {**payload.model_dump(), "master_issue_id": master_issue_id, "recorded_by": None}
    result = supabase.table("root_cause_records").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Root cause record could not be saved")
    supabase.table("master_issues").update({
        "probable_root_cause": payload.probable_cause, "verified_root_cause": payload.verified_cause,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", master_issue_id).execute()
    write_audit_log(supabase, user, "root_cause.created", "master_issue", master_issue_id)
    return result.data[0]


@app.post("/api/v1/admin/master-issues/{master_issue_id}/financial-records", status_code=status.HTTP_201_CREATED)
def create_financial_record(
    master_issue_id: str,
    payload: FinancialRecordPayload,
    user: Annotated[AuthenticatedUser, Depends(require_operations_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    issue = supabase.table("master_issues").select("id").eq("id", master_issue_id).limit(1).execute()
    if not issue.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master issue not found")
    record = {**payload.model_dump(), "currency": payload.currency.upper(), "master_issue_id": master_issue_id,
              "recorded_by": None}
    result = supabase.table("financial_records").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Financial record could not be saved")
    write_audit_log(supabase, user, "financial_record.created", "master_issue", master_issue_id,
                    {"amount": payload.amount, "currency": payload.currency.upper()})
    return result.data[0]


@app.get("/api/v1/admin/financial-summary")
def get_financial_summary(
    _: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    rows = supabase.table("financial_records").select(
        "master_issue_id,expense_category,amount,currency"
    ).execute().data or []
    totals: dict[str, float] = {}
    by_issue: dict[str, float] = {}
    for row in rows:
        key = f"{row['currency']}:{row['expense_category']}"
        totals[key] = totals.get(key, 0) + float(row["amount"])
        by_issue[row["master_issue_id"]] = by_issue.get(row["master_issue_id"], 0) + float(row["amount"])
    return {"record_count": len(rows), "totals_by_currency_category": totals,
            "totals_by_master_issue": by_issue}


@app.get("/api/v1/admin/analytics/overview")
def get_analytics_overview(
    _: Annotated[AuthenticatedUser, Depends(require_staff)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> dict[str, Any]:
    complaints = supabase.table("complaints").select("status,emergency_flag").execute().data or []
    issues = supabase.table("master_issues").select("status,priority").execute().data or []
    escalations = supabase.table("escalations").select("status,sla_due_at").execute().data or []
    now = datetime.now(timezone.utc)
    overdue = sum(1 for item in escalations if item.get("status") not in {"resolved", "cancelled"}
                  and item.get("sla_due_at") and datetime.fromisoformat(item["sla_due_at"].replace("Z", "+00:00")) < now)
    return {
        "complaints": {"total": len(complaints), "by_status": _count_values(complaints, "status"),
                       "emergency": sum(1 for item in complaints if item["emergency_flag"])},
        "master_issues": {"total": len(issues), "by_status": _count_values(issues, "status"),
                          "by_priority": _count_values(issues, "priority")},
        "escalations": {"total": len(escalations), "overdue_sla": overdue,
                        "by_status": _count_values(escalations, "status")},
    }


@app.get("/api/v1/admin/complaints/{complaint_id}", response_model=StaffComplaintSummary)
def get_staff_complaint_detail(
    complaint_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_assignment_manager)],
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> StaffComplaintSummary:
    complaint = get_staff_complaint(supabase, complaint_id)
    ensure_staff_scope(supabase, user, complaint)
    return StaffComplaintSummary(**complaint)


@app.patch("/api/v1/admin/complaints/{complaint_id}/status", response_model=StaffComplaintSummary)
def update_staff_complaint_status(
    complaint_id: str,
    payload: StaffStatusUpdate,
    user: Annotated[AuthenticatedUser, Depends(require_status_editor)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> StaffComplaintSummary:
    if payload.status not in COMPLAINT_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid complaint status")
    complaint = get_staff_complaint(supabase, complaint_id)
    ensure_staff_scope(supabase, user, complaint)
    previous_status = complaint["status"]
    updated_at = datetime.now(timezone.utc).isoformat()
    update = supabase.table("complaints").update({
        "status": payload.status,
        "updated_at": updated_at,
    }).eq("id", complaint_id).execute()
    if not update.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Complaint status could not be updated")
    if complaint.get("master_issue_id"):
        supabase.table("master_issues").update({
            "status": payload.status,
            "updated_at": updated_at,
            "resolved_at": updated_at if payload.status in {"resolved", "closed"} else None,
        }).eq("id", complaint["master_issue_id"]).execute()
    history = supabase.table("status_history").insert({
        "complaint_id": complaint_id,
        "from_status": previous_status,
        "to_status": payload.status,
        "note": payload.note,
        "changed_by_firebase_uid": user.firebase_uid,
    }).execute()
    if not history.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Status history could not be recorded")
    write_audit_log(supabase, user, "complaint.status_updated", "complaint", complaint_id, {
        "from_status": previous_status,
        "to_status": payload.status,
    })
    notify_complaint_reporter(
        supabase,
        complaint,
        "Complaint status updated",
        f"Your complaint is now: {payload.status.replace('_', ' ')}.",
    )
    return StaffComplaintSummary(**get_staff_complaint(supabase, complaint_id))


@app.get("/api/v1/admin/departments")
def list_staff_departments(
    _: Annotated[AuthenticatedUser, Depends(require_staff)] = None,
    supabase: Annotated[Any, Depends(get_supabase)] = None,
) -> list[dict[str, Any]]:
    result = supabase.table("departments").select("id,name,description").eq("active", True).order("name").execute()
    return result.data or []


@app.post("/api/v1/admin/complaints/{complaint_id}/assignment", response_model=StaffComplaintSummary)
def assign_staff_complaint(
    complaint_id: str,
    payload: StaffAssignment,
    user: Annotated[AuthenticatedUser, Depends(require_assignment_manager)],
    supabase: Annotated[Any, Depends(get_supabase)],
) -> StaffComplaintSummary:
    complaint = get_staff_complaint(supabase, complaint_id)
    ensure_staff_scope(supabase, user, complaint)
    master_issue_id = complaint.get("master_issue_id")
    if not master_issue_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Complaint has no master issue")
    department = supabase.table("departments").select("id").eq("id", payload.department_id).eq("active", True).limit(1).execute()
    if not department.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    supabase.table("assignments").update({
        "unassigned_at": datetime.now(timezone.utc).isoformat(),
    }).eq("master_issue_id", master_issue_id).is_("unassigned_at", "null").execute()
    supabase.table("assignments").insert({
        "master_issue_id": master_issue_id,
        "department_id": payload.department_id,
        "officer_firebase_uid": payload.officer_firebase_uid,
        "assigned_by_firebase_uid": user.firebase_uid,
    }).execute()
    if complaint["status"] in {"submitted", "under_verification", "verified"}:
        supabase.table("complaints").update({"status": "assigned"}).eq("id", complaint_id).execute()
        supabase.table("master_issues").update({"status": "assigned"}).eq("id", master_issue_id).execute()
        supabase.table("status_history").insert({
            "complaint_id": complaint_id,
            "from_status": complaint["status"],
            "to_status": "assigned",
            "note": "Assigned to a municipal department",
            "changed_by_firebase_uid": user.firebase_uid,
        }).execute()
    supabase.table("master_issues").update({
        "assigned_department_id": payload.department_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", master_issue_id).execute()
    write_audit_log(supabase, user, "complaint.assigned", "complaint", complaint_id, {
        "department_id": payload.department_id,
        "officer_firebase_uid": payload.officer_firebase_uid,
    })
    notify_complaint_reporter(
        supabase,
        complaint,
        "Complaint assigned",
        "Your complaint has been assigned to a municipal department.",
    )
    return StaffComplaintSummary(**get_staff_complaint(supabase, complaint_id))


@app.post("/api/v1/complaints", response_model=ComplaintReceipt, status_code=status.HTTP_201_CREATED)
async def create_complaint(
    category: Annotated[ComplaintCategory, Form()],
    description: Annotated[str, Form()],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    location_label: Annotated[str, Form(min_length=1, max_length=300)],
    is_anonymous: Annotated[bool, Form()],
    images: Annotated[list[UploadFile], File()],
    supabase: Annotated[Any, Depends(get_supabase)] = None,
    user: Annotated[AuthenticatedUser, Depends(require_citizen)] = None,
) -> ComplaintReceipt:
    payload = ComplaintSubmission.model_validate({
        "category": category,
        "description": description,
        "latitude": latitude,
        "longitude": longitude,
        "location_label": location_label,
        "is_anonymous": is_anonymous,
    })
    uploaded_images = images
    if not uploaded_images or any(not image.filename for image in uploaded_images):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one evidence image is required")
    if len(uploaded_images) > MAX_IMAGES_PER_COMPLAINT:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="A maximum of five images is allowed")

    image_contents: list[tuple[UploadFile, bytes]] = []
    for image in uploaded_images:
        if image.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only JPEG, PNG, and WebP images are allowed")
        content = await image.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Each image must be 10 MB or smaller")
        if not matches_image_signature(image.content_type, content):
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Image content does not match its declared type")
        image_contents.append((image, content))

    tracking_token = secrets.token_urlsafe(32)
    emergency_flag = detect_emergency(payload.description)
    location_wkt = f"SRID=4326;POINT({payload.longitude} {payload.latitude})"
    complaint = {
        "tracking_token_hash": tracking_hash(tracking_token),
        "category": payload.category.value,
        "description": payload.description,
        "location": location_wkt,
        "location_label": payload.location_label,
        "is_anonymous": payload.is_anonymous,
        "emergency_flag": emergency_flag,
        "reporter_firebase_uid": user.firebase_uid,
    }

    result = supabase.table("complaints").insert(complaint).execute()
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Complaint could not be stored",
        )

    stored = result.data[0]
    if emergency_flag:
        severity = "critical" if any(term in description.casefold() for term in CRITICAL_SEVERITY_TERMS) else "high"
        supabase.table("emergency_reviews").upsert({
            "complaint_id": stored["id"], "severity": severity, "review_status": "pending"
        }).execute()
    uploaded_storage_paths: list[str] = []
    try:
        for image, content in image_contents:
            extension = image.content_type.split("/", maxsplit=1)[1].replace("jpeg", "jpg")
            storage_path = f"{stored['id']}/{secrets.token_hex(16)}.{extension}"
            supabase.storage.from_("complaint-evidence").upload(
                storage_path,
                content,
                {"content-type": image.content_type, "upsert": "false"},
            )
            uploaded_storage_paths.append(storage_path)
            supabase.table("complaint_images").insert({
                "complaint_id": stored["id"],
                "storage_path": storage_path,
                "content_type": image.content_type,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "malware_scan_status": "pending",
            }).execute()
    except Exception as error:
        cleanup_failed_complaint_submission(
            supabase,
            stored["id"],
            None,
            uploaded_storage_paths,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Complaint evidence could not be stored") from error

    master_issue_id: str | None = None
    try:
        matching_issue = find_matching_master_issue(supabase, payload)
        if matching_issue:
            link_complaint_to_existing_issue(supabase, stored, matching_issue)
        else:
            master_issue = create_master_issue_for_complaint(supabase, stored, payload)
            master_issue_id = master_issue["id"]
            create_duplicate_candidates(supabase, stored, payload, master_issue["id"])
    except Exception as error:
        cleanup_failed_complaint_submission(
            supabase,
            stored["id"],
            master_issue_id,
            uploaded_storage_paths,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Complaint processing could not be completed",
        ) from error

    return ComplaintReceipt(
        complaint_id=stored["id"],
        public_reference=stored["public_reference"],
        tracking_token=tracking_token,
        status="under_verification",
        emergency_flag=stored["emergency_flag"],
    )


@app.post("/api/v1/complaints/track", response_model=TrackingResponse)
def track_complaint(
    payload: TrackingRequest,
    supabase: Annotated[Any, Depends(get_supabase)],
) -> TrackingResponse:
    result = (
        supabase.table("complaints")
        .select("id,public_reference,category,status,submitted_at,updated_at")
        .eq("tracking_token_hash", tracking_hash(payload.tracking_token))
        .execute()
    )
    if not result.data or not isinstance(result.data, list):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No complaint was found for this tracking token",
        )
    complaint = result.data[0]
    history_result = (
        supabase.table("status_history")
        .select("to_status,note,created_at")
        .eq("complaint_id", complaint["id"])
        .order("created_at")
        .execute()
    )
    link_result = (
        supabase.table("complaint_master_links")
        .select("master_issue_id,decision")
        .eq("complaint_id", complaint["id"])
        .execute()
    )
    issue_details: dict[str, Any] = {}
    if link_result.data and isinstance(link_result.data, list):
        link = link_result.data[0]
        issue_result = (
            supabase.table("master_issues")
            .select("public_reference,status")
            .eq("id", link["master_issue_id"])
            .execute()
        )
        if issue_result.data and isinstance(issue_result.data, list):
            issue = issue_result.data[0]
            count_result = (
                supabase.table("complaint_master_links")
                .select("complaint_id", count="exact")
                .eq("master_issue_id", link["master_issue_id"])
                .execute()
            )
            issue_details = {
                "master_issue_reference": issue["public_reference"],
                "master_issue_status": issue["status"],
                "report_count": count_result.count or 0,
            }
    return TrackingResponse(
        **{
            **complaint,
            **issue_details,
            "history": [
                StatusHistoryEntry(
                    status=entry["to_status"],
                    note=entry["note"],
                    created_at=entry["created_at"],
                )
                for entry in history_result.data or []
            ],
        }
    )


@app.get("/api/v1/public/issues", response_model=list[PublicIssue])
def list_public_issues(
    supabase: Annotated[Any, Depends(get_supabase)],
) -> list[PublicIssue]:
    result = (
        supabase.table("public_master_issues")
        .select(
            "id,public_reference,category,title,priority,status,"
            "report_count,action_count,created_at,updated_at,resolved_at"
        )
        .order("updated_at", desc=True)
        .limit(100)
        .execute()
    )
    return [PublicIssue(**issue) for issue in result.data]


@app.get("/api/v1/public/summary", response_model=PublicDashboardSummary)
def get_public_dashboard_summary(
    supabase: Annotated[Any, Depends(get_supabase)],
) -> PublicDashboardSummary:
    result = (
        supabase.table("public_dashboard_summary")
        .select("open_master_issues,duplicate_reports,critical_issues,resolved_issues")
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Dashboard summary could not be loaded",
        )
    return PublicDashboardSummary(**result.data)


fastapi_app = app
app = RequestBodyLimitMiddleware(fastapi_app)
