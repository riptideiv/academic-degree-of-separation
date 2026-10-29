"""Publication-backed institution discovery, independent of coauthor distance."""
from __future__ import annotations

import asyncio
from collections import defaultdict
import re

from backend.affiliation_overrides import (
    apply_reviewed_identity, get_effective_affiliation_overrides, get_verified_work_ids,
)
from backend.openalex_client import _short_id as _source_id

MAX_TOPICS = 8
MAX_CANDIDATES = 80


def _short_id(value) -> str:
    # OpenAlex can retain an authorship whose resolved author ID is null.
    return _source_id(value) if isinstance(value, str) else ''


def topics_for(record: dict) -> list[dict]:
    result = []
    for topic in record.get('topics') or []:
        if not isinstance(topic, dict):
            continue
        topic_id = _short_id(topic.get('id', ''))
        if re.fullmatch(r'T\d{1,20}', topic_id):
            result.append({'id': topic_id, 'display_name': topic.get('display_name') or topic.get('name') or topic_id})
    return result


def work_record(work: dict) -> dict:
    return {'id': _short_id(work['id']), 'title': work.get('title') or '(untitled)',
            'publication_year': work.get('publication_year'), 'doi': work.get('doi')}


async def discover(client, institution_id: str, reference_ids: list[str]) -> dict:
    # Each stage owns and awaits its children; cancellation does not leave work running.
    semaphore = asyncio.Semaphore(6)

    async def fetch_reference(ref):
        async with semaphore:
            if ref.startswith('T'):
                return await client.get_topic(ref)
            if ref.startswith('W'):
                return await client.get_work(ref)
            return apply_reviewed_identity(await client.get_author(ref))

    institution = await client.get_institution(institution_id)
    records = await asyncio.gather(*(fetch_reference(ref) for ref in reference_ids), return_exceptions=True)
    topics = {}
    authors = {}
    references = []
    failures = 0
    # Explicit topics take priority. Other references contribute their top three topics.
    for ref, record in sorted(zip(reference_ids, records), key=lambda pair: not pair[0].startswith('T')):
        if isinstance(record, BaseException):
            failures += 1
            references.append({'id': ref, 'display_name': ref, 'unavailable': True})
            continue
        references.append({'id': ref, 'display_name': record.get('display_name') or record.get('title') or ref})
        if ref.startswith('T'):
            topics[ref] = {'id': ref, 'display_name': record.get('display_name') or ref}
        else:
            for topic in topics_for(record)[:3]:
                topics.setdefault(topic['id'], topic)
        if ref.startswith('A'):
            authors[ref] = {'id': ref, 'display_name': record.get('display_name') or ref}
        elif ref.startswith('W'):
            for authorship in (record.get('authorships') or [])[:20]:
                author = authorship.get('author') or {}
                aid = _short_id(author.get('id', ''))
                if re.fullmatch(r'A\d{1,20}', aid):
                    authors[aid] = {'id': aid, 'display_name': author.get('display_name') or aid}
    all_topic_count = len(topics)
    topics = dict(list(topics.items())[:MAX_TOPICS])
    base = {
        'query': {'institution': {'id': institution_id, 'display_name': institution.get('display_name') or institution_id},
                  'references': references},
        'topics': list(topics.values()), 'reference_authors': list(authors.values())[:30],
        'results': [],
    }
    if not topics:
        return {**base, 'coverage': {'partial': bool(failures), 'works_checked': 0, 'candidates_checked': 0},
                'message': 'Reference data could not be loaded. Try again.' if failures else
                'No indexed topics were found for these references. Choose a topic explicitly.'}

    async def lane(topic):
        async with semaphore:
            return await client.get_discovery_works(institution_id, topic)

    lanes = await asyncio.gather(*(lane(topic) for topic in topics), return_exceptions=True)
    works = {}
    total_matching = 0
    for data in lanes:
        if isinstance(data, BaseException):
            failures += 1
            continue
        total_matching += data.get('meta', {}).get('count', len(data.get('results', [])))
        for work in data.get('results', []):
            wid = _short_id(work.get('id', ''))
            if re.fullmatch(r'W\d{1,20}', wid):
                works.setdefault(wid, work)

    # Only authors whose own authorship carries the selected institution qualify.
    # A collaborator from another institution on the same work is not a campus result.
    evidence = defaultdict(dict)
    matched_topics = defaultdict(set)
    overrides = {item.author_id: item for item in get_effective_affiliation_overrides(institution_id)}
    excluded = {aid for aid, item in overrides.items() if item.action == 'exclude'}
    for wid, work in works.items():
        matches = {topic['id'] for topic in topics_for(work)} & topics.keys()
        if not matches:
            continue
        for authorship in work.get('authorships') or []:
            aid = _short_id((authorship.get('author') or {}).get('id', ''))
            if not re.fullmatch(r'A\d{1,20}', aid) or aid in excluded or aid in reference_ids:
                continue
            institutions = {_short_id(inst.get('id', '')) for inst in authorship.get('institutions') or []}
            if institution_id not in institutions:
                continue
            scope = get_verified_work_ids(aid)
            if scope is not None and wid not in scope:
                continue
            evidence[aid][wid] = work_record(work)
            matched_topics[aid].update(matches)

    def relevance(aid):
        items = list(evidence[aid].values())
        return (-len(matched_topics[aid]), -min(len(items), 3),
                -max((item['publication_year'] or 0 for item in items), default=0), aid)

    candidate_ids = sorted(evidence, key=relevance)[:MAX_CANDIDATES]
    profiles = []
    for start in range(0, len(candidate_ids), 40):
        try:
            profiles.extend(await client.get_authors_batch(candidate_ids[start:start + 40]))
        except Exception:
            failures += 1
    by_id = {_short_id(record.get('id', '')): apply_reviewed_identity(record) for record in profiles}
    missing_profiles = len(set(candidate_ids) - by_id.keys())
    results = []
    for aid in candidate_ids:
        author = by_id.get(aid)
        if not author:
            continue
        override = overrides.get(aid)
        reviewed = override is not None and override.action == 'include'
        institution_ids = {_short_id(inst.get('id', '')) for inst in author.get('last_known_institutions') or []}
        if not reviewed and institution_id not in institution_ids:
            continue
        aff = {
            'institution_id': institution_id,
            'display_name': base['query']['institution']['display_name'],
            'status': 'verified_current' if reviewed else 'openalex_last_known',
            'verified_current': reviewed,
            'source_url': override.evidence_url if reviewed else f'https://openalex.org/{aid}',
        }
        results.append({
            'author': {'id': aid, 'display_name': author.get('display_name') or aid,
                       'institution': aff['display_name']},
            'affiliation_evidence': aff,
            'matching_topics': [topics[tid] for tid in sorted(matched_topics[aid])],
            'works': sorted(evidence[aid].values(), key=lambda work: (-(work['publication_year'] or 0), work['id']))[:3],
            'connection': {'status': 'not_checked'},
        })
    partial = bool(failures or missing_profiles or all_topic_count > MAX_TOPICS or len(evidence) > MAX_CANDIDATES)
    return {**base, 'results': results[:20],
            'coverage': {'partial': partial, 'works_checked': len(works), 'candidates_checked': len(profiles),
                         'topic_limit': MAX_TOPICS, 'candidate_limit': MAX_CANDIDATES,
                         'more_works_available': total_matching > len(works),
                         'note': 'A bounded sample of recent publications; not everyone at the university. '
                                 + ('Some sources or candidates could not be checked.' if partial else '')},
            'message': '' if results else ('Some sources could not be checked. Try again.' if failures else
                                          'No matching researchers in this sample. Try a different topic or reference.')}
