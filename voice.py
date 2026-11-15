# voice.py - everything to do with sound
# whisper turns speech into text, pyttsx3 talks back, and a background thread listens to the mic

import os
import io
import threading
import time
import queue
import winsound  # little beep when you say "hey nova", built into python on windows

# offline mode so whisper never tries to download stuff
# has to be above the faster_whisper import, and the model needs to be downloaded once first
os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st
from faster_whisper import WhisperModel
import pyttsx3
import pythoncom  # comes with pyttsx3 on windows
import numpy as np  # comes with faster-whisper
import sounddevice as sd  # reads the mic for the voice tab

# voice settings
MIC_RATE = 16000          # whisper wants 16 kHz audio
MIC_THRESHOLD = 0.03      # how loud counts as talking (room noise here was about 0.01), raise it if noise sets it off
SILENCE_SECONDS = 2.0     # 2 seconds of quiet means you're done talking, so the recording stops
NO_SPEECH_SECONDS = 8     # pressed start but said nothing for this long, so give up
SPEECH_RATE = 220         # how fast NOVA talks, pyttsx3 default is about 200

# whisper often hears "nova" as something close, so accept those too
WAKE_WORDS = ["hey nova", "hi nova", "okay nova", "ok nova", "hey noah", "hey nover", "hey novah", "hey no va"]

# saying one of these while NOVA is talking cuts it off
STOP_WORDS = ["stop", "nova", "noah", "wait", "hold on", "be quiet"]


# load the whisper speech-to-text model once, it's slow so we cache it
# offline mode is on so it just loads it from the laptop
@st.cache_resource
def load_whisper():
    return WhisperModel("base", device="cpu", compute_type="int8")


# turn audio into text with whisper, works for a file or raw mic audio
def whisper_text(model, audio):
    # english only, beam_size 1 is a lot faster and still fine for short questions,
    # vad_filter skips the silent bits so whisper doesn't make up words from noise
    segments, info = model.transcribe(audio, language="en", beam_size=1, vad_filter=True)

    # whisper hands back the speech in little pieces, so glue them together
    text = ""
    for segment in segments:
        text = text + segment.text
    return text.strip()


# turn a push-to-talk recording into text, all on this laptop
def transcribe(audio_bytes):
    return whisper_text(load_whisper(), io.BytesIO(audio_bytes))


# everything the voice threads share: whether NOVA is talking, the stop flag,
# and the questions the mic heard that the page still has to answer
@st.cache_resource
def get_voice_state():
    return {
        "questions": queue.Queue(),
        "lock": threading.Lock(),  # only one voice at a time, two at once crashes pyttsx3
        "speaking": False,
        "speaking_text": "",
        "stop": False,
        "enabled": True,         # listen for "hey nova" (the sidebar checkbox)
        "error": "",
        "notice": "",            # little message under the orb, like "didn't catch that"
        "voice_mode": False,     # true while the voice tab is open, "hey nova" only works then
        "recording": False,      # true between start and end, everything you say goes into the question
        "finish_now": False,     # set by the end button, stops the recording straight away
        "transcribing": False,   # whisper is turning the recording into text
        "user_talking_at": 0,    # last time the mic heard you, makes the orb pulse
    }


# this runs in a background thread and does the actual talking
def say_out_loud(text, state):
    # wait for our turn if something else is already talking
    with state["lock"]:
        # windows needs this in every new thread before it can use the voice
        pythoncom.CoInitialize()

        state["stop"] = False
        state["speaking"] = True
        state["speaking_text"] = clean_words(text)

        # make a NEW engine every time, reusing one tends to hang on windows
        engine = pyttsx3.init()
        engine.setProperty("rate", SPEECH_RATE)

        # pyttsx3 calls this before every word, so we can cut it off mid-sentence
        def on_word(name, location, length):
            if state["stop"]:
                engine.stop()

        engine.connect("started-word", on_word)
        engine.say(text)
        engine.runAndWait()

        state["speaking"] = False


# read text out loud in the background so the app doesn't freeze while it talks
def speak(text):
    thread = threading.Thread(target=say_out_loud, args=(text, get_voice_state()))
    thread.start()


# drop the "This came directly from [...]" line and any [file p.N] bits, nobody wants those read out
def for_speaking(text):
    lines = []
    for line in text.split("\n"):
        if not line.strip().startswith("This came directly from"):
            lines.append(line)
    text = " ".join(lines)

    while "[" in text and "]" in text:
        start = text.find("[")
        end = text.find("]", start)
        if end == -1:
            break
        text = text[:start] + text[end + 1:]

    # removing "[...]" leaves a gap before the full stop, so tidy that up
    return text.replace(" .", ".").strip()


# lowercase and no punctuation, so "Hey, Nova!" matches "hey nova"
def clean_words(text):
    cleaned = ""
    for letter in text.lower():
        if letter.isalnum() or letter == " " or letter == "'":
            cleaned = cleaned + letter
        else:
            cleaned = cleaned + " "
    return " ".join(cleaned.split())


# if the text has a wake word, return whatever comes after it ("" if nothing), otherwise None
def after_wake_word(text):
    padded = " " + text + " "
    for wake in WAKE_WORDS:
        spot = padded.find(" " + wake + " ")
        if spot != -1:
            return padded[spot + len(wake) + 2:].strip()
    return None


# true if you said a stop word, but not one NOVA is saying herself right now
# (on laptop speakers the mic hears NOVA too, so her own words can't count)
def said_stop_word(text, nova_text):
    padded = " " + text + " "
    nova_padded = " " + nova_text + " "
    for word in STOP_WORDS:
        if " " + word + " " in padded and " " + word + " " not in nova_padded:
            return True
    return False


# the recording is over (2 s of quiet or the end button), turn it into a question
def finish_recording(state, model, recording, talked):
    state["recording"] = False
    state["finish_now"] = False

    # pressed start but never said anything, so just quietly stop
    if not talked or len(recording) == 0:
        state["notice"] = "Didn't hear anything. Press Start or say \"Hey Nova\" to try again."
        return

    state["transcribing"] = True
    text = whisper_text(model, np.concatenate(recording))
    state["transcribing"] = False

    if text == "":
        state["notice"] = "Didn't catch that, try again."
        return
    state["questions"].put(text)


# check a stretch of talking for "hey nova", only used while the voice tab is open
def check_wake_word(state, model, audio):
    rest = after_wake_word(clean_words(whisper_text(model, audio)))
    if rest is None:
        return  # no "hey nova", it's just people talking, ignore it

    winsound.Beep(880, 120)
    if rest == "":
        # just "hey nova", so start recording the actual question now
        state["notice"] = ""
        state["recording"] = True
    else:
        # "hey nova, what's the co2" in one go, only the part after "hey nova" counts
        state["questions"].put(rest[0].upper() + rest[1:])


# runs forever in the background and does one of three jobs:
#   1. NOVA is talking: only listen for "stop" so you can cut in
#   2. recording (after start or "hey nova"): keep everything until 2 s of quiet or the end button
#   3. voice tab open: wait for "hey nova", everything else is ignored
def listen_forever(state, model):
    mic_queue = queue.Queue()
    block = int(MIC_RATE * 0.1)  # 0.1 seconds of audio at a time

    # sounddevice calls this every 0.1 s with fresh mic audio, we just stash it
    def on_audio(indata, frames, time_info, status):
        mic_queue.put(indata[:, 0].copy())

    try:
        stream = sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32",
                                blocksize=block, callback=on_audio)
        stream.start()
    except Exception as error:
        state["error"] = "Mic not available: " + str(error)
        return

    recording = []      # the audio we're collecting right now
    quiet_blocks = 0    # how many quiet 0.1 s pieces in a row
    talked = False      # did you actually say something since recording started
    was_recording = False
    was_speaking = False
    lead_in = []        # the last 0.3 s of audio, so we don't cut off the start of a word

    while True:
        sound = mic_queue.get()

        # how loud this little piece is
        loudness = np.sqrt(np.mean(sound * sound))
        loud = loudness > MIC_THRESHOLD
        if loud and not state["speaking"]:
            state["user_talking_at"] = time.time()  # makes the orb pulse

        # start or "hey nova" just switched recording on, or NOVA just stopped talking,
        # so begin with a clean slate (we don't want NOVA's own voice in your question)
        nova_just_stopped = was_speaking and not state["speaking"]
        if (state["recording"] and not was_recording) or nova_just_stopped:
            recording = []
            quiet_blocks = 0
            talked = False
        was_recording = state["recording"]
        was_speaking = state["speaking"]

        # job 1: NOVA is talking, the mic hears her too, so only check for stop words every 1.5 s
        if state["speaking"]:
            recording.append(sound)
            if len(recording) >= 15:
                text = clean_words(whisper_text(model, np.concatenate(recording)))
                if said_stop_word(text, state["speaking_text"]):
                    state["stop"] = True
                recording = recording[-5:]  # keep a little overlap so we don't cut a word in half
            continue

        # job 2: recording your question
        if state["recording"]:
            recording.append(sound)
            if loud:
                talked = True
                quiet_blocks = 0
            else:
                quiet_blocks = quiet_blocks + 1

            pressed_end = state["finish_now"]
            went_quiet = talked and quiet_blocks * 0.1 >= SILENCE_SECONDS
            never_talked = not talked and len(recording) * 0.1 >= NO_SPEECH_SECONDS
            if pressed_end or went_quiet or never_talked:
                # pressing end counts even if it was too quiet to set off the loudness check
                finish_recording(state, model, recording, talked or pressed_end)
                recording = []
                was_recording = False
            continue

        # job 3: voice tab open, wait for "hey nova"
        if state["voice_mode"] and state["enabled"]:
            if loud:
                # just started talking, so add the 0.3 s before too,
                # otherwise the soft "h" in "hey" gets cut off and whisper hears "pain over"
                if len(recording) == 0:
                    recording = lead_in[:]
                recording.append(sound)
                quiet_blocks = 0
            elif len(recording) > 0:
                recording.append(sound)
                quiet_blocks = quiet_blocks + 1

            # remember the last 3 quiet pieces for the lead-in above
            lead_in.append(sound)
            if len(lead_in) > 3:
                lead_in.pop(0)

            # only keep the last 10 seconds, so long background chatter doesn't pile up
            if len(recording) > 100:
                recording = recording[-100:]

            # you stopped talking for 2 s, so see if that stretch had "hey nova" in it
            if len(recording) > 0 and quiet_blocks * 0.1 >= SILENCE_SECONDS:
                check_wake_word(state, model, np.concatenate(recording))
                recording = []
                quiet_blocks = 0
            continue

        # on the chat tab with NOVA quiet, the mic ignores everything
        recording = []
        quiet_blocks = 0


# start the mic thread once for the whole app
@st.cache_resource
def start_listener():
    thread = threading.Thread(target=listen_forever, args=(get_voice_state(), load_whisper()), daemon=True)
    thread.start()
    return thread


# start button: cut NOVA off if she's talking, then record everything you say
def start_recording():
    state = get_voice_state()
    state["stop"] = True
    state["notice"] = ""
    state["recording"] = True


# end button: stop recording right now and send it, no need to wait for 2 s of quiet
def end_recording():
    get_voice_state()["finish_now"] = True
