# brain.py - the thinking part of NOVA
# reads the manuals, searches them, keeps the simulated ship sensors, and asks the local LLM
# NOVA is read-only: nothing here sends a command to the ship, it only reads and suggests.

import os
import random

import streamlit as st
import ollama
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

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

# extra rules for the voice tab, so it sounds like a person talking instead of reading a manual out loud
VOICE_RULES = """
VOICE MODE: your reply will be spoken out loud, not read. These rules override the formatting rules above.
- Keep the whole reply under 30 words, like a calm, friendly crewmate talking. Use everyday words and contractions.
- Get straight to the point. Do NOT say "According to the manual".
- Never use square brackets and never mention file names or page numbers, the screen already shows the sources.
- Talk about the sensors naturally, like "Cabin pressure's 101 kPa, that's normal." Never say "LIVE SHIP STATUS".
- If you suggest something, end with the plain sentence: Suggested - crew decides.
Example: "Put your suits on right away, the ship will try to seal off the leak. Suggested - crew decides."
"""

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
        # keep the model loaded for an hour, reloading it takes a few seconds
        keep_alive="1h",
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
# voice=True means it's for the voice tab: skip the keyword step (faster) and use the short chatty style
def answer(question, previous_question="", previous_reply="", voice=False):
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
    # (voice skips the keyword step, it saves a whole LLM call)
    if not voice:
        search_text = search_text + " " + get_keywords(search_text)
    results = search(search_text)
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
    if voice:
        system_text = system_text + "\n" + VOICE_RULES

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
