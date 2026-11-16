# NOVA - Offline Space Ai Assistant

NOVA is an onboard assistant for the in-transit phase of a space mission. It answers crew questions from NASA manuals, watches simulated ship sensors, raises alerts, and talks with the crew by voice. Everything runs locally on a single Windows laptop with no internet connection.

## Features

- **Manual Q&A with citations.** Questions are answered only from the PDFs in `manuals/`, with the source file and page cited and the exact excerpts shown. If the manuals don't cover a question, NOVA says so instead of guessing.
- **Simulated ship telemetry.** Six sensors (CO2, O2, cabin pressure, cabin temperature, battery, coolant temperature) update every second in a sidebar dashboard.
- **Rule-based alerts.** CAUTION and WARNING levels are decided by plain threshold checks in code, never by the LLM. Warnings show a red banner and are spoken aloud.
- **Voice chat.** Push-to-talk in the Chat tab, and a hands-free Voice tab with a "Hey Nova" wake word, an animated orb, and spoken answers that can be interrupted by saying "stop".
- **Read-only by design.** NOVA never sends commands or changes anything. Any advice is labelled "Suggested - crew decides".
- **Fully offline.** The LLM, speech-to-text and text-to-speech all run on the laptop.

## Tech stack

| Part | Tool |
|---|---|
| UI | Streamlit |
| LLM | llama3.1:8b via Ollama |
| Manual search | BM25 (`rank-bm25`) over PDF chunks read with `pypdf` |
| Speech-to-text | faster-whisper (`base` model, CPU, int8) |
| Text-to-speech | pyttsx3 |
| Microphone | sounddevice |

## Requirements

- Windows (uses `winsound` and the Windows speech engine)
- Python 3.10
- [Ollama](https://ollama.com) with the `llama3.1:8b` model pulled
- A microphone and speakers

## Setup

```powershell
git clone <repo-url>
cd NOVA

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install streamlit ollama pypdf rank-bm25 faster-whisper pyttsx3 sounddevice

ollama pull llama3.1:8b
```

**First run only:** Whisper needs to download its model once. `voice.py` sets `HF_HUB_OFFLINE=1` to block all network access, so comment out that line, run the app once with internet access, then switch it back on.

## Running

1. Make sure Ollama is running in the background.
2. Put the manual PDFs in the `manuals/` folder (three NASA documents are included).
3. Start the app:

```powershell
streamlit run app.py
```

Note that Streamlit only auto-reloads `app.py`. Restart the app after editing `brain.py` or `voice.py`.

## Project structure

```
app.py               The page: Chat tab, Voice tab, sidebar dashboard, orb
brain.py             Manual loading, search, LLM calls, simulated sensors and alert levels
voice.py             Speech-to-text, text-to-speech, background mic listener
manuals/             NASA PDFs used as the knowledge base
.streamlit/          Streamlit config (usage stats disabled)
HOW_IT_WORKS.md      Detailed walkthrough of the design and internals
```

## Usage

**Chat tab**
- Type a question, or record one with the mic widget.
- Expand "Excerpts used" under any answer to see the exact manual text.
- Follow-ups such as "Where does it say that?" work.
- Tick "Speak answers" to have replies read aloud.

**Voice tab**
- Press "Start voice chat" or say "Hey Nova" to start recording.
- Recording ends when you press "End voice chat" or after 2 seconds of silence.
- Say "stop" or "Nova" while NOVA is talking to cut it off.
- The conversation is saved to the Chat log.

**Demo controls**
- "Inject CO2 scrubber fault" makes CO2 climb, reaching CAUTION after about 30 seconds and WARNING after about 65 seconds.
- "Clear fault" resets it.

## Configuration

| Setting | File | Default | Purpose |
|---|---|---|---|
| `MODEL_NAME` | brain.py | llama3.1:8b | Ollama model used for answers |
| `MIN_SCORE` | brain.py | 2.0 | Minimum search score before NOVA refuses to answer |
| `SENSORS` | brain.py | demo limits | Sensor ranges and alert thresholds |
| `MIC_THRESHOLD` | voice.py | 0.03 | Loudness that counts as speech |
| `SILENCE_SECONDS` | voice.py | 2.0 | Silence that ends a recording |
| `SPEECH_RATE` | voice.py | 220 | Speaking speed |
| `WAKE_WORDS` | voice.py | "hey nova" variants | Phrases that start recording |

## Known limitations

- Telemetry is simulated with made-up thresholds and is not connected to any real system. The limits are not real NASA values.
- The wake word and voice interruption work best in a quiet room. On laptop speakers, interrupting NOVA can take 1-2 seconds.
- Use a single browser tab. The mic listener and question queue are shared.
- BM25 is keyword search, so questions worded very differently from the manuals may miss. An LLM keyword-expansion step helps for typed questions.
- The 8B model occasionally drops the exact citation wording, but the excerpts dropdown always shows the real sources.

## More detail

See [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the full walkthrough of the answer pipeline, the alert system, the background voice listener, and a demo checklist.
