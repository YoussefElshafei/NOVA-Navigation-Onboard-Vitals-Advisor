# NOVA is read-only. Nothing in this app writes to or commands the ship.
# It only reads the manuals and the simulated sensors, and gives advice.
# The crew makes every decision.

import os
import io
import hashlib
import threading
import random

# turn this on later, once Whisper has downloaded its model one time
# os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st
import ollama
from pypdf import PdfReader
from rank_bm25 import BM25Okapi
from faster_whisper import WhisperModel
import pyttsx3
import pythoncom  # comes with pyttsx3 on windows

MANUALS_FOLDER = "manuals"
CHUNK_WORDS = 150
MODEL_NAME = "llama3.1:8b"

# if the best search score is under this, we say "can't find it" instead of asking the LLM
MIN_SCORE = 2.0

NOT_FOUND = "I can't find that in the loaded manuals."

# the rules the LLM has to follow every time
SYSTEM_RULES = """You are NOVA, a read-only assistant on a spacecraft.
Rules:
- Answer ONLY using the manual excerpts and the LIVE SHIP STATUS below. Do not use outside knowledge.
- For questions about the ship's status, use ONLY the LIVE SHIP STATUS block and quote its numbers.
- If any sensor is CAUTION or WARNING, say so, and point to a matching manual excerpt if there is one.
- If the user asks you to DO something (shut down, vent, turn on, fire, reset), say you cannot take actions,
then give a suggestion labeled "Suggested - crew decides", with the manual citation if there is one.
- When you use the manual excerpts, start your answer with "According to the manual," and only say that once.
- Keep the answer to 2 sentences when possible, not counting the source line, since answers are read out loud.
- If you used any excerpts, end with one last line listing every excerpt label you used, each in its own square brackets, for example:
This came directly from [ochmo-tb-002-eclss.pdf p.9] and [ochmo-tb-004-carbon-dioxide.pdf p.8].
- If you give any advice, label it "Suggested - crew decides".
- Never claim you did anything yourself. You cannot control or change the ship.
- If the user asks a follow-up like "where does it say that?", use the previous answer and give its sources again in the same format.
- If the excerpts cover the question even partly, answer with what they say.
- Only if neither the excerpts nor the LIVE SHIP STATUS have anything relevant, reply exactly: I can't find that in the loaded manuals."""

# heads up: these limits are made-up demo numbers, NOT real NASA limits
SENSORS = {
    "co2":          {"start": 3000, "danger": "high", "caution": 5000, "warning": 7000, "unit": "ppm"},
    "o2":           {"start": 20.9, "danger": "low",  "caution": 19.5, "warning": 18,   "unit": "%"},
    "pressure":     {"start": 101,  "danger": "low",  "caution": 96,   "warning": 90,   "unit": "kPa"},
    "cabin_temp":   {"start": 22,   "danger": "high", "caution": 27,   "warning": 32,   "unit": "C"},
    "battery":      {"start": 90,   "danger": "low",  "caution": 30,   "warning": 15,   "unit": "%"},
    "coolant_temp": {"start": 18,   "danger": "high", "caution": 30,   "warning": 38,   "unit": "C"},
}

# if a question has one of these (or a sensor name), it's about the ship, so we don't refuse it
STATUS_WORDS = ["status", "how is", "how's", "current"]

# green / yellow / red dot for each level
EMOJI = {"OK": "🟢", "CAUTION": "🟡", "WARNING": "🔴"}


# read all the PDFs once and keep the chunks in memory
@st.cache_resource
def load_manuals():
    chunks = []
    pdf_count = 0

    # no folder means no manuals, just return nothing
    if not os.path.isdir(MANUALS_FOLDER):
        return chunks, pdf_count

    for file_name in sorted(os.listdir(MANUALS_FOLDER)):
        if not file_name.lower().endswith(".pdf"):
            continue
        pdf_count =pdf_count + 1
        reader = PdfReader(os.path.join(MANUALS_FOLDER, file_name))

        # go page by page so we remember where each chunk came from
        page_number = 1
        for page in reader.pages:
            text = page.extract_text() or ""
            words =text.split()

            # cut the page into pieces of about 150 words
            for start in range(0, len(words), CHUNK_WORDS):
                piece = " ".join(words[start:start + CHUNK_WORDS])
                chunks.append((file_name, page_number, piece))

            page_number= page_number + 1

    return chunks, pdf_count


# build the BM25 search index once from all the chunks
@st.cache_resource
def build_index():
    chunks, pdf_count = load_manuals()

    # BM25 crashes on an empty list, so bail out if there's nothing
    if len(chunks) == 0:
        return None

    # turn every chunk into a list of lowercase words
    tokenized = []
    for chunk in chunks:
        text = chunk[2]
        tokenized.append(text.lower().split())

    return BM25Okapi(tokenized)


# find the 3 chunks that best match the question
def search(question):
    chunks, pdf_count = load_manuals()
    index = build_index()
    if index is None:
        return []

    # score every chunk against the question words
    scores = index.get_scores(question.lower().split())

    # pair each score with its chunk number, then sort best first
    scored = []
    for i in range(len(scores)):
        scored.append((scores[i], i))
    scored.sort(reverse=True)

    # grab the top 3 matching chunks
    results = []
    for score, i in scored[:3]:
        results.append((chunks[i], score))
    return results


# send a system prompt and a user message to the local model, get its text back
def ask_llm(system_text, user_text):
    response = ollama.chat(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        # temperature 0 so it doesn't get creative
        options={"temperature": 0},
    )
    return response["message"]["content"]


# ask the LLM for 5 search words, since BM25 only matches exact words
def get_keywords(question):
    reply = ask_llm(
        "You turn questions into search keywords for a NASA manual. "
        "Reply with exactly 5 keywords separated by spaces. No numbering, no other text.",
        question,
    )

    # clean up the reply in case it added commas, numbers or bullets
    keywords = []
    for word in reply.replace(",", " ").split():
        word = word.strip(".:;-*\"'()").lower()
        if word == "" or word.isdigit():
            continue
        keywords.append(word)

    # if the LLM gave us nothing useful, just fall back to the question itself
    if len(keywords) == 0:
        return question

    # it sometimes gives more than 5, so only keep the first 5
    return " ".join(keywords[:5])


# all the reply logic lives here so later phases can reuse it
# previous_question and previous_reply are the last turn, so follow-ups like "where does it say that?" work
def answer(question, previous_question="", previous_reply=""):
    # status questions skip the refusal, and so does anything asked while an alarm is on
    # (so "what should I do?" still works during the CO2 fault)
    alerts = alert_sensors()
    status_question = is_status_question(question) or len(alerts) > 0

    # mix in the last question so a follow-up still finds the same pages,
    # plus any sensor that's acting up so its manual pages come up too
    search_text = previous_question + " " + question
    for name in alerts:
        search_text = search_text + " " + name.replace("_", " ")

    # search with the question AND the keywords, so we don't lose the original words
    keywords = get_keywords(search_text)
    results = search(search_text + " " + keywords)
    if len(results) == 0:
        return "No manuals loaded, so there is nothing to search.", []

    # best match is too weak, so refuse instead of letting the LLM guess
    best_score = results[0][1]
    if best_score < MIN_SCORE:
        if not status_question:
            return NOT_FOUND, []
        # weak match but it's about the ship, so answer from the live numbers with no excerpts
        results = []

    # glue the 3 excerpts together, each with its [file p.N] label
    excerpts_text = ""
    excerpts_used = []
    for chunk, score in results:
        label = "[" + chunk[0] + " p." + str(chunk[1]) + "]"
        excerpts_text = excerpts_text + label + "\n" + chunk[2] + "\n\n"
        excerpts_used.append(chunk)

    if excerpts_text == "":
        excerpts_text = "(none)\n"

    # the LLM gets the rules, the live sensor numbers, then the manual excerpts
    system_text = (
        SYSTEM_RULES
        + "\n\nLIVE SHIP STATUS:\n" + status_text()
        + "\n\nManual excerpts:\n\n" + excerpts_text
    )

    # give the LLM the last turn too, if there was one
    user_text = question
    if previous_question != "":
        user_text = (
            "Previous question: " + previous_question + "\n"
            + "Previous answer: " + previous_reply + "\n\n"
            + "New question: " + question
        )

    reply_text = ask_llm(system_text, user_text)

    # if the LLM refused, just show the refusal, not a made-up source line
    if NOT_FOUND in reply_text:
        return NOT_FOUND, []

    return reply_text, excerpts_used


# load the whisper speech-to-text model once, it's slow to load so we cache it
# the very first run downloads it (needs internet), after that it's all local
@st.cache_resource
def load_whisper():
    return WhisperModel("base", device="cpu", compute_type="int8")


# turn a voice recording into text, all on this laptop
def transcribe(audio_bytes):
    model = load_whisper()

    # we only talk english, so tell whisper that instead of making it guess
    segments, info = model.transcribe(io.BytesIO(audio_bytes), language="en")

    # whisper hands back the speech in little pieces, so glue them together
    text = ""
    for segment in segments:
        text = text + segment.text
    return text.strip()


# one shared lock so only one voice talks at a time
# two at once makes pyttsx3 crash with "run loop already started"
@st.cache_resource
def get_speak_lock():
    return threading.Lock()


# this runs in the background thread and does the actual talking
def say_out_loud(text, lock):
    # wait for our turn if something else is already talking
    with lock:
        # windows needs this in every new thread before it can use the voice
        pythoncom.CoInitialize()

        # make a NEW engine every time, reusing one tends to hang on windows
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()


# read text out loud in the background so the app doesn't freeze while it talks
def speak(text):
    lock = get_speak_lock()
    thread = threading.Thread(target=say_out_loud, args=(text, lock))
    thread.start()


# one place that handles a question, typed or spoken, so both look the same in the chat
def handle_question(text):
    # grab the last question and answer before we add the new one
    previous_question = ""
    previous_reply = ""
    messages = st.session_state["messages"]
    if len(messages) >= 2:
        previous_question = messages[-2]["content"]
        previous_reply = messages[-1]["content"]

    # show the question and save it in the history
    with st.chat_message("user"):
        st.write(text)
    st.session_state["messages"].append({"role": "user", "content": text})

    # get the answer, show it with its excerpts, and save it too
    with st.chat_message("assistant"):
        with st.spinner("NOVA is thinking..."):
            reply_text, excerpts_used = answer(text, previous_question, previous_reply)
        st.write(reply_text)
        show_excerpts(excerpts_used)
    st.session_state["messages"].append(
        {"role": "assistant", "content": reply_text, "excerpts": excerpts_used}
    )

    # read the reply out loud if the checkbox is on
    if speak_answers:
        speak(reply_text)


# little dropdown under an answer showing which excerpts it used
def show_excerpts(excerpts):
    if len(excerpts) == 0:
        return
    with st.expander("Excerpts used"):
        for chunk in excerpts:
            st.markdown("**" + chunk[0] + " p." + str(chunk[1]) + "**")
            st.write(chunk[2])


# compare a reading against that sensor's limits
def get_level(name, value):
    s = SENSORS[name]
    if s["danger"] == "high":
        if value >= s["warning"]:
            return "WARNING"
        if value >= s["caution"]:
            return "CAUTION"
    else:
        if value <= s["warning"]:
            return "WARNING"
        if value <= s["caution"]:
            return "CAUTION"
    return "OK"


# every sensor begins at its start number
def start_values():
    values = {}
    for name in SENSORS:
        values[name] = SENSORS[name]["start"]
    return values


# nudge every sensor a little, runs once a second
def step():
    values = st.session_state["values"]
    for name in SENSORS:
        start = SENSORS[name]["start"]
        if name == "battery":
            # battery only goes down, slowly
            values[name] = max(0, values[name] - 0.02)
        elif name == "co2" and st.session_state["co2_fault"]:
            # scrubber is broken so co2 keeps climbing
            values[name] = values[name] + 60 + random.uniform(-10, 10)
        else:
            # small wobble, pulled back toward normal so nothing alerts by accident
            wiggle = start * random.uniform(-0.002, 0.002)
            values[name] = values[name] + wiggle + (start - values[name]) * 0.1


# big numbers look cleaner without decimals
def nice_number(value):
    if value >= 100:
        return str(round(value))
    return str(round(value, 1))


# names of the sensors that are red right now
def sensors_at_warning():
    names = []
    for name in SENSORS:
        if get_level(name, st.session_state["values"][name]) == "WARNING":
            names.append(name)
    return names


# one line per sensor, this is what the LLM sees as LIVE SHIP STATUS
def status_text():
    lines = []
    for name in SENSORS:
        value = st.session_state["values"][name]
        unit = SENSORS[name]["unit"]
        level = get_level(name, value)
        lines.append(name + ": " + nice_number(value) + " " + unit + " (" + level + ")")
    return "\n".join(lines)


# names of the sensors that are yellow or red right now
def alert_sensors():
    names = []
    for name in SENSORS:
        if get_level(name, st.session_state["values"][name]) != "OK":
            names.append(name)
    return names


# true if the question is about the ship itself, so we skip the MIN_SCORE refusal
def is_status_question(question):
    q = question.lower()
    for word in STATUS_WORDS:
        if word in q:
            return True
    for name in SENSORS:
        if name in q or name.replace("_", " ") in q:
            return True
    return False


# reruns by itself every second without wiping the chat
@st.fragment(run_every=1)
def dashboard():
    step()
    for name in SENSORS:
        value = st.session_state["values"][name]
        level = get_level(name, value)
        st.metric(EMOJI[level] + " " + name, nice_number(value) + " " + SENSORS[name]["unit"])


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

warning_banner()

chunks, pdf_count = load_manuals()

# show what got loaded in the sidebar
st.sidebar.header("Manuals")
if pdf_count == 0:
    st.sidebar.warning("No PDFs found. Drop some NASA manuals into the manuals/ folder and restart the app.")
else:
    st.sidebar.write("PDFs loaded: " + str(pdf_count))
    st.sidebar.write("Chunks loaded: " + str(len(chunks)))

# live ship status and the demo fault buttons
with st.sidebar:
    st.header("Ship status (simulated)")
    dashboard()
    # on_click runs before the page redraws, so the change shows up right away
    st.button("Inject CO2 scrubber fault", on_click=inject_fault)
    st.button("Clear fault", on_click=clear_fault)

# voice settings, on by default
st.sidebar.header("Voice")
speak_answers = st.sidebar.checkbox("Speak answers", value=True)

# remember which recording we already used, so reruns don't send it again
if "last_audio_id" not in st.session_state:
    st.session_state["last_audio_id"] = ""

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

# keep the chat history between reruns
if "messages" not in st.session_state:
    st.session_state["messages"] = []

# draw all the old messages first
for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message["role"] == "assistant":
            # .get so older messages without excerpts don't crash
            show_excerpts(message.get("excerpts", []))

# typed questions and voice questions both go through handle_question
question = st.chat_input("Ask NOVA about the manuals")
st.caption("Read-only assistant: it advises, the crew decides.")
if question:
    handle_question(question)
if voice_question != "":
    handle_question(voice_question)
