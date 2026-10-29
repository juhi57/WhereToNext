"""HTTP surface: status codes, error shapes, headers and the PDF download."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class TestSystemRoutes:
    def test_health_reports_configuration(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["environment"] == "test"
        assert body["ai_enabled"] is False
        assert body["planner_model"] == "heuristic-v1"
        assert "cache" in body

    def test_healthz_needs_no_dependencies(self, client: TestClient) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_options_drive_the_form(self, client: TestClient) -> None:
        body = client.get("/api/options").json()
        for key in (
            "transport_modes",
            "stay_types",
            "food_preferences",
            "paces",
            "interests",
            "currencies",
            "destinations",
        ):
            assert body[key], f"{key} must not be empty"
        assert {"value": "INR", "label": "INR"} in body["currencies"]
        assert "Mumbai" in body["destinations"]

    def test_openapi_schema_is_served(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "/api/itineraries" in schema["paths"]

    def test_home_page_renders(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "WhereToNext" in response.text

    def test_response_time_header_is_present(self, client: TestClient) -> None:
        assert "X-Response-Time-Ms" in client.get("/api/health").headers


class TestEstimate:
    def test_returns_a_costing_without_storing_anything(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        body = client.post("/api/estimate", json=trip_payload).json()
        assert body["total"] > 0
        assert body["verdict"] in {"under_budget", "on_budget", "near_limit", "over_budget"}
        assert set(body["lines"]) == {
            "transport",
            "accommodation",
            "food",
            "activities",
            "local_transport",
            "misc",
        }
        assert client.get("/api/itineraries").json() == []

    def test_flags_an_estimated_distance(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        payload = {**trip_payload, "destination": "Narnia"}
        assert client.post("/api/estimate", json=payload).json()["distance_estimated"] is True


class TestCreateItinerary:
    def test_creates_and_returns_a_full_plan(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        response = client.post("/api/itineraries", json=trip_payload)
        assert response.status_code == 201

        body = response.json()
        assert body["from_cache"] is False
        plan = body["itinerary"]
        assert plan["id"]
        assert len(plan["days"]) == 6
        assert plan["budget"]["total"] > 0
        assert plan["meta"]["mode"] == "heuristic"

    def test_stored_plan_can_be_fetched_again(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        created = client.post("/api/itineraries", json=trip_payload).json()["itinerary"]
        fetched = client.get(f"/api/itineraries/{created['id']}").json()
        assert fetched["id"] == created["id"]
        assert fetched["budget"]["total"] == created["budget"]["total"]

    def test_listing_shows_summaries_newest_first(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        client.post("/api/itineraries", json=trip_payload)
        client.post("/api/itineraries", json={**trip_payload, "destination": "Goa"})
        rows = client.get("/api/itineraries").json()
        assert len(rows) == 2
        assert {"id", "destination", "total_cost", "verdict"} <= set(rows[0])

    def test_pagination_is_honoured(self, client: TestClient, trip_payload: dict[str, Any]) -> None:
        for city in ("Goa", "Jaipur", "Kochi"):
            client.post("/api/itineraries", json={**trip_payload, "destination": city})
        assert len(client.get("/api/itineraries?limit=2").json()) == 2
        assert len(client.get("/api/itineraries?limit=2&offset=2").json()) == 1

    def test_delete_removes_the_plan(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        plan = client.post("/api/itineraries", json=trip_payload).json()["itinerary"]
        assert client.delete(f"/api/itineraries/{plan['id']}").status_code == 204
        assert client.get(f"/api/itineraries/{plan['id']}").status_code == 404


class TestErrors:
    def test_missing_plan_returns_a_structured_404(self, client: TestClient) -> None:
        response = client.get("/api/itineraries/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "itinerary_not_found"

    def test_deleting_a_missing_plan_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/api/itineraries/nope").status_code == 404

    def test_validation_errors_name_the_field(
        self, client: TestClient, trip_payload: dict[str, Any], start_date: date
    ) -> None:
        bad = {**trip_payload, "end_date": (start_date - timedelta(days=5)).isoformat()}
        response = client.post("/api/itineraries", json=bad)
        assert response.status_code == 422

        error = response.json()["error"]
        assert error["code"] == "validation_failed"
        assert error["fields"]
        assert all({"field", "message"} <= set(f) for f in error["fields"])

    def test_unknown_fields_are_rejected(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        response = client.post("/api/estimate", json={**trip_payload, "surprise": 1})
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "patch",
        [
            {"adults": 0},
            {"budget_total": -100},
            {"currency": "XYZ"},
            {"pace": "frantic"},
            {"transport_modes": []},
            {"origin": ""},
        ],
    )
    def test_invalid_values_are_rejected(
        self, client: TestClient, trip_payload: dict[str, Any], patch: dict[str, Any]
    ) -> None:
        assert client.post("/api/estimate", json={**trip_payload, **patch}).status_code == 422


class TestPdfDownload:
    def test_serves_a_pdf_attachment(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        plan = client.post("/api/itineraries", json=trip_payload).json()["itinerary"]
        response = client.get(f"/api/itineraries/{plan['id']}/pdf")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert ".pdf" in response.headers["content-disposition"]
        assert response.content.startswith(b"%PDF-")
        assert int(response.headers["content-length"]) == len(response.content)

    def test_inline_mode_renders_in_the_browser(
        self, client: TestClient, trip_payload: dict[str, Any]
    ) -> None:
        plan = client.post("/api/itineraries", json=trip_payload).json()["itinerary"]
        response = client.get(f"/api/itineraries/{plan['id']}/pdf?inline=true")
        assert response.headers["content-disposition"].startswith("inline;")

    def test_pdf_for_a_missing_plan_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/itineraries/nope/pdf").status_code == 404


class TestCaching:
    def test_identical_requests_are_served_from_cache(
        self, settings: Settings, trip_payload: dict[str, Any]
    ) -> None:
        cached = settings.model_copy(update={"cache_ttl_seconds": 300})
        with TestClient(create_app(cached)) as client:
            assert client.post("/api/itineraries", json=trip_payload).json()["from_cache"] is False
            assert client.post("/api/itineraries", json=trip_payload).json()["from_cache"] is True

    def test_refresh_bypasses_the_cache(
        self, settings: Settings, trip_payload: dict[str, Any]
    ) -> None:
        cached = settings.model_copy(update={"cache_ttl_seconds": 300})
        with TestClient(create_app(cached)) as client:
            client.post("/api/itineraries", json=trip_payload)
            body = client.post("/api/itineraries?refresh=true", json=trip_payload).json()
            assert body["from_cache"] is False

    def test_a_changed_brief_is_a_different_cache_entry(
        self, settings: Settings, trip_payload: dict[str, Any]
    ) -> None:
        cached = settings.model_copy(update={"cache_ttl_seconds": 300})
        with TestClient(create_app(cached)) as client:
            client.post("/api/itineraries", json=trip_payload)
            body = client.post("/api/itineraries", json={**trip_payload, "adults": 3}).json()
            assert body["from_cache"] is False


class TestRateLimit:
    def test_excess_requests_are_rejected_with_retry_after(
        self, settings: Settings, trip_payload: dict[str, Any]
    ) -> None:
        limited = settings.model_copy(update={"rate_limit_per_minute": 2})
        with TestClient(create_app(limited)) as client:
            codes = [
                client.post("/api/itineraries", json=trip_payload).status_code for _ in range(4)
            ]
            assert codes[:2] == [201, 201]
            assert 429 in codes[2:]

            response = client.post("/api/itineraries", json=trip_payload)
            assert response.json()["error"]["code"] == "rate_limited"
            assert int(response.headers["Retry-After"]) > 0

    def test_reads_are_not_rate_limited(
        self, settings: Settings, trip_payload: dict[str, Any]
    ) -> None:
        """Only generation costs a model call; fetching a plan should stay free."""
        limited = settings.model_copy(update={"rate_limit_per_minute": 1})
        with TestClient(create_app(limited)) as client:
            plan = client.post("/api/itineraries", json=trip_payload).json()["itinerary"]
            for _ in range(5):
                assert client.get(f"/api/itineraries/{plan['id']}").status_code == 200
