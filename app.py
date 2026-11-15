import os

# turn this on later, once Whisper has downloaded its model one time
# os.environ["HF_HUB_OFFLINE"] = "1"

import streamlit as st
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

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

# simple search box, no LLM yet, just shows the matching chunks
question = st.text_input("Ask a question about the manuals")
if question:
    results = search(question)
    if len(results) == 0:
        st.info("No manuals loaded, so there is nothing to search.")
    for chunk, score in results:
        file_name = chunk[0]
        page_number = chunk[1]
        text = chunk[2]
        st.markdown("**" + file_name + "**, page " + str(page_number) + ", score " + str(round(score, 2)))
        st.write(text)
