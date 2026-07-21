import os
import re
import socket
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

# =====================================================
# ⚙️ Basic setup
# =====================================================
os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"
os.environ["LANGCHAIN_DISABLE_TELEMETRY"] = "true"
os.environ["CHROMA_TELEMETRY_ENABLED"] = "false"
os.environ["POSTHOG_DISABLED"] = "true"

data_folder = "data"
persist_dir = "engine_db"

# =====================================================
# 🧠 Embeddings (Ollama only)
# =====================================================
print("🧠 Using local Ollama embeddings (mxbai-embed-large, 1024-d).")
embeddings = OllamaEmbeddings(model="mxbai-embed-large")
chunk_size = 800
chunk_overlap = 100
print(f"🪶 Chunk size: {chunk_size}, overlap: {chunk_overlap}")

# =====================================================
# 📘 Load PDFs
# =====================================================
all_docs = []
for filename in os.listdir(data_folder):
    if filename.lower().endswith(".pdf"):
        file_path = os.path.join(data_folder, filename)
        print(f"📘 Loading new file: {filename}")
        loader = PyPDFLoader(file_path)
        docs = loader.load()

        for d in docs:
            d.metadata["source"] = filename
            d.metadata["page"] = d.metadata.get("page", "?")

        all_docs.extend(docs)

if not all_docs:
    print("⚠️ No PDF files found in 'data' folder!")
    exit()

# =====================================================
# ✂️ Split text into manageable chunks
# =====================================================
splitter = RecursiveCharacterTextSplitter(
    chunk_size=chunk_size,
    chunk_overlap=chunk_overlap,
    separators=["\n\n", "\n", ".", " "],
)
chunks = splitter.split_documents(all_docs)

valid_chunks = []
for doc in chunks:
    text = re.sub(r"[^ -~\n]", "", doc.page_content).strip()
    if 50 < len(text) < 3000:
        doc.page_content = text
        valid_chunks.append(doc)

print(f"🧩 Split into {len(valid_chunks)} clean chunks.")

# =====================================================
# 🧠 Build / update Chroma database
# =====================================================
print("⚙️ Adding documents to local Chroma database...")
db = Chroma(persist_directory=persist_dir, embedding_function=embeddings)
db.add_documents(valid_chunks)

print(f"✅ Successfully added {len(valid_chunks)} chunks to '{persist_dir}'")
print("🔗 Metadata (file + page) successfully embedded — ready for offline RAG.")
