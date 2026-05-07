"""Unit tests for the /feedback endpoint and its helpers.

The frontend contract is a discriminated union keyed on `type`. Tests
cover all four variants (bug / data_source / loading_text / general),
the response receipt shape, rate limiting, and the Resend delivery
error paths.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app import feedback as feedback_module
from app.feedback import (
    BugFeedback,
    DataSourceFeedback,
    FeedbackBody,
    FeedbackEmailError,
    GeneralFeedback,
    LoadingTextFeedback,
    _compose_html,
    _compose_subject,
    _compose_text,
    _rate_limit_check,
    _reset_rate_limiter,
    _resend_payload,
)
from app.main import app


# Adapter exposes the discriminated-union validator for direct testing
# (the endpoint goes through FastAPI which uses the same type internally).
_validate_body = TypeAdapter(FeedbackBody).validate_python


# ---------------------------------------------------------------------------
# Schema validation — discriminated union
# ---------------------------------------------------------------------------


class TestDiscriminator:
    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"type": "smiley", "message": "hi"})

    def test_missing_type_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"message": "hi"})

    def test_extra_top_level_field_rejected(self):
        # extra='forbid' — keeps schema drift visible.
        with pytest.raises(ValidationError):
            _validate_body({"type": "general", "message": "hi", "rogue": "x"})


class TestBugVariant:
    def test_minimal_valid(self):
        body = _validate_body({
            "type": "bug",
            "intent": "ask about i-70 traffic",
            "problem": "got an empty response",
        })
        assert isinstance(body, BugFeedback)
        assert body.intent == "ask about i-70 traffic"
        assert body.query is None
        assert body.email is None

    def test_with_optional_query_and_email(self):
        body = _validate_body({
            "type": "bug",
            "intent": "ask",
            "problem": "broken",
            "query": "traffic on i-70?",
            "email": "user@example.com",
        })
        assert body.query == "traffic on i-70?"
        assert body.email == "user@example.com"

    def test_missing_intent_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"type": "bug", "problem": "broken"})

    def test_missing_problem_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"type": "bug", "intent": "ask"})

    def test_empty_intent_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"type": "bug", "intent": "", "problem": "broken"})

    def test_oversized_problem_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({
                "type": "bug",
                "intent": "ok",
                "problem": "x" * 5000,
            })


class TestDataSourceVariant:
    def test_minimal_valid(self):
        body = _validate_body({
            "type": "data_source",
            "category": "Transportation",
            "source": "https://data.denvergov.org/dataset/foo",
            "usefulness": "would help with commute Qs",
        })
        assert isinstance(body, DataSourceFeedback)
        assert body.category == "Transportation"

    @pytest.mark.parametrize(
        "category",
        ["Transportation", "Demographics", "Safety", "Housing", "Environment", "Other"],
    )
    def test_all_canonical_categories_accepted(self, category):
        body = _validate_body({
            "type": "data_source",
            "category": category,
            "source": "src",
            "usefulness": "use",
        })
        assert body.category == category

    def test_unknown_category_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({
                "type": "data_source",
                "category": "Sports",
                "source": "src",
                "usefulness": "use",
            })

    def test_case_sensitive_category(self):
        # Frontend sends exactly the canonical capitalization. Server
        # mirrors that — lowercased version is rejected.
        with pytest.raises(ValidationError):
            _validate_body({
                "type": "data_source",
                "category": "transportation",
                "source": "src",
                "usefulness": "use",
            })


class TestLoadingTextVariant:
    def test_minimal_valid(self):
        body = _validate_body({"type": "loading_text", "phrase": "broncos-ing"})
        assert isinstance(body, LoadingTextFeedback)
        assert body.phrase == "broncos-ing"

    def test_empty_phrase_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({"type": "loading_text", "phrase": ""})


class TestGeneralVariant:
    def test_minimal_valid(self):
        body = _validate_body({"type": "general", "message": "love the app"})
        assert isinstance(body, GeneralFeedback)
        assert body.message == "love the app"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            _validate_body({
                "type": "general",
                "message": "hi",
                "email": "not-an-email",
            })


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------


class TestComposeSubject:
    def test_bug_subject_uses_intent(self):
        body = BugFeedback(type="bug", intent="ask about i-70", problem="broken")
        assert _compose_subject(body) == "[Denver Bot Feedback] Bug: ask about i-70"

    def test_data_source_subject_includes_category_and_source(self):
        body = DataSourceFeedback(
            type="data_source",
            category="Transportation",
            source="https://example.com",
            usefulness="useful",
        )
        subject = _compose_subject(body)
        assert "Data source (Transportation)" in subject
        assert "https://example.com" in subject

    def test_loading_text_subject_uses_phrase(self):
        body = LoadingTextFeedback(type="loading_text", phrase="broncos-ing")
        assert _compose_subject(body) == "[Denver Bot Feedback] Loading phrase: broncos-ing"

    def test_general_subject_uses_message(self):
        body = GeneralFeedback(type="general", message="love it")
        assert _compose_subject(body) == "[Denver Bot Feedback] General: love it"

    def test_subject_truncates_long_field(self):
        body = GeneralFeedback(type="general", message="a" * 200)
        subject = _compose_subject(body)
        assert subject.startswith("[Denver Bot Feedback] General:")
        assert len(subject) <= len("[Denver Bot Feedback] General: ") + 80


class TestComposeHtml:
    def test_bug_html_includes_all_fields(self):
        body = BugFeedback(
            type="bug",
            intent="ask",
            problem="broken",
            query="traffic?",
            email="u@example.com",
        )
        html = _compose_html(body, feedback_id="abc-123")
        assert "abc-123" in html
        assert "ask" in html
        assert "broken" in html
        assert "traffic?" in html
        assert "u@example.com" in html

    def test_bug_html_omits_query_when_blank(self):
        body = BugFeedback(type="bug", intent="ask", problem="broken")
        html = _compose_html(body, feedback_id="abc-123")
        assert "Query" not in html

    def test_data_source_html_renders_variant_fields(self):
        body = DataSourceFeedback(
            type="data_source",
            category="Safety",
            source="src",
            usefulness="use",
        )
        html = _compose_html(body, feedback_id="id")
        assert "Category" in html
        assert "Safety" in html
        assert "Source" in html
        assert "Usefulness" in html

    def test_html_escapes_user_input(self):
        body = GeneralFeedback(type="general", message="<script>alert(1)</script>")
        html = _compose_html(body, feedback_id="id")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestComposeText:
    def test_bug_text_skips_blank_query(self):
        body = BugFeedback(type="bug", intent="ask", problem="broken")
        text = _compose_text(body, feedback_id="id")
        assert "Query" not in text

    def test_general_text_includes_id_and_type(self):
        body = GeneralFeedback(type="general", message="hi")
        text = _compose_text(body, feedback_id="abc-1")
        assert "Feedback ID: abc-1" in text
        assert "Type: general" in text
        assert "Message: hi" in text

    def test_email_appears_when_present(self):
        body = GeneralFeedback(type="general", message="hi", email="x@y.com")
        text = _compose_text(body, feedback_id="id")
        assert "Email: x@y.com" in text


# ---------------------------------------------------------------------------
# Resend payload shape
# ---------------------------------------------------------------------------


class TestResendPayload:
    def test_payload_has_required_keys(self):
        body = GeneralFeedback(type="general", message="hi")
        payload = _resend_payload(body, feedback_id="id", from_email="from@x", to_email="to@y")
        assert payload["from"] == "from@x"
        assert payload["to"] == ["to@y"]
        assert "subject" in payload
        assert "html" in payload
        assert "text" in payload
        assert "reply_to" not in payload

    def test_payload_sets_reply_to_when_email_present(self):
        body = GeneralFeedback(type="general", message="hi", email="user@example.com")
        payload = _resend_payload(body, feedback_id="id", from_email="from@x", to_email="to@y")
        assert payload["reply_to"] == ["user@example.com"]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def setup_method(self):
        _reset_rate_limiter()

    def test_first_n_requests_allowed(self):
        for _ in range(feedback_module.RATE_LIMIT_MAX_REQUESTS):
            assert _rate_limit_check("1.2.3.4") is True

    def test_n_plus_one_blocked(self):
        for _ in range(feedback_module.RATE_LIMIT_MAX_REQUESTS):
            _rate_limit_check("1.2.3.4")
        assert _rate_limit_check("1.2.3.4") is False

    def test_different_ips_have_independent_budgets(self):
        for _ in range(feedback_module.RATE_LIMIT_MAX_REQUESTS):
            _rate_limit_check("1.2.3.4")
        assert _rate_limit_check("9.9.9.9") is True


# ---------------------------------------------------------------------------
# Endpoint integration (mocks Resend)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_rate_limit_between_tests():
    _reset_rate_limiter()
    yield
    _reset_rate_limiter()


@pytest.fixture
def configured_env(monkeypatch):
    """Both env vars set so the endpoint passes its config gate."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "owner@example.com")


class TestFeedbackEndpoint:
    def test_general_happy_path_returns_receipt(self, client, configured_env):
        with patch.object(
            feedback_module, "_send_via_resend", new=AsyncMock(return_value=None)
        ) as mock_send:
            response = client.post(
                "/feedback",
                json={"type": "general", "message": "great app!"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert isinstance(body["id"], str) and len(body["id"]) > 0
        mock_send.assert_awaited_once()

    def test_bug_happy_path(self, client, configured_env):
        with patch.object(
            feedback_module, "_send_via_resend", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/feedback",
                json={
                    "type": "bug",
                    "intent": "ask about i-70 traffic",
                    "problem": "got an empty response",
                    "query": "traffic on i-70?",
                },
            )
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_data_source_happy_path(self, client, configured_env):
        with patch.object(
            feedback_module, "_send_via_resend", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/feedback",
                json={
                    "type": "data_source",
                    "category": "Transportation",
                    "source": "https://data.denvergov.org/dataset/foo",
                    "usefulness": "would help with commute Qs",
                },
            )
        assert response.status_code == 200

    def test_loading_text_happy_path(self, client, configured_env):
        with patch.object(
            feedback_module, "_send_via_resend", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/feedback",
                json={"type": "loading_text", "phrase": "broncos-ing"},
            )
        assert response.status_code == 200

    def test_invalid_payload_returns_422(self, client, configured_env):
        # Missing required field for the bug variant.
        response = client.post(
            "/feedback",
            json={"type": "bug", "intent": "ask"},
        )
        assert response.status_code == 422

    def test_unknown_category_returns_422(self, client, configured_env):
        response = client.post(
            "/feedback",
            json={
                "type": "data_source",
                "category": "Sports",
                "source": "src",
                "usefulness": "use",
            },
        )
        assert response.status_code == 422

    def test_missing_env_returns_503(self, client, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("FEEDBACK_TO_EMAIL", raising=False)
        response = client.post(
            "/feedback",
            json={"type": "general", "message": "hi"},
        )
        assert response.status_code == 503

    def test_resend_failure_returns_502(self, client, configured_env):
        with patch.object(
            feedback_module,
            "_send_via_resend",
            new=AsyncMock(side_effect=FeedbackEmailError("boom")),
        ):
            response = client.post(
                "/feedback",
                json={"type": "general", "message": "hi"},
            )
        assert response.status_code == 502
        # Detail must be generic — never echo the underlying error.
        assert "boom" not in response.text

    def test_rate_limit_returns_429(self, client, configured_env):
        with patch.object(
            feedback_module, "_send_via_resend", new=AsyncMock(return_value=None)
        ):
            for _ in range(feedback_module.RATE_LIMIT_MAX_REQUESTS):
                r = client.post(
                    "/feedback",
                    json={"type": "general", "message": "hi"},
                )
                assert r.status_code == 200
            # Same IP — TestClient sends as 'testclient'.
            r = client.post(
                "/feedback",
                json={"type": "general", "message": "hi"},
            )
        assert r.status_code == 429
