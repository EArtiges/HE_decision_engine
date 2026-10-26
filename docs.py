"""Simulated RAG over the programme documents in docs/ (fetched by fetch_docs.py).

Documents are split into chunks by heading (long sections into overlapping windows) and searched with BM25.
"""
import math
import re
from collections import Counter
from pathlib import Path

import yaml

DOCS = Path(__file__).parent / "docs"
MAX_WORDS, OVERLAP, MIN_WORDS = 300, 60, 25
STOPWORDS = set("a an and are as at be by can for from has have how if in is it its of on or that the this to "
                "we what when where which who will with you your our not may must should".split())


def tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


def load():
    docs, chunks = [], []
    for path in sorted(DOCS.glob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = yaml.safe_load(front)
        docs.append({k: str(v).strip() for k, v in meta.items()} | {"words": len(body.split())})
        headings, text = [], []

        def flush(force=False):
            words = " ".join(text).split()
            if len(words) < MIN_WORDS and not force:
                return  # too short to stand alone (e.g. contact details): carried into the next section
            for start in range(0, max(len(words), 1), MAX_WORDS - OVERLAP):
                part = " ".join(words[start:start + MAX_WORDS])
                if part:
                    chunks.append({"document": meta["id"], "title": meta["title"], "url": meta["url"],
                                   # H1 is the document title: left out so it doesn't boost every chunk
                                   "section": " > ".join(h for lvl, h in headings if lvl > 1) or "Introduction",
                                   "text": part})
                if start + MAX_WORDS >= len(words):
                    break
            text.clear()

        for line in body.splitlines():
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                flush()
                level = len(m.group(1))
                headings[:] = [h for h in headings if h[0] < level] + [(level, m.group(2).strip())]
            else:
                text.append(line)
        flush(force=True)
    return docs, chunks


DOCUMENTS, CHUNKS = load()
TOKENS = [tokens(c["section"] + " " + c["text"]) for c in CHUNKS]
AVG_LEN = sum(map(len, TOKENS)) / max(len(TOKENS), 1)
DF = Counter(t for toks in TOKENS for t in set(toks))


def search(query, document=None, top_k=5, k1=1.5, b=0.75):
    """BM25 over chunks, optionally restricted to one document id."""
    q = tokens(query)
    scored = []
    for chunk, toks in zip(CHUNKS, TOKENS):
        if document and chunk["document"] != document:
            continue
        tf = Counter(toks)
        score = sum(
            math.log(1 + (len(CHUNKS) - DF[t] + 0.5) / (DF[t] + 0.5))
            * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(toks) / AVG_LEN))
            for t in q if t in tf)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda s: -s[0])
    return [{"score": round(s, 2)} | c for s, c in scored[:top_k]]
