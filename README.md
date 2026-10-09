# 🥗 MacroSnap

*Snap it. Track it. Text yourself the results.*

MacroSnap is a Streamlit chat app for students. You enter your name and WhatsApp number once, then type what you ate or attach a photo of your meal, and Gemini estimates the calories, protein, carbs and fat. One button texts a summary of the whole conversation to your WhatsApp through Twilio.

- One Gemini model (`gemini-3.5-flash` by default) handles both chat and photos.
- Twilio's WhatsApp sandbox delivers the summary.
- Everything lives in `st.session_state`: one user per browser session and no database.
- Estimates are approximate and not medical or dietary advice.

## Project structure

```
macrosnap/
├── app.py                      # the app
├── prompts.py                  # the AI's personality, kept separate from logic
├── requirements.txt            # app dependencies
├── requirements-dev.txt        # test dependencies (pytest)
├── README.md                   # this file
├── tests/
│   └── test_app.py             # AppTest tests with mocked Gemini and Twilio
├── .gitignore                  # keeps secrets.toml out of GitHub
└── .streamlit/
    └── secrets.toml.example    # template - copy to secrets.toml and fill in
```

## Setup

You need Python 3.10 or newer.

1. Create a virtual environment:

   ```bash
   python -m venv venv
   ```

   Activate it:
   - macOS/Linux: `source venv/bin/activate`
   - PowerShell: `.\venv\Scripts\Activate.ps1` (if it's blocked, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first)
   - cmd: `venv\Scripts\activate.bat`

   Then install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Get a Gemini API key: [aistudio.google.com](https://aistudio.google.com) → **Get API key**.
3. Sign up for Twilio at [twilio.com/try-twilio](https://www.twilio.com/try-twilio) and copy the **Account SID** and **Auth Token** from the Console.
4. In the Twilio Console, go to **Messaging → Try it out → Send a WhatsApp message**, and note the sandbox number and your join code (e.g. `join happy-tiger`).
5. From the phone you'll test with, send the join message to the sandbox number. This opt-in is required and lapses after about 72 hours of inactivity, so rejoin if sends start failing.
6. Go to **Messaging → Content Template Builder** and create a Text template with `{{1}}` (name) and `{{2}}` (summary), for example:

   ```
   Hi {{1}}, here's your MacroSnap summary:

   {{2}}
   ```

   Copy its Content SID (it starts with `HX`).

   **No template?** Set `TWILIO_CONTENT_SID = ""` and MacroSnap sends a plain message instead. WhatsApp only delivers a plain message within 24 hours of your last message to the sandbox (for example, right after joining).

7. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in your real values. `.gitignore` already keeps `secrets.toml` out of Git.

## Run

```bash
streamlit run app.py
```

Open http://localhost:8501. Onboard with the number that joined the sandbox, try a text question and then a photo, then click **📤 Send to WhatsApp**.

## Test

The tests use Streamlit's `AppTest` with mocked Gemini and Twilio clients, so they need no keys and send nothing:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## Deploy to Streamlit Community Cloud

1. Push the project to GitHub. Only `secrets.toml.example` should be tracked, and `git status` should never list `.streamlit/secrets.toml`.
2. Go to [share.streamlit.io](https://share.streamlit.io) and click **New app**.
3. Pick the repo, branch and `app.py`.
4. Under **Settings → Secrets**, paste the contents of your `secrets.toml`.
5. Click **Deploy**.

Anyone with your app's link can use it, and their chats and sends count against your Gemini key and Twilio account, so share the link carefully.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot send a request, as the client has been closed.` | The Gemini and Twilio clients must be created inside `@st.cache_resource` functions, as they are in `app.py`. A client created at the top level is rebuilt and closed on every rerun, which breaks the chat. |
| Twilio error **63015** | The number hasn't joined the sandbox, or its opt-in lapsed. Send the join message to the sandbox number again. |
| Twilio error **63016** | The send is outside WhatsApp's 24-hour window, so use the Content Template (`TWILIO_CONTENT_SID`). In the sandbox, sending any message to the sandbox number from your phone also reopens the window. |
| Template or variable errors (e.g. **132005**, text too long) | The filled-in template must stay under 1,024 characters, and variables can't contain line breaks, tabs or long runs of spaces. MacroSnap collapses the whitespace and caps the summary at 900 characters, so keep your template's own text short. |
| The app said "Sent!" but nothing arrived | "Sent!" means Twilio accepted the message. WhatsApp delivery errors such as 63015 and 63016 arrive afterwards, so check the message's error code in the Twilio Console's messaging logs (**Monitor → Logs → Messaging**). |
| The model is "not found" | Set `GEMINI_MODEL` in `secrets.toml` to a current Gemini Flash model that accepts images. |
| A secrets error when the app starts | `.streamlit/secrets.toml` is missing or incomplete. Copy it from the example and fill in every value. |

## Privacy

- Photos and messages are sent to the Gemini API. On the free tier, Google may use your inputs to improve its products, so don't upload anything sensitive.
- MacroSnap keeps your phone number only in session memory, and it's gone when the session ends. Twilio's message logs on your account do record the number and the summary.
