#!/usr/bin/env python3
"""Sync the _publications/ collection from ORCID + Crossref.

ORCID answers "which papers exist" (it is the authoritative DOI list, and it
auto-populates from Crossref once you grant that permission in your ORCID
account). Crossref answers "what is this paper" -- full author list, journal,
volume, issue, pages, dates -- which is what the AMS-style citations on this
site need.

Google Scholar is deliberately not used: it has no official API, and scraping
it from a GitHub Actions runner gets CAPTCHA-blocked.

Existing files in _publications/ are treated as read-only. This script only
ever creates files that do not already exist, so hand-written excerpts and
prose bodies are never clobbered. Deduplication is by DOI.

Usage:
    python scripts/sync_publications.py --dry-run
    python scripts/sync_publications.py
    python scripts/sync_publications.py --output-dir /tmp/pubtest
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ORCID = "0000-0002-3212-7300"
CONTACT_EMAIL = "qlawton@niu.edu"

ORCID_API = "https://pub.orcid.org/v3.0/{orcid}/works"
CROSSREF_API = "https://api.crossref.org/works/{doi}"

# Crossref sees far better service if you identify yourself (the "polite pool").
USER_AGENT = f"lawton-research-lab/1.0 (+https://quintonlawton.com; mailto:{CONTACT_EMAIL})"

TIMEOUT = 30
CROSSREF_DELAY = 0.5

# Crossref work type -> the site's publication_category keys in _config.yml.
CATEGORY_BY_TYPE = {
    "journal-article": "manuscripts",
    "proceedings-article": "conferences",
    "book": "books",
    "book-chapter": "books",
    "monograph": "books",
    "edited-book": "books",
    "reference-book": "books",
}
PREPRINT_TYPES = {"posted-content"}

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;)\]]+")

SLUG_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "its",
    "of", "on", "or", "the", "their", "to", "with", "within",
}
SLUG_WORD_LIMIT = 6

EXCERPT_MAX_CHARS = 320


# --------------------------------------------------------------------------
# DOI helpers
# --------------------------------------------------------------------------

def normalize_doi(value):
    """Reduce any DOI spelling to a bare '10.xxxx/yyyy', preserving case."""
    if not value:
        return None
    text = value.strip().strip("'\"")
    text = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    match = DOI_PATTERN.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;")


def doi_key(doi):
    """DOIs are case-insensitive, so compare on a lowercased key."""
    return (doi or "").lower()


def display_doi(doi, work):
    """The DOI as the publisher writes it, for use in citations.

    Neither source is reliable on its own: Crossref lowercases the `DOI` field,
    and ORCID's casing varies per record. The publisher's own article URL does
    carry the canonical form (AMS embeds the DOI suffix verbatim), so prefer
    that when it matches, and otherwise keep whatever casing we were handed.
    """
    primary = ((work.get("resource") or {}).get("primary") or {}).get("URL") or ""
    prefix, _, suffix = doi.partition("/")
    match = re.search(re.escape(suffix), primary, flags=re.I)
    if match:
        return f"{prefix}/{match.group(0)}"
    return doi


def existing_dois(pub_dir):
    """Every DOI already represented in _publications/, for deduplication.

    `paperurl` is the primary key, but we also sweep the whole file for DOI
    patterns (they appear inside `citation` too). Over-collecting here is safe:
    the only consequence is correctly skipping a paper we already have.
    """
    found = set()
    if not pub_dir.is_dir():
        return found
    for path in sorted(pub_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for raw in DOI_PATTERN.findall(text):
            doi = normalize_doi(raw)
            if doi:
                found.add(doi_key(doi))
    return found


# --------------------------------------------------------------------------
# Remote sources
# --------------------------------------------------------------------------

def get_json(url, accept="application/json"):
    """GET and parse JSON using only the standard library.

    Deliberately dependency-free: no `pip install` step in CI, and the script
    runs anywhere Python does.
    """
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_orcid_dois(orcid):
    """Return an ordered, de-duplicated list of DOIs on the ORCID record."""
    payload = get_json(ORCID_API.format(orcid=orcid))

    dois = []
    seen = set()
    for group in payload.get("group") or []:
        external_ids = (group.get("external-ids") or {}).get("external-id") or []
        for ext in external_ids:
            if (ext.get("external-id-type") or "").lower() != "doi":
                continue
            doi = normalize_doi(ext.get("external-id-value"))
            if doi and doi_key(doi) not in seen:
                seen.add(doi_key(doi))
                dois.append(doi)
    return dois


def fetch_crossref(doi):
    """Fetch Crossref metadata for a DOI, or None if it cannot be resolved."""
    url = CROSSREF_API.format(doi=urllib.parse.quote(doi, safe="/"))
    try:
        return get_json(url).get("message")
    except urllib.error.HTTPError as exc:
        print(f"  ! Crossref returned {exc.code} for {doi}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"  ! network error for {doi}: {exc.reason}", file=sys.stderr)
    except ValueError:
        print(f"  ! unparseable Crossref response for {doi}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def strip_markup(text):
    """Drop JATS/HTML tags Crossref sometimes embeds, and unescape entities."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def initials(given):
    """'Quinton A.' -> 'Q. A.'   'Jean-Pierre' -> 'J.-P.'"""
    out = []
    for part in re.split(r"[\s.]+", (given or "").strip()):
        if not part:
            continue
        if "-" in part:
            segments = [seg for seg in part.split("-") if seg]
            out.append("-".join(f"{seg[0].upper()}." for seg in segments))
        else:
            out.append(f"{part[0].upper()}.")
    return " ".join(out)


def format_authors(authors):
    """AMS style: 'Lawton, Q. A., S. J. Majumdar, and C. J. Schreck'."""
    names = []
    for index, author in enumerate(authors or []):
        family = (author.get("family") or "").strip()
        if not family:
            organisation = (author.get("name") or "").strip()
            if organisation:
                names.append(organisation)
            continue
        given = initials(author.get("given"))
        if index == 0:
            names.append(f"{family}, {given}".strip().rstrip(","))
        else:
            names.append(f"{given} {family}".strip())

    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}, and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def publication_date(work):
    """Best available date as (YYYY-MM-DD, year). Prefers the issue date."""
    for key in ("published-print", "published", "published-online", "issued"):
        parts = (work.get(key) or {}).get("date-parts") or []
        if not parts or not parts[0] or parts[0][0] is None:
            continue
        chunk = list(parts[0]) + [1, 1]
        year, month, day = int(chunk[0]), int(chunk[1] or 1), int(chunk[2] or 1)
        return f"{year:04d}-{month:02d}-{day:02d}", year
    return None, None


def format_pages(work):
    """Crossref uses a hyphen; the site's existing citations use an en dash."""
    pages = (work.get("page") or "").strip()
    if not pages:
        return ""
    return pages.replace("--", "–").replace("-", "–")


def build_citation(work, doi):
    authors = format_authors(work.get("author"))
    title = strip_markup(_first(work.get("title")))
    venue = strip_markup(_first(work.get("container-title")))
    date_str, year = publication_date(work)

    head = f"{authors}, {year}: " if authors and year else (f"{authors}: " if authors else "")
    body = title.rstrip(".") + "." if title else ""

    tail = ""
    if venue:
        tail = f" <i>{venue}</i>"
        volume = (work.get("volume") or "").strip()
        issue = (work.get("issue") or "").strip()
        pages = format_pages(work)
        if volume:
            tail += f", {volume}"
            if issue:
                tail += f"({issue})"
        if pages:
            tail += f", {pages}"
        tail += "."

    return f"{head}{body}{tail} https://doi.org/{doi}".strip()


def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def slugify(title):
    words = re.sub(r"[^a-z0-9\s-]", " ", strip_markup(title).lower()).split()
    kept = [w for w in words if w not in SLUG_STOPWORDS] or words
    return "-".join(kept[:SLUG_WORD_LIMIT]) or "untitled"


def build_excerpt(work):
    """Two sentences of the Crossref abstract, if there is one at all.

    Plenty of AMS DOIs carry no abstract in Crossref. Returning empty is the
    honest outcome -- the PR reviewer writes the real summary.
    """
    abstract = strip_markup(work.get("abstract"))
    if not abstract:
        return ""
    abstract = re.sub(r"^abstract[:\s]*", "", abstract, flags=re.I)
    sentences = re.split(r"(?<=[.!?])\s+", abstract)
    excerpt = ""
    for sentence in sentences[:2]:
        candidate = f"{excerpt} {sentence}".strip()
        if excerpt and len(candidate) > EXCERPT_MAX_CHARS:
            break
        excerpt = candidate
    if len(excerpt) > EXCERPT_MAX_CHARS:
        excerpt = excerpt[:EXCERPT_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return excerpt


def yaml_single(value):
    """Value for a YAML single-quoted scalar: internal apostrophes double."""
    return (value or "").replace("'", "''")


def yaml_double(value):
    """Value for a YAML double-quoted scalar."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def render_markdown(work, doi, category):
    doi = display_doi(doi, work)
    title = strip_markup(_first(work.get("title")))
    venue = strip_markup(_first(work.get("container-title")))
    date_str, _ = publication_date(work)
    slug = slugify(title)
    permalink = f"/publication/{date_str}-{slug}"
    excerpt = build_excerpt(work)
    citation = build_citation(work, doi)

    lines = [
        "---",
        f'title: "{yaml_double(title)}"',
        "collection: publications",
        f"category: {category}",
        f"permalink: {permalink}",
    ]
    if excerpt:
        lines.append(f"excerpt: '{yaml_single(excerpt)}'")
    lines.append(f"date: {date_str}")
    if venue:
        lines.append(f"venue: '{yaml_single(venue)}'")
    lines.append(f"paperurl: 'https://doi.org/{doi}'")
    lines.append(f"citation: '{yaml_single(citation)}'")
    lines.append("---")
    lines.append("")
    lines.append("<!-- TODO: auto-generated from Crossref. Review the summary below before merging. -->")
    lines.append("")
    lines.append(excerpt if excerpt else "<!-- No abstract available from Crossref. Add a summary here. -->")
    lines.append("")

    return f"{date_str}-{slug}.md", "\n".join(lines)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create _publications/*.md for papers on ORCID that the site is missing."
    )
    parser.add_argument("--orcid", default=DEFAULT_ORCID, help="ORCID iD to read")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "_publications",
        help="Where to write new publication files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching disk",
    )
    parser.add_argument(
        "--include-preprints",
        action="store_true",
        help="Also include Crossref 'posted-content' records (preprints)",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Write a markdown summary of additions here (used as the PR body)",
    )
    args = parser.parse_args()

    # Dedup always reads the real collection, even when writing elsewhere, so
    # that --output-dir stays a rendering sandbox rather than a second source.
    pub_dir = REPO_ROOT / "_publications"
    known = existing_dois(pub_dir)
    print(f"Found {len(known)} DOI(s) already in {pub_dir.relative_to(REPO_ROOT)}/")

    try:
        orcid_dois = fetch_orcid_dois(args.orcid)
    except (urllib.error.URLError, ValueError) as exc:
        print(f"ERROR: could not read ORCID {args.orcid}: {exc}", file=sys.stderr)
        return 1
    print(f"ORCID {args.orcid} lists {len(orcid_dois)} DOI(s)")

    missing = [doi for doi in orcid_dois if doi_key(doi) not in known]
    if not missing:
        print("Nothing new. The site is up to date with ORCID.")
        _write_summary(args.summary_file, [])
        return 0

    print(f"{len(missing)} DOI(s) not yet on the site:")
    for doi in missing:
        print(f"  - {doi}")

    added = []
    skipped = []
    for doi in missing:
        time.sleep(CROSSREF_DELAY)
        work = fetch_crossref(doi)
        if work is None:
            skipped.append((doi, "no Crossref record"))
            continue

        work_type = (work.get("type") or "").lower()
        if work_type in PREPRINT_TYPES and not args.include_preprints:
            skipped.append((doi, "preprint (use --include-preprints)"))
            continue
        category = CATEGORY_BY_TYPE.get(work_type)
        if category is None:
            skipped.append((doi, f"unhandled Crossref type '{work_type}'"))
            continue

        date_str, _ = publication_date(work)
        if not date_str:
            skipped.append((doi, "no usable publication date"))
            continue

        filename, content = render_markdown(work, doi, category)
        target = args.output_dir / filename

        # Hard guarantee: never open an existing file for writing.
        if target.exists():
            skipped.append((doi, f"{filename} already exists"))
            continue

        if args.dry_run:
            print(f"\n--- would write {filename} ---\n{content}")
        else:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"  + wrote {filename}")

        added.append(
            (display_doi(doi, work), filename, strip_markup(_first(work.get("title"))))
        )

    print(f"\n{len(added)} added, {len(skipped)} skipped")
    for doi, reason in skipped:
        print(f"  ~ {doi}: {reason}")

    _write_summary(args.summary_file, added, skipped)
    return 0


def _write_summary(path, added, skipped=()):
    if not path:
        return
    lines = []
    if added:
        lines.append(
            f"Found {len(added)} publication(s) on ORCID that were missing from `_publications/`."
        )
        lines.append("")
        for doi, filename, title in added:
            lines.append(f"- **{title}**  ")
            lines.append(f"  `{filename}` — https://doi.org/{doi}")
        lines.append("")
        lines.append(
            "Metadata comes from Crossref. Please check the citation, and replace the "
            "auto-generated summary (marked with a `TODO` comment) with your own before merging. "
            "Renaming the file to a shorter slug is fine — update `permalink` to match."
        )
    else:
        lines.append("No new publications found on ORCID.")
    if skipped:
        lines.append("")
        lines.append("<details><summary>Skipped</summary>")
        lines.append("")
        for doi, reason in skipped:
            lines.append(f"- `{doi}` — {reason}")
        lines.append("")
        lines.append("</details>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
