"""Simple entity extraction without external NLP dependencies.

Extracts named entities using pattern matching:
- Dates (various common formats)
- Metrics (numbers with units)
- Company/organization names (capitalized multi-word sequences)
- Product names (capitalized sequences following product keywords)

Clearly separates *extracted facts* (directly quoted) from
*inferred relationships* (structural patterns observed across sources).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Patterns for entity extraction

_DATE_PATTERN = re.compile(
    r"""
    (?:
        # ISO / standard formats
        \b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b |
        # Month name DD, YYYY
        \b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
              Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|
              Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b |
        # DD Month YYYY
        \b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|
              Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|
              Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}\b |
        # Year alone (4 digit)
        \b(?:19|20)\d{2}\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_METRIC_PATTERN = re.compile(
    r"""
    \b
    (?:\$|€|£|¥|USD\s?|EUR\s?)?        # optional currency prefix
    \d[\d,\.]*                          # number
    \s*
    (?:                                  # unit
        [KkMmBbTt](?:illion|n)?\b |    # K/M/B/T scaling
        %|percent|
        (?:USD|EUR|GBP|INR)\b |
        (?:kg|km|m|cm|mm|mi|ft|in|lb|oz|L|mL|GB|MB|TB|KB)\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Heuristic: multi-word sequences where every word starts with capital letter,
# longer than 1 word, excluding common sentence starters
_ORG_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+){1,4})\b"
)

_PRODUCT_INDICATOR = re.compile(
    r"\b(?:product|platform|solution|service|software|app|application|system|tool|api|sdk|model|framework)\b",
    re.IGNORECASE,
)


@dataclass
class ExtractedEntities:
    """Entities extracted from a text passage."""

    dates: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    source_name: str = ""
    source_page: int | None = None
    source_section: str | None = None
    # Raw passage from which entities were extracted
    excerpt: str = ""


@dataclass
class InferredRelationship:
    """
    A structural pattern observed across sources.

    Explicitly labelled as *inferred* – not a confirmed causal or logical
    relationship. Chronological ordering alone does not imply causation.
    """

    relationship_type: str  # e.g. "shared_entity", "co-occurrence"
    entity: str
    sources: list[str]  # source names
    note: str  # human-readable description of the inference
    # Inferences are explicitly labelled to avoid misrepresentation
    is_inferred: bool = True


def extract_entities(
    text: str,
    source_name: str,
    page: int | None = None,
    section: str | None = None,
) -> ExtractedEntities:
    """Extract entities from a text passage using pattern matching."""
    entities = ExtractedEntities(
        source_name=source_name,
        source_page=page,
        source_section=section,
        excerpt=text[:500],  # keep excerpt bounded
    )

    entities.dates = list(dict.fromkeys(m.group(0) for m in _DATE_PATTERN.finditer(text)))
    entities.metrics = list(dict.fromkeys(m.group(0).strip() for m in _METRIC_PATTERN.finditer(text)))

    # Organization heuristic: deduplicate case-insensitively, exclude very short ones
    seen: set[str] = set()
    orgs: list[str] = []
    for m in _ORG_PATTERN.finditer(text):
        candidate = m.group(1)
        lower = candidate.lower()
        if lower not in seen and len(candidate) > 5:
            seen.add(lower)
            orgs.append(candidate)
    entities.organizations = orgs[:20]  # cap to avoid noise

    return entities


def find_shared_entities(
    entity_sets: list[ExtractedEntities],
) -> list[InferredRelationship]:
    """
    Identify entities that appear in multiple sources.

    Returns InferredRelationship objects labelled as inferred.
    Shared occurrence does not imply causation or logical connection.
    """
    # Build entity → sources index
    org_sources: dict[str, list[str]] = {}
    date_sources: dict[str, list[str]] = {}
    metric_sources: dict[str, list[str]] = {}

    for es in entity_sets:
        for org in es.organizations:
            key = org.lower()
            org_sources.setdefault(key, [])
            if es.source_name not in org_sources[key]:
                org_sources[key].append(es.source_name)
        for date in es.dates:
            date_sources.setdefault(date, [])
            if es.source_name not in date_sources[date]:
                date_sources[date].append(es.source_name)
        for metric in es.metrics:
            key = metric.lower()
            metric_sources.setdefault(key, [])
            if es.source_name not in metric_sources[key]:
                metric_sources[key].append(es.source_name)

    relationships: list[InferredRelationship] = []

    for org, sources in org_sources.items():
        if len(sources) > 1:
            relationships.append(InferredRelationship(
                relationship_type="shared_organization",
                entity=org,
                sources=sources,
                note=(
                    f"Entity '{org}' appears in {len(sources)} sources: "
                    f"{', '.join(sources)}. "
                    "Co-occurrence is noted; no causal relationship is implied."
                ),
            ))

    for date, sources in date_sources.items():
        if len(sources) > 1:
            relationships.append(InferredRelationship(
                relationship_type="shared_date",
                entity=date,
                sources=sources,
                note=(
                    f"Date '{date}' appears in {len(sources)} sources. "
                    "Chronological proximity does not imply causation."
                ),
            ))

    for metric, sources in metric_sources.items():
        if len(sources) > 1:
            relationships.append(InferredRelationship(
                relationship_type="shared_metric",
                entity=metric,
                sources=sources,
                note=(
                    f"Metric '{metric}' appears in {len(sources)} sources. "
                    "Shared numeric value may reflect the same data cited multiple times."
                ),
            ))

    return relationships
