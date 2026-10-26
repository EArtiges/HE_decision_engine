"""One-off: download Homes England programme guidance from GOV.UK as Markdown into docs/.

The agent searches these documents (see docs.py) when the user asks about a programme or fund.
Content is published under the Open Government Licence v3.0.

Run: uv run fetch_docs.py
"""
import json
from pathlib import Path
from urllib.request import urlopen

from markdownify import markdownify

DOCS = Path(__file__).parent / "docs"
SOURCES = {
    "bil-fund": "guidance/brownfield-infrastructure-and-land-fund",
    "sahp-2026-2036": "guidance/social-and-affordable-homes-programme-sahp-2026-to-2036",
}


def main():
    DOCS.mkdir(exist_ok=True)
    for doc_id, slug in SOURCES.items():
        with urlopen(f"https://www.gov.uk/api/content/{slug}", timeout=60) as r:
            page = json.load(r)
        body = markdownify(page["details"]["body"], heading_style="ATX", strip=["abbr"])
        front = "\n".join([
            "---",
            f"id: {doc_id}",
            f"title: {json.dumps(page['title'])}",
            f"url: https://www.gov.uk/{slug}",
            f"updated: {page['public_updated_at'][:10]}",
            f"summary: {json.dumps(page['description'])}",
            "licence: Open Government Licence v3.0",
            "---",
        ])
        (DOCS / f"{doc_id}.md").write_text(f"{front}\n\n# {page['title']}\n\n{body.strip()}\n")
        print(f"{doc_id}: {len(body.split())} words")


if __name__ == "__main__":
    main()
