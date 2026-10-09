
"""MacroSnap: snap it, track it, text yourself the results.

A Streamlit chat app using Google Gemini for meal analysis and Twilio
for WhatsApp nutrition summaries.

Configuration is loaded from environment variables first, then from
.local Streamlit secrets when available.
"""

import json
import os
import re
from typing import Literal, TypedDict

import streamlit as st
from google import genai
from google.genai import types
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from prompts import SUMMARY_REQUEST_PROMPT, SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE


# --- Page configuration ----------------------------------------------------------

st.set_page_config(page_title="MacroSnap", page_icon="🥗")


# --- Configuration and secrets ---------------------------------------------------


def get_config(key: str, default: str = "") -> str:
    """Read configuration from environment variables or Streamlit secrets."""
    value = os.getenv(key)

    if value:
        return value.strip()

    try:
        secret_value = st.secrets.get(key, default)
        return str(secret_value).strip() if secret_value else default
    except Exception:
        # Streamlit secrets may not exist on Render.
        return default


MODEL_NAME = get_config("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_API_KEY = get_config("GEMINI_API_KEY")

TWILIO_ACCOUNT_SID = get_config("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = get_config("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = get_config("TWILIO_WHATSAPP_FROM")
TWILIO_CONTENT_SID = get_config("TWILIO_CONTENT_SID")

# Gemini is required to run the nutrition assistant.
if not GEMINI_API_KEY:
    st.error(
        "Missing GEMINI_API_KEY. Add it to your Render Environment settings "
        "or your local .streamlit/secrets.toml file."
    )
    st.stop()


# --- Constants -------------------------------------------------------------------

MAX_SUMMARY_CHARS = 900

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$", re.ASCII)

PHOTO_ONLY_PROMPT = "What is this meal? Give me the calories and macros."

EMPTY_REPLY_MESSAGE = (
    "Sorry, I couldn't come up with an answer for that. "
    "Try rephrasing it or sending a clearer photo."
)

NO_TEMPLATE_HINT = (
    "Without a Content Template, WhatsApp only delivers this within 24 hours "
    "of your last message to the sandbox. Message the sandbox number from "
    "your phone, then try again."
)

Role = Literal["user", "assistant"]
Kind = Literal["text", "image"]


class Message(TypedDict):
    """One chat message stored in st.session_state.messages."""

    role: Role
    kind: Kind
    content: str | bytes


# --- Cached API clients -----------------------------------------------------------


@st.cache_resource
def get_gemini_client(api_key: str) -> genai.Client:
    """Create and reuse the Gemini client."""
    return genai.Client(api_key=api_key)


@st.cache_resource
def get_twilio_client(account_sid: str, auth_token: str) -> TwilioClient:
    """Create and reuse the Twilio client."""
    return TwilioClient(account_sid, auth_token)


gemini_client = get_gemini_client(GEMINI_API_KEY)


# --- Helper functions -------------------------------------------------------------


def render_message(message: Message) -> None:
    """Render a text or image message in the chat."""
    with st.chat_message(message["role"]):
        if message["kind"] == "image":
            st.image(message["content"])
        else:
            st.write(message["content"])


def add_message(role: Role, kind: Kind, content: str | bytes) -> None:
    """Save a message and render it immediately."""
    message: Message = {
        "role": role,
        "kind": kind,
        "content": content,
    }
    st.session_state.messages.append(message)
    render_message(message)


def normalize_phone(raw: str) -> str | None:
    """Normalize an international phone number to E.164 format."""
    number = re.sub(r"[\s\-.()]", "", raw)
    return number if E164_PATTERN.fullmatch(number) else None


def ask_gemini(parts: list[types.Part | str]) -> tuple[bool, str]:
    """Send a message to the current Gemini conversation."""
    try:
        reply = st.session_state.chat.send_message(parts).text
    except Exception:
        return (
            False,
            "Sorry, something went wrong while contacting Gemini. "
            "Check your API key, model name, and network connection.",
        )

    if not reply or not reply.strip():
        return False, EMPTY_REPLY_MESSAGE

    return True, reply


def build_transcript(messages: list[Message]) -> str:
    """Convert chat history to a text transcript for summarization."""
    lines = []

    # The first message is the welcome message, so skip it.
    for message in messages[1:]:
        speaker = "User" if message["role"] == "user" else "MacroSnap"
        content = (
            "[meal photo]"
            if message["kind"] == "image"
            else str(message["content"])
        )
        lines.append(f"{speaker}: {content}")

    return "\n".join(lines)


def summarize_conversation() -> tuple[bool, str]:
    """Generate a WhatsApp-ready nutrition summary."""
    transcript = build_transcript(st.session_state.messages)

    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=(
                f"Conversation transcript:\n{transcript}\n\n"
                f"{SUMMARY_REQUEST_PROMPT}"
            ),
        )
        summary = response.text
    except Exception:
        return False, ""

    if not summary or not summary.strip():
        return False, ""

    return True, summary


def clean_whatsapp_text(text: str | None) -> str:
    """Normalize and limit the WhatsApp summary text."""
    cleaned = " ".join((text or "").split())

    if not cleaned:
        return "No nutrition summary available."

    if len(cleaned) > MAX_SUMMARY_CHARS:
        cleaned = cleaned[:MAX_SUMMARY_CHARS].rstrip() + "..."

    return cleaned


def send_whatsapp(
    to_number: str,
    user_name: str,
    summary: str,
) -> tuple[bool, str]:
    """Send a nutrition summary through Twilio WhatsApp."""
    if not all(
        [
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            TWILIO_WHATSAPP_FROM,
        ]
    ):
        return (
            False,
            "WhatsApp is not configured. Add TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM to Render's "
            "Environment settings.",
        )

    name = " ".join(user_name.split())
    cleaned = clean_whatsapp_text(summary)

    # Twilio expects a WhatsApp address with the whatsapp: prefix.
    sender = TWILIO_WHATSAPP_FROM.strip()
    if not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{sender}"

    recipient = f"whatsapp:{to_number}"

    try:
        twilio_client = get_twilio_client(
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
        )

        if TWILIO_CONTENT_SID:
            content_variables = json.dumps(
                {"1": name, "2": cleaned},
                ensure_ascii=False,
            )

            message = twilio_client.messages.create(
                from_=sender,
                to=recipient,
                content_sid=TWILIO_CONTENT_SID,
                content_variables=content_variables,
            )
        else:
            message = twilio_client.messages.create(
                from_=sender,
                to=recipient,
                body=f"Hi {name}, here's your MacroSnap summary: {cleaned}",
            )

    except TwilioRestException as error:
        code = f" {error.code}" if error.code else ""
        return False, f"Twilio error{code}: {error.msg}"

    except Exception:
        return (
            False,
            "Unable to send the WhatsApp message. Check your Twilio "
            "configuration and try again.",
        )

    return True, message.sid


def send_summary() -> None:
    """Summarize the conversation and send it through WhatsApp."""
    with st.spinner("Summarizing your day..."):
        summary_ok, summary = summarize_conversation()

        sent, info = False, ""

        if summary_ok:
            sent, info = send_whatsapp(
                st.session_state.whatsapp_number,
                st.session_state.name,
                summary,
            )

    if not summary_ok:
        st.error(
            "Couldn't create your summary. Check your Gemini configuration "
            "and try again."
        )
    elif sent:
        st.success("Sent! Check your WhatsApp 📲")
    else:
        st.error(f"Couldn't send that: {info}")

        if not TWILIO_CONTENT_SID and TWILIO_ACCOUNT_SID:
            st.info(NO_TEMPLATE_HINT)


# --- Onboarding -------------------------------------------------------------------

if "onboarded" not in st.session_state:
    st.title("🥗 MacroSnap")
    st.caption("Snap it. Track it. Text yourself the results.")

    with st.form("onboarding_form"):
        name = st.text_input("Your name", max_chars=50)

        number = st.text_input(
            "WhatsApp number (with country code)",
            placeholder="+91XXXXXXXXXX",
            help="This is the number MacroSnap will send your summary to.",
        )

        submitted = st.form_submit_button("Let's go 🚀")

    if submitted:
        name = name.strip()
        number = number.strip()
        normalized_number = normalize_phone(number)

        if not name or not number:
            st.warning("Please fill in both your name and WhatsApp number.")

        elif normalized_number is None:
            st.warning(
                "Enter the number in international format, "
                "for example +919876543210."
            )

        else:
            st.session_state.name = name
            st.session_state.whatsapp_number = normalized_number

            try:
                st.session_state.chat = gemini_client.chats.create(
                    model=MODEL_NAME,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    ),
                )
            except Exception:
                st.error(
                    "Couldn't start the Gemini conversation. Check your "
                    "GEMINI_API_KEY and GEMINI_MODEL settings."
                )
                st.stop()

            st.session_state.messages = []
            st.session_state.onboarded = True
            st.rerun()

    st.stop()


# --- Chat screen ------------------------------------------------------------------

header_col, button_col = st.columns(
    [5, 2],
    vertical_alignment="center",
)

with header_col:
    st.title("🥗 MacroSnap")

with button_col:
    send_clicked = st.button(
        "📤 Send to WhatsApp",
        disabled=len(st.session_state.messages) <= 2,
        width="stretch",
    )

st.caption(
    f"Logged in as {st.session_state.name} - "
    f"updates go to {st.session_state.whatsapp_number}"
)

st.caption(
    "Nutrition estimates are approximate and are not medical "
    "or dietary advice."
)

if send_clicked:
    send_summary()


if not st.session_state.messages:
    add_message(
        "assistant",
        "text",
        WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name),
    )
else:
    for message in st.session_state.messages:
        render_message(message)


# --- User chat input ---------------------------------------------------------------

user_input = st.chat_input(
    "Ask a question, or attach a photo of your meal",
    accept_file=True,
    file_type=["jpg", "jpeg", "png"],
    max_upload_size=10,
)

if user_input:
    photo = user_input.files[0] if user_input.files else None
    text = user_input.text.strip()
    parts: list[types.Part | str] = []

    if photo:
        photo_bytes = photo.getvalue()

        add_message("user", "image", photo_bytes)

        # Send the uploaded image directly to Gemini for analysis.
        parts.append(
            types.Part.from_bytes(
                data=photo_bytes,
                mime_type=photo.type or "image/jpeg",
            )
        )

    if text:
        add_message("user", "text", text)
        parts.append(text)

    elif photo:
        parts.append(PHOTO_ONLY_PROMPT)

    if parts:
        with st.spinner("Crunching the numbers..."):
            _, answer = ask_gemini(parts)

        add_message("assistant", "text", answer)

    # Refresh the page so the WhatsApp button updates immediately.
    st.rerun()
