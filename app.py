# app.py - the page itself: the chat tab, the voice tab and the sidebar
# run it with: streamlit run app.py
#
# NOVA is read-only. Nothing in this app writes to or commands the ship.
# It only reads the manuals and the simulated sensors, and gives advice.
# The crew makes every decision.

import html
import hashlib
import time

import streamlit as st

# voice.py has to be imported first, it switches on offline mode before whisper loads
from voice import (get_voice_state, start_listener, start_recording, end_recording,
                   speak, for_speaking, transcribe)
from brain import (SENSORS, load_manuals, answer, get_level, start_values, step,
                   nice_number, sensors_at_warning)


# one place that handles a question, typed or spoken, so everything ends up in the same chat history
# on_voice means we're on the voice page, so don't draw chat bubbles, just save and talk
def handle_question(text, on_voice=False):
    # grab the last question and answer before we add the new one
    # (skip the "voice chat ended" notes, they're not part of the conversation)
    turns = []
    for message in st.session_state["messages"]:
        if message["role"] != "note":
            turns.append(message)
    previous_question = ""
    previous_reply = ""
    if len(turns) >= 2:
        previous_question = turns[-2]["content"]
        previous_reply = turns[-1]["content"]

    st.session_state["messages"].append({"role": "user", "content": text})

    if on_voice:
        # voice page: the orb shows we're thinking, so just get the answer
        reply_text, excerpts_used = answer(text, previous_question, previous_reply, voice=True)
    else:
        # chat page: show the question, then the answer with its excerpts
        with st.chat_message("user"):
            st.write(text)
        with st.chat_message("assistant"):
            with st.spinner("NOVA is thinking..."):
                reply_text, excerpts_used = answer(text, previous_question, previous_reply)
            st.write(reply_text)
            show_excerpts(excerpts_used)

    st.session_state["messages"].append(
        {"role": "assistant", "content": reply_text, "excerpts": excerpts_used}
    )

    # read the reply out loud, always on the voice page, otherwise only if the checkbox is on
    if speak_answers or on_voice:
        speak(for_speaking(reply_text))


# little dropdown under an answer showing which excerpts it used
def show_excerpts(excerpts):
    if len(excerpts) == 0:
        return
    with st.expander("Excerpts used"):
        for chunk in excerpts:
            st.markdown("**" + chunk[0] + " p." + str(chunk[1]) + "**")
            st.write(chunk[2])


# reruns by itself every second without wiping the chat
@st.fragment(run_every=1)
def dashboard():
    step()
    for name in SENSORS:
        value = st.session_state["values"][name]
        level = get_level(name, value)
        # plain text level next to the name, no emoji
        st.metric(name + " (" + level + ")", nice_number(value) + " " + SENSORS[name]["unit"])

        # say it out loud once when a sensor first goes red (rules only, no LLM)
        announced = st.session_state["announced"]
        if level == "WARNING" and name not in announced:
            announced.add(name)
            if speak_alerts:
                spoken_name = name.replace("_", " ").upper()
                unit = SENSORS[name]["unit"]
                speak("Warning. " + spoken_name + " at " + nice_number(value) + " " + unit + ".")

        # back to normal, so it's allowed to warn again next time
        if level == "OK" and name in announced:
            announced.remove(name)


# the big circle on the voice page, mood is "nova", "user", "listening", "thinking" or "still"
def draw_orb(mood, label):
    st.markdown('<div class="nova-orb ' + mood + '"></div>', unsafe_allow_html=True)
    st.markdown('<p class="orb-label">' + label + "</p>", unsafe_allow_html=True)


# the voice tab: orb, start/end buttons, redrawn 4 times a second so the orb reacts right away
@st.fragment(run_every=0.25)
def voice_panel():
    state = get_voice_state()
    you_are_talking = time.time() - state["user_talking_at"] < 0.4

    if state["speaking"]:
        draw_orb("nova", 'NOVA is talking. Press Start or say "stop" to cut in.')
    elif state["transcribing"]:
        draw_orb("thinking", "Thinking...")
    elif state["recording"] and you_are_talking:
        draw_orb("user", "Listening... press End when you're done.")
    elif state["recording"]:
        draw_orb("listening", "Listening... press End when you're done.")
    else:
        draw_orb("still", 'Press Start or say "Hey Nova".')

    left, right = st.columns(2)
    left.button("Start voice chat", on_click=start_recording, disabled=state["recording"], width="stretch")
    right.button("End voice chat", on_click=end_recording, disabled=not state["recording"], width="stretch")

    if state["error"] != "":
        st.caption(state["error"])
    elif state["notice"] != "":
        st.caption(state["notice"])

    # the mic turned your question into text, so rerun the whole page to answer and save it
    if not state["questions"].empty():
        st.session_state["mic_question"] = state["questions"].get()
        st.rerun()


# red box at the top when anything hits WARNING
@st.fragment(run_every=1)
def warning_banner():
    names = sensors_at_warning()
    if len(names) > 0:
        st.error("WARNING: " + ", ".join(names))


# demo button: pretend the CO2 scrubber just broke
def inject_fault():
    st.session_state["co2_fault"] = True


# demo button: fix the scrubber and put CO2 back to normal
def clear_fault():
    st.session_state["co2_fault"] = False
    st.session_state["values"]["co2"] = SENSORS["co2"]["start"]


# hide the default person and robot icons next to chat messages
st.markdown(
    """
    <style>
    [data-testid="stChatMessageAvatarUser"],
    [data-testid="stChatMessageAvatarAssistant"] {
        display: none;
    }

    /* the voice page orb, it only animates while someone is talking */
    .nova-orb {
        width: 200px;
        height: 200px;
        border-radius: 50%;
        margin: 60px auto 28px auto;
        background: radial-gradient(circle at 35% 30%, #f4f8ff 0%, #a9c8ff 30%, #4d7fe0 65%, #23408f 100%);
        box-shadow: 0 0 60px rgba(90, 140, 255, 0.25);
    }
    .nova-orb.nova {
        animation: orb-pulse 1.1s ease-in-out infinite;
    }
    .nova-orb.user {
        animation: orb-pulse 0.7s ease-in-out infinite;
    }
    .nova-orb.thinking {
        opacity: 0.6;
    }
    /* recording but quiet: a still ring around it, no pulse */
    .nova-orb.listening {
        box-shadow: 0 0 0 6px rgba(120, 160, 255, 0.35), 0 0 60px rgba(90, 140, 255, 0.25);
    }
    @keyframes orb-pulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.1); }
    }
    .orb-label {
        text-align: center;
        opacity: 0.7;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Offline Space Copilot, NOVA")
st.caption("NOVA runs fully offline on this laptop. No internet, no cloud.")

# sensor readings and the fault switch, set up once
if "values" not in st.session_state:
    st.session_state["values"] = start_values()
if "co2_fault" not in st.session_state:
    st.session_state["co2_fault"] = False
# sensors we already said a warning for, so we don't repeat it every second
if "announced" not in st.session_state:
    st.session_state["announced"] = set()

warning_banner()

chunks, pdf_count = load_manuals()

st.sidebar.caption("Offline mode: everything runs on this device")

# show what got loaded in the sidebar
st.sidebar.header("Manuals")
if pdf_count == 0:
    st.sidebar.warning("No PDFs found. Drop some NASA manuals into the manuals/ folder and restart the app.")
else:
    st.sidebar.write("PDFs loaded: " + str(pdf_count))
    st.sidebar.write("Chunks loaded: " + str(len(chunks)))

# voice settings, on by default
# (these have to come before the dashboard, since it checks speak_alerts)
st.sidebar.header("Voice")
speak_answers = st.sidebar.checkbox("Speak answers", value=True)
speak_alerts = st.sidebar.checkbox("Speak alerts", value=True)
hey_nova_on = st.sidebar.checkbox('Listen for "Hey Nova" on the Voice tab', value=True)

# start the mic thread in the background (only happens once), and tell it if "hey nova" is on
start_listener()
get_voice_state()["enabled"] = hey_nova_on

# live ship status and the demo fault buttons
with st.sidebar:
    st.header("Ship status (simulated)")
    dashboard()
    # on_click runs before the page redraws, so the change shows up right away
    st.button("Inject CO2 scrubber fault", on_click=inject_fault)
    st.button("Clear fault", on_click=clear_fault)

# keep the chat history between reruns
if "messages" not in st.session_state:
    st.session_state["messages"] = []
# remember which recording we already used, so reruns don't send it again
if "last_audio_id" not in st.session_state:
    st.session_state["last_audio_id"] = ""
# how many questions were answered in the current voice chat
if "voice_turns" not in st.session_state:
    st.session_state["voice_turns"] = 0

# two tabs like chatgpt, only you switch between them (on_change="rerun" so .open is always right)
chat_tab, voice_tab = st.tabs(["Chat", "Voice"], key="page", on_change="rerun")

# the mic only listens for "hey nova" while the voice tab is open
voice_state = get_voice_state()
voice_state["voice_mode"] = voice_tab.open

# just left the voice page, so stop any recording, and mark the chat log like chatgpt does
if chat_tab.open and st.session_state.get("last_page", "") == "Voice":
    voice_state["recording"] = False
    if st.session_state["voice_turns"] > 0:
        st.session_state["messages"].append({"role": "note", "content": "Voice chat ended"})
    st.session_state["voice_turns"] = 0

if voice_tab.open:
    st.session_state["last_page"] = "Voice"
else:
    st.session_state["last_page"] = "Chat"

# ---------- chat page ----------
if chat_tab.open:
    with chat_tab:
        # push to talk: record a question with the mic
        voice_question = ""
        audio = st.audio_input("Ask NOVA with your voice")
        if audio is not None:
            audio_bytes = audio.getvalue()

            # hash the recording to get a fingerprint, same audio = same fingerprint
            audio_id = hashlib.md5(audio_bytes).hexdigest()

            # only run whisper when it's a new recording
            if audio_id != st.session_state["last_audio_id"]:
                with st.spinner("Turning your voice into text..."):
                    voice_question = transcribe(audio_bytes)
                st.session_state["last_audio_id"] = audio_id

                # whisper heard nothing, so let the user know
                if voice_question == "":
                    st.caption("Didn't catch that, try recording again.")

        # draw all the old messages first, voice chats included
        for message in st.session_state["messages"]:
            if message["role"] == "note":
                st.caption(message["content"])
                continue
            with st.chat_message(message["role"]):
                st.write(message["content"])
                if message["role"] == "assistant":
                    # .get so older messages without excerpts don't crash
                    show_excerpts(message.get("excerpts", []))

    # outside the tab so the box stays pinned to the bottom of the screen
    question = st.chat_input("Ask NOVA about the manuals")
    st.caption("Read-only assistant: it advises, the crew decides.")

    # typed questions and push-to-talk questions both go through handle_question
    with chat_tab:
        if question:
            handle_question(question)
        if voice_question != "":
            handle_question(voice_question)

# ---------- voice page ----------
if voice_tab.open:
    with voice_tab:
        mic_question = st.session_state.get("mic_question", "")
        if mic_question != "":
            # a question came in, so show a dim still orb while NOVA works on it
            st.session_state["mic_question"] = ""
            draw_orb("thinking", "Thinking...")
            handle_question(mic_question, on_voice=True)
            st.session_state["voice_turns"] = st.session_state["voice_turns"] + 1
            # redraw so the live orb takes over again while NOVA talks
            st.rerun()

        voice_panel()

        # show what NOVA heard last in this voice chat, handy if whisper got a word wrong
        if st.session_state["voice_turns"] > 0:
            for message in reversed(st.session_state["messages"]):
                if message["role"] == "user":
                    # html.escape so odd characters in what you said can't break the page
                    heard = html.escape(message["content"])
                    st.markdown('<p class="orb-label">You said: "' + heard + '"</p>', unsafe_allow_html=True)
                    break
