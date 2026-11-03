import unittest
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import Message, Receive, Scope, Send

# Importing the FastAPI application reads its required server settings at module load.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from app.main import (
    AuthenticatedUser,
    ComplaintCategory,
    ComplaintSubmission,
    RequestBodyLimitMiddleware,
    StaffStatusUpdate,
    matches_image_signature,
    require_citizen,
    list_emergency_reviews,
    link_complaint_to_existing_issue,
    cleanup_failed_complaint_submission,
    update_staff_complaint_status,
)
from app.main import app


class SecurityGuardTests(unittest.TestCase):
    def test_failed_complaint_cleanup_removes_dependents_before_master(self) -> None:
        operations: list[str] = []

        class FakeQuery:
            def __init__(self, table_name: str) -> None:
                self.table_name = table_name

            def delete(self) -> "FakeQuery":
                operations.append(self.table_name)
                return self

            def eq(self, *_: object) -> "FakeQuery":
                return self

            def execute(self) -> SimpleNamespace:
                return SimpleNamespace(data=[{"id": "removed"}])

        class FakeStorageBucket:
            def remove(self, paths: list[str]) -> None:
                operations.extend(f"storage:{path}" for path in paths)

        class FakeStorage:
            def from_(self, _: str) -> FakeStorageBucket:
                return FakeStorageBucket()

        class FakeSupabase:
            storage = FakeStorage()

            def table(self, table_name: str) -> FakeQuery:
                return FakeQuery(table_name)

        cleanup_failed_complaint_submission(
            FakeSupabase(),
            "complaint-1",
            "issue-1",
            ["complaint-1/image.jpg"],
        )

        self.assertEqual(
            operations,
            [
                "complaints",
                "master_issues",
                "storage:complaint-1/image.jpg",
            ],
        )

    def test_request_body_limit_counts_actual_streamed_bytes(self) -> None:
        async def run_limited_request(
            chunks: list[bytes],
            headers: list[tuple[bytes, bytes]],
        ) -> tuple[int, bool]:
            called = False
            responses: list[Message] = []

            async def app(_scope: Scope, receive: Receive, send: Send) -> None:
                nonlocal called
                called = True
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if not message.get("more_body", False):
                        break
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            async def receive() -> Message:
                if chunks:
                    body = chunks.pop(0)
                    return {"type": "http.request", "body": body, "more_body": bool(chunks)}
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                responses.append(message)

            middleware = RequestBodyLimitMiddleware(app, max_bytes=5)
            await middleware(
                {"type": "http", "method": "POST", "headers": headers, "asgi": {"version": "3.0"}},
                receive,
                send,
            )
            response_status = next(
                message["status"]
                for message in responses
                if message["type"] == "http.response.start"
            )
            return int(response_status), called

        streamed_status, app_called = asyncio.run(run_limited_request([b"abc", b"def"], []))
        self.assertEqual(streamed_status, 413)
        self.assertTrue(app_called)

        misleading_status, _ = asyncio.run(
            run_limited_request([b"abc", b"def"], [(b"content-length", b"1")])
        )
        self.assertEqual(misleading_status, 413)

        accepted_status, _ = asyncio.run(run_limited_request([b"12345"], []))
        self.assertEqual(accepted_status, 200)

        declared_status, app_called = asyncio.run(
            run_limited_request([], [(b"content-length", b"6")])
        )
        self.assertEqual(declared_status, 413)
        self.assertFalse(app_called)

    def test_officer_emergency_review_list_is_limited_to_assigned_complaints(self) -> None:
        class FakeQuery:
            def __init__(self, table_name: str) -> None:
                self.table_name = table_name
                self.selected_ids: list[str] = []

            def select(self, *_: object) -> "FakeQuery":
                return self

            def order(self, *_: object, **_kwargs: object) -> "FakeQuery":
                return self

            def limit(self, _: int) -> "FakeQuery":
                return self

            def in_(self, _: str, values: list[str]) -> "FakeQuery":
                self.selected_ids = values
                return self

            def execute(self) -> SimpleNamespace:
                if self.table_name == "emergency_reviews":
                    return SimpleNamespace(data=[
                        {"id": "review-assigned", "complaint_id": "complaint-assigned"},
                        {"id": "review-other", "complaint_id": "complaint-other"},
                    ])
                return SimpleNamespace(data=[
                    {"id": "complaint-assigned", "description": "Assigned report"},
                ] if self.selected_ids == ["complaint-assigned"] else [])

        class FakeSupabase:
            def table(self, table_name: str) -> FakeQuery:
                return FakeQuery(table_name)

        officer = AuthenticatedUser(
            firebase_uid="officer-1",
            email="officer@example.com",
            display_name="Officer",
            role="officer",
            is_staff=True,
            department_id="roads",
        )

        def verify_scope(
            _supabase: object,
            _user: AuthenticatedUser,
            complaint: dict[str, object],
        ) -> None:
            if complaint["id"] != "complaint-assigned":
                raise HTTPException(status_code=403, detail="Outside department")

        with (
            patch("app.main.get_staff_complaint", side_effect=lambda _, complaint_id: {"id": complaint_id}),
            patch("app.main.ensure_staff_scope", side_effect=verify_scope),
        ):
            result = list_emergency_reviews(None, officer, FakeSupabase())

        self.assertEqual([item["id"] for item in result], ["review-assigned"])
        self.assertEqual(result[0]["complaint"]["description"], "Assigned report")

    def test_linking_repeat_report_increases_master_issue_priority(self) -> None:
        class FakeQuery:
            def __init__(self, table_name: str, supabase: "FakeSupabase") -> None:
                self.table_name = table_name
                self.supabase = supabase
                self.operation = ""
                self.payload: dict[str, object] = {}

            def insert(self, payload: dict[str, object]) -> "FakeQuery":
                self.operation = "insert"
                self.payload = payload
                return self

            def update(self, payload: dict[str, object]) -> "FakeQuery":
                self.operation = "update"
                self.payload = payload
                return self

            def select(self, *_: object, **_kwargs: object) -> "FakeQuery":
                self.operation = "select"
                return self

            def eq(self, *_: object) -> "FakeQuery":
                return self

            def limit(self, _: int) -> "FakeQuery":
                return self

            def execute(self) -> SimpleNamespace:
                if self.table_name == "master_issues" and self.operation == "select":
                    return SimpleNamespace(data=[{"priority": "low", "priority_score": 25}])
                if self.table_name == "complaint_master_links" and self.operation == "select":
                    return SimpleNamespace(data=[], count=2)
                if self.table_name == "master_issues" and self.operation == "update":
                    self.supabase.priority_update = self.payload
                if self.table_name == "priority_history" and self.operation == "insert":
                    self.supabase.priority_history = self.payload
                return SimpleNamespace(data=[{"id": "row"}], count=None)

        class FakeSupabase:
            priority_update: dict[str, object] = {}
            priority_history: dict[str, object] = {}

            def table(self, table_name: str) -> FakeQuery:
                return FakeQuery(table_name, self)

        supabase = FakeSupabase()
        link_complaint_to_existing_issue(
            supabase,
            {"id": "complaint-2", "location": "SRID=4326;POINT(78.486 17.385)"},
            {
                "id": "issue-1",
                "distance_meters": 10,
                "text_similarity": 0.9,
                "location_similarity": 0.98,
                "combined_score": 0.93,
            },
        )

        self.assertEqual(supabase.priority_update["priority"], "medium")
        self.assertEqual(supabase.priority_update["priority_score"], 35)
        self.assertEqual(supabase.priority_history["factors"]["report_count"], 2)

    def test_officer_status_update_uses_supported_supabase_builder(self) -> None:
        complaint_id = "complaint-1"
        complaint = {
            "id": complaint_id,
            "public_reference": "CIV-1",
            "category": "road_damage",
            "description": "A sufficiently detailed complaint",
            "location_label": "Central park",
            "status": "assigned",
            "emergency_flag": False,
            "submitted_at": "2026-11-03T00:00:00+00:00",
            "updated_at": "2026-11-03T00:00:00+00:00",
        }
        executed_tables: list[str] = []

        class FakeQuery:
            def __init__(self, table_name: str) -> None:
                self.table_name = table_name

            def update(self, _: dict[str, object]) -> "FakeQuery":
                return self

            def insert(self, _: dict[str, object]) -> "FakeQuery":
                return self

            def eq(self, *_: object) -> "FakeQuery":
                return self

            def execute(self) -> SimpleNamespace:
                executed_tables.append(self.table_name)
                return SimpleNamespace(data=[{"id": complaint_id}])

        class FakeSupabase:
            def table(self, table_name: str) -> FakeQuery:
                return FakeQuery(table_name)

        officer = AuthenticatedUser(
            firebase_uid="officer-1",
            email="officer@example.com",
            display_name="Officer",
            role="officer",
            is_staff=True,
        )

        with (
            patch("app.main.get_staff_complaint", return_value=complaint),
            patch("app.main.ensure_staff_scope"),
            patch("app.main.write_audit_log"),
            patch("app.main.notify_complaint_reporter"),
        ):
            result = update_staff_complaint_status(
                complaint_id,
                StaffStatusUpdate(status="in_progress", note="Work started"),
                officer,
                FakeSupabase(),
            )

        self.assertEqual(result.id, complaint_id)
        self.assertEqual(executed_tables, ["complaints", "status_history"])

    def test_staff_roles_cannot_use_citizen_complaint_guard(self) -> None:
        staff_user = AuthenticatedUser(
            firebase_uid="staff-1",
            email="staff@example.com",
            display_name="Staff",
            role="officer",
            is_staff=True,
        )

        with self.assertRaises(HTTPException) as raised:
            require_citizen(staff_user)

        self.assertEqual(raised.exception.status_code, 403)

    def test_unknown_roles_cannot_be_treated_as_citizens(self) -> None:
        unknown_user = AuthenticatedUser(
            firebase_uid="unknown-1",
            email="unknown@example.com",
            display_name="Unknown",
            role="unconfigured",
            is_staff=False,
        )

        with self.assertRaises(HTTPException):
            require_citizen(unknown_user)

    def test_complaint_payload_rejects_unexpected_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ComplaintSubmission(
                category=ComplaintCategory.OTHER,
                description="A sufficiently detailed complaint",
                latitude=17.385,
                longitude=78.486,
                location_label="Central park",
                is_anonymous=True,
                unexpected="value",
            )

    def test_complaint_requires_location_and_anonymity_choice(self) -> None:
        with self.assertRaises(ValidationError):
            ComplaintSubmission(
                category=ComplaintCategory.OTHER,
                description="A sufficiently detailed complaint",
                latitude=17.385,
                longitude=78.486,
            )

    def test_complaint_location_label_cannot_be_blank(self) -> None:
        with self.assertRaises(ValidationError):
            ComplaintSubmission(
                category=ComplaintCategory.OTHER,
                description="A sufficiently detailed complaint",
                latitude=17.385,
                longitude=78.486,
                location_label="   ",
                is_anonymous=True,
            )

    def test_complaint_location_label_is_normalized(self) -> None:
        complaint = ComplaintSubmission(
            category=ComplaintCategory.OTHER,
            description="A sufficiently detailed complaint",
            latitude=17.385,
            longitude=78.486,
            location_label="  Civic   Centre ",
            is_anonymous=True,
        )

        self.assertEqual(complaint.location_label, "Civic Centre")

    def test_complaint_form_requires_every_report_field(self) -> None:
        schema = TestClient(app).get("/openapi.json").json()
        request_schema = schema["paths"]["/api/v1/complaints"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        schema_name = request_schema["$ref"].rsplit("/", 1)[-1]
        required_fields = set(schema["components"]["schemas"][schema_name]["required"])

        self.assertTrue({
            "category",
            "description",
            "latitude",
            "longitude",
            "location_label",
            "is_anonymous",
            "images",
        }.issubset(required_fields))

    def test_webp_signature_requires_webp_container(self) -> None:
        self.assertTrue(matches_image_signature("image/webp", b"RIFFxxxxWEBP"))
        self.assertFalse(matches_image_signature("image/webp", b"RIFFxxxxNOTP"))
        self.assertFalse(matches_image_signature("image/webp", b"RIFF"))

    def test_local_preview_origin_is_allowed_by_cors(self) -> None:
        response = TestClient(app).options(
            "/health",
            headers={
                "Origin": "http://127.0.0.1:4175",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://127.0.0.1:4175",
        )


if __name__ == "__main__":
    unittest.main()
