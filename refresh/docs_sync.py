"""Refresh the Homes England programme documents in the Foundry vector store that the agent's file search reads.

Downloads each GOV.UK guidance page in SOURCES as Markdown and keeps one file per document in the vector store:
only documents whose content changed are re-uploaded, and documents no longer listed are removed.
Content is published under the Open Government Licence v3.0.

Runs daily as an Azure Function (function_app.py). Run locally (after `az login`): uv run refresh/docs_sync.py
"""
import hashlib
import json
import os
from urllib.request import urlopen

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from markdownify import markdownify

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT",
                          "https://ai-applications-uk-resource.services.ai.azure.com/api/projects/ai-applications-uk")
VECTOR_STORE = "he-programme-documents"  # agent.py looks the store up by this name
SOURCES = {
    "bil-fund": "guidance/brownfield-infrastructure-and-land-fund",
    "sahp-2026-2036": "guidance/social-and-affordable-homes-programme-sahp-2026-to-2036",
}


def fetch(doc_id, slug):
    """GOV.UK page -> Markdown with front matter."""
    with urlopen(f"https://www.gov.uk/api/content/{slug}", timeout=60) as r:
        page = json.load(r)
    meta = {"id": doc_id, "title": page["title"], "url": f"https://www.gov.uk/{slug}",
            "updated": page["public_updated_at"][:10], "summary": page["description"]}
    body = markdownify(page["details"]["body"], heading_style="ATX", strip=["abbr"])
    front = "\n".join(["---", *(f"{k}: {json.dumps(v)}" for k, v in meta.items()),
                       "licence: Open Government Licence v3.0", "---"])
    return f"{front}\n\n# {page['title']}\n\n{body.strip()}\n"


def vector_store(openai):
    for store in openai.vector_stores.list():
        if store.name == VECTOR_STORE:
            return store
    return openai.vector_stores.create(name=VECTOR_STORE)


def remove(openai, store, file):
    openai.vector_stores.files.delete(file.id, vector_store_id=store.id)
    openai.files.delete(file.id)


def sync():
    """Brings the vector store in line with SOURCES; returns what changed per document."""
    with DefaultAzureCredential() as credential, AIProjectClient(endpoint=ENDPOINT, credential=credential) as project:
        openai = project.get_openai_client()
        store = vector_store(openai)
        existing = {}  # doc id -> [(vector store file, content hash)]
        for f in openai.vector_stores.files.list(store.id):
            # Foundry drops vector store file attributes, so the filename carries the doc id and content hash
            doc_id, _, digest = openai.files.retrieve(f.id).filename.removesuffix(".md").partition("--")
            existing.setdefault(doc_id, []).append((f, digest))
        changes = {}
        for doc_id, slug in SOURCES.items():
            text = fetch(doc_id, slug)
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            old = existing.pop(doc_id, [])
            current = [f for f, d in old if d == digest]
            if not current:
                # upload the new version before removing the old one, so the document never drops out of search
                new = openai.vector_stores.files.upload_and_poll(vector_store_id=store.id,
                                                                 file=(f"{doc_id}--{digest}.md", text.encode()))
                if new.status != "completed":
                    raise RuntimeError(f"Indexing {doc_id} failed: {new.last_error}")
            for f, _ in old:
                if not current or f.id != current[0].id:  # older versions, and duplicates of the current one
                    remove(openai, store, f)
            changes[doc_id] = "unchanged" if current else "updated" if old else "added"
        for doc_id, old in existing.items():  # dropped from SOURCES
            for f, _ in old:
                remove(openai, store, f)
            changes[doc_id] = "removed"
        return {"vector_store": store.id, "documents": changes}


if __name__ == "__main__":
    print(json.dumps(sync(), indent=1))
