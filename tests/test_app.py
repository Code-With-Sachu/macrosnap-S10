"""Tests for MacroSnap, run with Streamlit's AppTest and mocked Gemini and Twilio.

No real API calls are made, so no keys are needed:  pytest -q
"""

import json
from collections.abc import Iterator
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest
from twilio.base.exceptions import TwilioRestException

APP_PATH = "../app.py"  # AppTest resolves this relative to this test file.

SECRETS = {
    "GEMINI_API_KEY": "test-gemini-key",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
    "TWILIO_CONTENT_SID": "HXtest",
}
SECRETS_WITHOUT_TEMPLATE = {k: v for k, v in SECRETS.items() if k != "TWILIO_CONTENT_SID"}

MEAL_REPLY = "Looks like 2 boiled eggs: about 140 kcal, 12g protein, 1g carbs, 10g fat (rough)."
SUMMARY = "🥚 2 boiled eggs - 140 kcal • Total: 140 kcal, 12g protein, 1g carbs, 10g fat"
SUMMARY_ERROR = "Couldn't create your summary - please try again."
SENT = "Sent! Check your WhatsApp 📲"


@pytest.fixture
def fakes() -> Iterator[SimpleNamespace]:
    """Patch the Gemini and Twilio client classes for the whole test."""
    # Cached clients outlive a single AppTest, so clear them or an earlier
    # test's mocks would leak into this one.
    st.cache_resource.clear()
    with patch("google.genai.Client") as gemini_cls, patch("twilio.rest.Client") as twilio_cls:
        gemini = gemini_cls.return_value
        chat = gemini.chats.create.return_value
        chat.send_message.return_value = MagicMock(text=MEAL_REPLY)
        gemini.models.generate_content.return_value = MagicMock(text=SUMMARY)
        twilio = twilio_cls.return_value
        twilio.messages.create.return_value = MagicMock(sid="SMtest")
        yield SimpleNamespace(gemini=gemini, chat=chat, twilio=twilio)
    st.cache_resource.clear()


def start_app(secrets: dict[str, str] | None = None) -> AppTest:
    """Load app.py with test secrets and run it once (the onboarding screen)."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    for key, value in (SECRETS if secrets is None else secrets).items():
        at.secrets[key] = value
    return at.run()


def onboard(at: AppTest, name: str = "Asha", number: str = "+91 98765-43210") -> AppTest:
    """Fill in the onboarding form and submit it."""
    at.text_input[0].input(name)
    at.text_input[1].input(number)
    return at.button[0].click().run()


def exchange(at: AppTest, text: str = "2 boiled eggs") -> AppTest:
    """Send one chat message and let the app reply."""
    return at.chat_input[0].set_value(text).run()


def chat_texts(at: AppTest) -> list[str]:
    """Return the text of every chat message currently on screen."""
    return [message.markdown[0].value for message in at.chat_message if message.markdown]


def tiny_png() -> bytes:
    """Return a small valid PNG to stand in for a meal photo."""
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "orange").save(buffer, format="PNG")
    return buffer.getvalue()


# --- Onboarding ----------------------------------------------------------------


def test_blank_onboarding_shows_warning(fakes: SimpleNamespace) -> None:
    at = onboard(start_app(), name="   ", number="")
    assert at.warning[0].value == "Please fill in both your name and WhatsApp number."
    assert "onboarded" not in at.session_state
    assert not at.exception


@pytest.mark.parametrize(
    "number",
    ["98765 43210", "+0 98765 43210", "+91 987", "+91 98765 abcde", "+91९८७६५४३२१०"],
)
def test_invalid_number_shows_warning(fakes: SimpleNamespace, number: str) -> None:
    at = onboard(start_app(), number=number)
    assert at.warning[0].value == "Enter the number in international format, e.g. +919876543210."
    assert "onboarded" not in at.session_state
    assert not at.exception


def test_valid_onboarding_shows_welcome(fakes: SimpleNamespace) -> None:
    at = onboard(start_app())
    assert not at.exception
    assert at.session_state.whatsapp_number == "+919876543210"  # normalized

    welcome = chat_texts(at)[0]
    assert welcome.startswith("Hey Asha! I'm MacroSnap 🥗")
    assert 'hit "📤 Send to WhatsApp" at the top' in welcome
    assert at.caption[0].value == "Logged in as Asha - updates go to +919876543210"
    assert at.caption[1].value == "Estimates are approximate and not medical or dietary advice."

    send_button = at.button[0]
    assert send_button.label == "📤 Send to WhatsApp"
    assert send_button.disabled  # nothing to send yet

    create_kwargs = fakes.gemini.chats.create.call_args.kwargs
    assert create_kwargs["model"] == "gemini-3.5-flash"
    assert create_kwargs["config"].system_instruction.startswith("You are MacroSnap")


# --- Chat ------------------------------------------------------------------------


def test_text_exchange_enables_send_right_away(fakes: SimpleNamespace) -> None:
    at = exchange(onboard(start_app()))
    assert not at.exception
    assert chat_texts(at)[1:] == ["2 boiled eggs", MEAL_REPLY]
    assert not at.button[0].disabled  # enabled on the same screen (E1)
    fakes.chat.send_message.assert_called_once_with(["2 boiled eggs"])


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("quota exceeded"), "Sorry, something went wrong: quota exceeded"),
        (None, "Sorry, I couldn't come up with an answer for that"),
    ],
)
def test_chat_failure_shows_friendly_message(
    fakes: SimpleNamespace, failure: Exception | None, expected: str
) -> None:
    if failure is None:
        fakes.chat.send_message.return_value = MagicMock(text=None)  # e.g. a blocked reply
    else:
        fakes.chat.send_message.side_effect = failure
    at = exchange(onboard(start_app()))
    assert not at.exception
    assert chat_texts(at)[-1].startswith(expected)


# --- Send to WhatsApp ----------------------------------------------------------


@pytest.mark.parametrize("failure", ["exception", "empty reply"])
def test_failed_summary_shows_error_and_skips_twilio(fakes: SimpleNamespace, failure: str) -> None:
    if failure == "exception":
        fakes.gemini.models.generate_content.side_effect = RuntimeError("boom")
    else:
        fakes.gemini.models.generate_content.return_value = MagicMock(text=None)
    at = exchange(onboard(start_app()))
    at.button[0].click().run()
    assert not at.exception
    assert at.error[0].value == SUMMARY_ERROR
    assert not at.success
    fakes.twilio.messages.create.assert_not_called()


def test_successful_summary_sends_single_line_template_variable(fakes: SimpleNamespace) -> None:
    long_summary = "🍳 Masala omelette - 250 kcal\n\n\tTotal:    250 kcal " + "x" * 2000
    fakes.gemini.models.generate_content.return_value = MagicMock(text=long_summary)
    at = exchange(onboard(start_app()))
    at.button[0].click().run()

    assert not at.exception
    assert at.success[0].value == SENT
    fakes.twilio.messages.create.assert_called_once()
    kwargs = fakes.twilio.messages.create.call_args.kwargs
    assert kwargs["content_sid"] == "HXtest"
    assert kwargs["from_"] == "whatsapp:+14155238886"
    assert kwargs["to"] == "whatsapp:+919876543210"

    variables = json.loads(kwargs["content_variables"])
    assert variables["1"] == "Asha"
    summary = variables["2"]
    assert not any(char in summary for char in "\n\t\r")
    assert "  " not in summary
    assert len(summary) <= 903
    assert summary.startswith("🍳 Masala omelette - 250 kcal Total: 250 kcal")
    assert summary.endswith("...")

    # The summary is a separate one-off call (E5): the chat saw only the meal.
    fakes.chat.send_message.assert_called_once()
    prompt = fakes.gemini.models.generate_content.call_args.kwargs["contents"]
    assert "User: 2 boiled eggs" in prompt
    assert f"MacroSnap: {MEAL_REPLY}" in prompt
    assert "Hey Asha" not in prompt  # welcome message skipped


def test_summary_transcript_marks_photos(fakes: SimpleNamespace) -> None:
    at = onboard(start_app())
    at.session_state["messages"] = at.session_state["messages"] + [
        {"role": "user", "kind": "image", "content": tiny_png()},
        {"role": "assistant", "kind": "text", "content": "Looks like a masala dosa: ~350 kcal."},
    ]
    at.run()
    assert not at.exception
    assert len(at.get("image")) == 1  # the photo is drawn in the chat

    at.button[0].click().run()
    prompt = fakes.gemini.models.generate_content.call_args.kwargs["contents"]
    assert "User: [meal photo]" in prompt
    assert "MacroSnap: Looks like a masala dosa: ~350 kcal." in prompt


def test_no_template_falls_back_to_plain_message(fakes: SimpleNamespace) -> None:
    at = exchange(onboard(start_app(SECRETS_WITHOUT_TEMPLATE)))
    at.button[0].click().run()
    assert not at.exception
    assert at.success[0].value == SENT
    kwargs = fakes.twilio.messages.create.call_args.kwargs
    assert "content_sid" not in kwargs
    assert kwargs["body"] == f"Hi Asha, here's your MacroSnap summary: {SUMMARY}"


def test_twilio_error_is_readable_and_hints_without_template(fakes: SimpleNamespace) -> None:
    fakes.twilio.messages.create.side_effect = TwilioRestException(
        400, "https://api.twilio.com/Messages.json", msg="Outside the allowed window", code=63016
    )
    at = exchange(onboard(start_app(SECRETS_WITHOUT_TEMPLATE)))
    at.button[0].click().run()
    assert not at.exception
    assert at.error[0].value == "Couldn't send that: Twilio error 63016: Outside the allowed window"
    assert "\x1b[" not in at.error[0].value  # no terminal colour codes
    assert "within 24 hours" in at.info[0].value
