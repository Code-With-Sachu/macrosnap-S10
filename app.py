"""MacroSnap: snap it, track it, text yourself the results.

A Streamlit chat app. A student enters their name and WhatsApp number once,
asks Gemini about their meals (typed or photographed) for calorie and macro
estimates, then texts themselves a summary through Twilio's WhatsApp sandbox.
All state lives in st.session_state: one user per browser session, no database.

Run locally with:  streamlit run app.py
"""

import json
import re
from typing import Literal, TypedDict

import streamlit as st
from google import genai
from google.genai import types
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from prompts import SUMMARY_REQUEST_PROMPT, SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE

# --- Constants and page config ---------------------------------------------------

st.set_page_config(page_title="MacroSnap", page_icon="🥗")

MODEL_NAME = st.secrets.get("GEMINI_MODEL", "gemini-3.5-flash")

# WhatsApp rejects a template whose filled-in body is over 1,024 characters
# (Meta error 132005), so the summary variable is capped well below that.
MAX_SUMMARY_CHARS = 900

# E.164: "+", a non-zero digit, then 6-14 more digits (ASCII digits only).
E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$", re.ASCII)

PHOTO_ONLY_PROMPT = "What is this meal? Give me the calories and macros."
EMPTY_REPLY_MESSAGE = (
    "Sorry, I couldn't come up with an answer for that - try rephrasing it "
    "or sending a clearer photo."
)
NO_TEMPLATE_HINT = (
    "Without a Content Template, WhatsApp only delivers this within 24 hours "
    "of your last message to the sandbox. Message the sandbox number from "
    "your phone, then try again."
)

Role = Literal["user", "assistant"]
Kind = Literal["text", "image"]


class Message(TypedDict):
    """One chat message, as stored in st.session_state.messages."""

    role: Role
    kind: Kind
    content: str | bytes


# --- Secrets ---------------------------------------------------------------------
# Read only from st.secrets: .streamlit/secrets.toml locally, or the Secrets
# panel on Streamlit Community Cloud. Never hard-code, log or print these.

GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
TWILIO_ACCOUNT_SID = st.secrets["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = st.secrets["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = st.secrets["TWILIO_WHATSAPP_FROM"]
# Optional. When empty, send_whatsapp falls back to a plain-text message.
TWILIO_CONTENT_SID = st.secrets.get("TWILIO_CONTENT_SID", "")

# --- Cached API clients ----------------------------------------------------------
# Streamlit reruns this whole script on every interaction. A top-level
# genai.Client(...) would be rebuilt on each rerun and the old one
# garbage-collected, which closes its connection, so any chat created from it
# fails with "Cannot send a request, as the client has been closed."
# @st.cache_resource builds each client once and every rerun reuses it.


@st.cache_resource
def get_gemini_client() -> genai.Client:
    """Return the shared Gemini client, created once per server process."""
    return genai.Client(api_key=GEMINI_API_KEY)


@st.cache_resource
def get_twilio_client() -> TwilioClient:
    """Return the shared Twilio client, created once per server process."""
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


gemini_client = get_gemini_client()
twilio_client = get_twilio_client()

# --- Helper functions ------------------------------------------------------------


def render_message(message: Message) -> None:
    """Draw one stored message: text with st.write, photos with st.image."""
    with st.chat_message(message["role"]):
        if message["kind"] == "image":
            st.image(message["content"])
        else:
            st.write(message["content"])


def add_message(role: Role, kind: Kind, content: str | bytes) -> None:
    """Append a message to the chat history and draw it right away."""
    message: Message = {"role": role, "kind": kind, "content": content}
    st.session_state.messages.append(message)
    render_message(message)


def normalize_phone(raw: str) -> str | None:
    """Return the number in E.164 form (e.g. +919876543210), or None if invalid.

    Spaces, dashes, dots and parentheses are removed before the check.
    """
    number = re.sub(r"[\s\-.()]", "", raw)
    return number if E164_PATTERN.fullmatch(number) else None


def ask_gemini(parts: list[types.Part | str]) -> tuple[bool, str]:
    """Send parts to the ongoing chat and return (ok, reply).

    Every call reuses the same chat, so follow-ups like "how much protein was
    in that?" keep their context. An exception, or an empty reply such as a
    blocked response, returns ok=False with a friendly message instead.
    """
    try:
        reply = st.session_state.chat.send_message(parts).text
    except Exception as error:  # Any API or network failure becomes a chat message.
        return False, f"Sorry, something went wrong: {error}"
    if not reply or not reply.strip():
        return False, EMPTY_REPLY_MESSAGE
    return True, reply


def build_transcript(messages: list[Message]) -> str:
    """Turn the chat history into plain text for the summary request.

    Skips the welcome message (always the first one) and writes photos as
    "[meal photo]", so no image is uploaded a second time.
    """
    lines = []
    for message in messages[1:]:
        speaker = "User" if message["role"] == "user" else "MacroSnap"
        content = "[meal photo]" if message["kind"] == "image" else message["content"]
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def summarize_conversation() -> tuple[bool, str]:
    """Ask Gemini for a WhatsApp-ready recap of the meals discussed so far.

    Uses a one-off generate_content call over a text transcript rather than
    the chat, so the hidden summary prompt never enters the chat history
    (repeat sends don't double-count). Returns (ok, summary); ok is False on
    an exception or an empty reply.
    """
    transcript = build_transcript(st.session_state.messages)
    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=f"Conversation transcript:\n{transcript}\n\n{SUMMARY_REQUEST_PROMPT}",
        )
        summary = response.text
    except Exception:  # The caller shows one friendly error for every failure.
        return False, ""
    if not summary or not summary.strip():
        return False, ""
    return True, summary


def clean_whatsapp_text(text: str | None) -> str:
    """Prepare text for a WhatsApp template variable.

    Template variables can't contain newlines, tabs or long runs of spaces,
    so all whitespace collapses to single spaces. The result is capped at
    MAX_SUMMARY_CHARS (plus "...") to stay under the 1,024-character limit.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "No nutrition summary available."
    if len(cleaned) > MAX_SUMMARY_CHARS:
        cleaned = cleaned[:MAX_SUMMARY_CHARS].rstrip() + "..."
    return cleaned


def send_whatsapp(to_number: str, user_name: str, summary: str) -> tuple[bool, str]:
    """Text the summary to the user's WhatsApp through Twilio.

    With TWILIO_CONTENT_SID set, this sends the Content Template that
    business-initiated WhatsApp messages need. Without it, this sends a plain
    message, which WhatsApp only delivers within 24 hours of the user's last
    message to the sandbox. Returns (True, message_sid) or (False, error_text).
    """
    name = " ".join(user_name.split())
    cleaned = clean_whatsapp_text(summary)
    try:
        if TWILIO_CONTENT_SID:
            content_variables = json.dumps({"1": name, "2": cleaned}, ensure_ascii=False)
            message = twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=f"whatsapp:{to_number}",
                content_sid=TWILIO_CONTENT_SID,
                content_variables=content_variables,
            )
        else:
            message = twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=f"whatsapp:{to_number}",
                body=f"Hi {name}, here's your MacroSnap summary: {cleaned}",
            )
    except TwilioRestException as error:
        # str(error) adds terminal colour codes when Streamlit runs in a
        # terminal, so build a plain message from the code and text instead.
        code = f" {error.code}" if error.code else ""
        return False, f"Twilio error{code}: {error.msg}"
    except Exception as error:
        return False, str(error)
    return True, message.sid


def send_summary() -> None:
    """Handle a click on "📤 Send to WhatsApp": summarize, send, report.

    Twilio is only called when Gemini produced a real summary, so an error
    message is never texted to the user.
    """
    with st.spinner("Summarizing your day..."):
        summary_ok, summary = summarize_conversation()
        sent, info = False, ""
        if summary_ok:
            sent, info = send_whatsapp(
                st.session_state.whatsapp_number, st.session_state.name, summary
            )

    if not summary_ok:
        st.error("Couldn't create your summary - please try again.")
    elif sent:
        st.success("Sent! Check your WhatsApp 📲")
    else:
        st.error(f"Couldn't send that: {info}")
        if not TWILIO_CONTENT_SID:
            st.info(NO_TEMPLATE_HINT)


# --- Onboarding (once per session) -------------------------------------------------

if "onboarded" not in st.session_state:
    st.title("🥗 MacroSnap")
    st.caption("Snap it. Track it. Text yourself the results.")

    with st.form("onboarding_form"):
        name = st.text_input("Your name", max_chars=50)
        number = st.text_input(
            "WhatsApp number (with country code)",
            placeholder="+91XXXXXXXXXX",
            help="This is the number MacroSnap will text your summary to.",
        )
        submitted = st.form_submit_button("Let's go 🚀")

    if submitted:
        name, number = name.strip(), number.strip()
        normalized_number = normalize_phone(number)
        if not name or not number:
            st.warning("Please fill in both your name and WhatsApp number.")
        elif normalized_number is None:
            st.warning("Enter the number in international format, e.g. +919876543210.")
        else:
            st.session_state.name = name
            st.session_state.whatsapp_number = normalized_number
            st.session_state.chat = gemini_client.chats.create(
                model=MODEL_NAME,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            )
            st.session_state.messages = []
            st.session_state.onboarded = True
            st.rerun()

    st.stop()

# --- Chat screen -------------------------------------------------------------------

header_col, button_col = st.columns([5, 2], vertical_alignment="center")
with header_col:
    st.title("🥗 MacroSnap")
with button_col:
    # Disabled until there's a real exchange beyond the welcome message.
    send_clicked = st.button(
        "📤 Send to WhatsApp",
        disabled=len(st.session_state.messages) <= 2,
        width="stretch",
    )

st.caption(f"Logged in as {st.session_state.name} - updates go to {st.session_state.whatsapp_number}")
st.caption("Estimates are approximate and not medical or dietary advice.")

if send_clicked:
    send_summary()

if not st.session_state.messages:
    add_message("assistant", "text", WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name))
else:
    for message in st.session_state.messages:
        render_message(message)

user_input = st.chat_input(
    "Ask a question, or attach a photo of your meal",
    accept_file=True,
    file_type=["jpg", "jpeg", "png"],
    max_upload_size=10,  # MB per photo
)

if user_input:
    photo = user_input.files[0] if user_input.files else None
    text = user_input.text.strip()
    parts: list[types.Part | str] = []

    if photo:
        photo_bytes = photo.getvalue()
        add_message("user", "image", photo_bytes)
        # The vision step: Gemini reads the meal photo straight from these bytes.
        parts.append(
            types.Part.from_bytes(data=photo_bytes, mime_type=photo.type or "image/jpeg")
        )
    if text:
        add_message("user", "text", text)
        parts.append(text)
    elif photo:
        parts.append(PHOTO_ONLY_PROMPT)  # Hidden prompt, not shown in the UI.

    with st.spinner("Crunching the numbers..."):
        _, answer = ask_gemini(parts)
    add_message("assistant", "text", answer)

    # The Send button was drawn before this reply existed. Rerun so it is
    # enabled on the same screen instead of after the next interaction.
    st.rerun()
