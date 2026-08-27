
from app.services.conversation_privacy_service import (
    _luhn_valid,
    redact_pii,
)


class TestEmailRedaction:
    def test_simple_email_is_redacted(self):
        assert redact_pii("contact me at john@example.com please") == (
            "contact me at [REDACTED] please"
        )

    def test_multiple_emails_are_redacted(self):
        text = "from a@x.io to b@y.co.uk"
        assert redact_pii(text) == "from [REDACTED] to [REDACTED]"

    def test_sentence_ending_period_survives(self):
        assert redact_pii("email me at jane@corp.com.") == "email me at [REDACTED]."


class TestPhoneRedaction:
    def test_us_format_is_redacted(self):
        assert "[REDACTED]" in redact_pii("call 555-123-4567 now")

    def test_parenthesized_format_is_redacted(self):
        assert "[REDACTED]" in redact_pii("(555) 123-4567")

    def test_international_format_is_redacted(self):
        assert "[REDACTED]" in redact_pii("+234 803 123 4567")

    def test_short_numbers_are_kept(self):
        # order numbers / quantities must survive
        assert redact_pii("order 123 is ready") == "order 123 is ready"
        assert redact_pii("invoice 2024 paid") == "invoice 2024 paid"

    def test_plain_long_digit_run_redacted(self):
        assert "[REDACTED]" in redact_pii("tracking 403928471638")


class TestCardRedaction:
    def test_luhn_valid_card_is_redacted(self):
        # 4111111111111111 passes Luhn
        assert redact_pii("card 4111111111111111 charged") == ("card [REDACTED] charged")

    def test_luhn_invalid_digits_are_kept(self):
        # 4111111111111112 fails Luhn
        assert "4111111111111112" in redact_pii("ref 4111111111111112")

    def test_spaced_card_is_redacted(self):
        assert redact_pii("pay with 4111 1111 1111 1111 today") == ("pay with [REDACTED] today")


class TestLuhn:
    def test_known_valid(self):
        assert _luhn_valid("4111111111111111") is True

    def test_known_invalid(self):
        assert _luhn_valid("4111111111111112") is False

    def test_too_short(self):
        assert _luhn_valid("411111") is False


class TestEdgeCases:
    def test_empty_text_passthrough(self):
        assert redact_pii("") == ""

    def test_clean_text_unchanged(self):
        assert redact_pii("How do I reset my password?") == ("How do I reset my password?")
