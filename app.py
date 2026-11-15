import os
import io
import hashlib
import threading

# turn this on later, once Whisper has downloaded its model one time
# os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st
from pypdf import PdfReader
from rank_bm25 import BM25Okapi
from faster_whisper import WhisperModel
import pyttsx3
import pythoncom  # comes with pyttsx3 on windows

MANUALS_FOLDER = "manuals"
CHUNK_WORDS = 150


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
    # show the question and save it in the history
    with st.chat_message("user"):
        st.write(text)
    st.session_state["messages"].append({"role": "user", "content": text})

    # get the answer, show it, and save it too
    reply_text, excerpts_used = answer(text)
    with st.chat_message("assistant"):
        st.write(reply_text)
    st.session_state["messages"].append({"role": "assistant", "content": reply_text})

    # read the reply out loud if the checkbox is on
    if speak_answers:
        speak(reply_text)


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

chunks, pdf_count = load_manuals()

# show what got loaded in the sidebar
st.sidebar.header("Manuals")
if pdf_count == 0:
    st.sidebar.warning("No PDFs found. Drop some NASA manuals into the manuals/ folder and restart the app.")
else:
    st.sidebar.write("PDFs loaded: " + str(pdf_count))
    st.sidebar.write("Chunks loaded: " + str(len(chunks)))

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

# typed questions and voice questions both go through handle_question
question = st.chat_input("Ask NOVA about the manuals")
if question:
    handle_question(question)
if voice_question != "":
    handle_question(voice_question)
