import os
import random

# turn this on later, once Whisper has downloaded its model one time
# os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

MANUALS_FOLDER = "manuals"
CHUNK_WORDS = 150

# heads up: these limits are made-up demo numbers, NOT real NASA limits
SENSORS = {
    "co2":          {"start": 3000, "danger": "high", "caution": 5000, "warning": 7000, "unit": "ppm"},
    "o2":           {"start": 20.9, "danger": "low",  "caution": 19.5, "warning": 18,   "unit": "%"},
    "pressure":     {"start": 101,  "danger": "low",  "caution": 96,   "warning": 90,   "unit": "kPa"},
    "cabin_temp":   {"start": 22,   "danger": "high", "caution": 27,   "warning": 32,   "unit": "C"},
    "battery":      {"start": 90,   "danger": "low",  "caution": 30,   "warning": 15,   "unit": "%"},
    "coolant_temp": {"start": 18,   "danger": "high", "caution": 30,   "warning": 38,   "unit": "C"},
}

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


# all the reply logic lives here so later phases can reuse it
# for now it just hands back the best chunk with its [file p.N] label
def answer(question):
    results = search(question)
    if len(results) == 0:
        return "No manuals loaded, so there is nothing to search.", []

    top_chunk = results[0][0]
    file_name = top_chunk[0]
    page_number = top_chunk[1]
    text = top_chunk[2]

    reply_text = text + " [" + file_name + " p." + str(page_number) + "]"
    excerpts_used = [top_chunk]
    return reply_text, excerpts_used


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

# keep the chat history between reruns
if "messages" not in st.session_state:
    st.session_state["messages"] = []

# draw all the old messages first
for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])

# when the user sends something, show it, answer it, and save both
question = st.chat_input("Ask NOVA about the manuals")
if question:
    with st.chat_message("user"):
        st.write(question)
    st.session_state["messages"].append({"role": "user", "content": question})

    reply_text, excerpts_used = answer(question)
    with st.chat_message("assistant"):
        st.write(reply_text)
    st.session_state["messages"].append({"role": "assistant", "content": reply_text})