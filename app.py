import os

# turn this on later, once Whisper has downloaded its model one time
# os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st

# just the page header for now
st.title("Offline Space Copilot")
st.caption("NOVA runs fully offline on this laptop. No internet, no cloud.")
