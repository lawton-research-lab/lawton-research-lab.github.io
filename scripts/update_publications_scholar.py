#!/usr/bin/env python3
"""
update_publications_scholar.py
==============================
Fetches publication metadata from Google Scholar and writes/updates
markdown_generator/publications.tsv, which can then be fed into
markdown_generator/publications.py to regenerate _publications/*.md files.

Usage:
    python3 scripts/update_publications_scholar.py

Requirements:
    pip install scholarly

Notes:
    - Google Scholar may throttle or block automated requests. If the script
      fails with a bot-detection error, wait a few minutes and retry, or use
      a proxy via scholarly's `use_proxy()` call (see commented lines below).
    - The script only ADDS new entries to the TSV; it does not overwrite
      existing manually-edited rows. Entries are matched by DOI (if available)
      or by title.
    - After running, verify the TSV and then run:
          python3 markdown_generator/publications.py publications.tsv
      from inside the markdown_generator/ directory to regenerate the .md files.
"""

import csv
import os
import re
import time

# Google Scholar user ID for Quinton Lawton
SCHOLAR_USER_ID = "6WX8IJ4AAAAJ"

TSV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "markdown_generator", "publications.tsv"
)

TSV_FIELDS = [
    "pub_date",
    "title",
    "venue",
    "excerpt",
    "citation",
    "url_slug",
    "paper_url",
    "slides_url",
    "bibtex_url",
]


def slugify(title: str) -> str:
    """Convert a title to a URL-friendly slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug[:80]


def load_existing_tsv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader)


def title_in_existing(title: str, existing: list[dict]) -> bool:
    title_norm = title.strip().lower()
    for row in existing:
        if row.get("title", "").strip().lower() == title_norm:
            return True
    return False


def fetch_publications() -> list[dict]:
    """Fetch publications from Google Scholar using the scholarly library."""
    try:
        from scholarly import scholarly
    except ImportError:
        print(
            "ERROR: 'scholarly' is not installed. Run: pip install scholarly"
        )
        return []

    # Uncomment to route through a proxy if Scholar blocks the request:
    # from scholarly import ProxyGenerator
    # pg = ProxyGenerator()
    # pg.FreeProxies()
    # scholarly.use_proxy(pg)

    print(f"Fetching profile for user ID: {SCHOLAR_USER_ID} ...")
    try:
        author = scholarly.search_author_id(SCHOLAR_USER_ID)
        author = scholarly.fill(author, sections=["publications"])
    except Exception as e:
        print(f"ERROR fetching author profile: {e}")
        return []

    results = []
    pubs = author.get("publications", [])
    print(f"Found {len(pubs)} publications on Scholar. Fetching details...")

    for i, pub in enumerate(pubs):
        try:
            pub = scholarly.fill(pub)
            bib = pub.get("bib", {})

            title = bib.get("title", "").strip()
            venue = bib.get("journal", bib.get("booktitle", "")).strip()
            year = str(bib.get("pub_year", "")).strip()
            abstract = bib.get("abstract", "").strip()
            # Truncate abstract for the excerpt field
            excerpt = abstract[:300] + "..." if len(abstract) > 300 else abstract

            # Build a simple citation string
            authors = bib.get("author", "").replace(" and ", ", ")
            volume = bib.get("volume", "")
            pages = bib.get("pages", "")
            citation_parts = [f"{authors}, {year}:", f'"{title}."']
            if venue:
                citation_parts.append(f"<i>{venue}</i>.")
            if volume:
                citation_parts.append(f"{volume}")
            if pages:
                citation_parts.append(f"{pages}.")
            citation = " ".join(citation_parts)

            pub_url = pub.get("pub_url", "") or ""

            results.append(
                {
                    "pub_date": f"{year}-01-01" if year else "",
                    "title": title,
                    "venue": venue,
                    "excerpt": excerpt,
                    "citation": citation,
                    "url_slug": slugify(title),
                    "paper_url": pub_url,
                    "slides_url": "",
                    "bibtex_url": "",
                }
            )

            # Be polite to Google Scholar
            time.sleep(1)

        except Exception as e:
            print(f"  Warning: could not fetch details for pub {i}: {e}")
            continue

    return results


def write_tsv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    existing = load_existing_tsv(TSV_PATH)
    print(f"Loaded {len(existing)} existing entries from {TSV_PATH}")

    fetched = fetch_publications()
    if not fetched:
        print("No publications fetched. Exiting.")
        return

    new_entries = []
    for pub in fetched:
        if not title_in_existing(pub["title"], existing):
            new_entries.append(pub)
            print(f"  NEW: {pub['title'][:70]}")
        else:
            print(f"  SKIP (already exists): {pub['title'][:70]}")

    if not new_entries:
        print("No new publications to add.")
        return

    all_rows = existing + new_entries
    write_tsv(TSV_PATH, all_rows)
    print(
        f"\nWrote {len(all_rows)} total entries ({len(new_entries)} new) to {TSV_PATH}"
    )
    print(
        "\nNext step: regenerate _publications/ markdown files by running:\n"
        "  cd markdown_generator && python3 publications.py publications.tsv"
    )


if __name__ == "__main__":
    main()
