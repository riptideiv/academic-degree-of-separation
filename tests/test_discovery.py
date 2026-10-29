import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import app
from backend.discovery import discover


def topic(tid='T1'):
    return {'id': 'https://openalex.org/' + tid, 'display_name': 'Robotics ' + tid}


def work(wid='W1', authors=None, topics=None):
    return {'id': 'https://openalex.org/' + wid, 'title': 'A robot paper', 'publication_year': 2026,
            'topics': topics or [topic()], 'authorships': authors or [authorship('A1', 'I1')]}


def authorship(aid, iid):
    return {'author': {'id': 'https://openalex.org/' + aid, 'display_name': aid},
            'institutions': [{'id': 'https://openalex.org/' + iid}]}


def author(aid='A1', iid='I1'):
    return {'id': 'https://openalex.org/' + aid, 'display_name': aid,
            'last_known_institutions': [{'id': 'https://openalex.org/' + iid}], 'topics': [topic()]}


def client():
    return SimpleNamespace(
        get_institution=AsyncMock(return_value={'display_name': 'University'}),
        get_topic=AsyncMock(return_value=topic()),
        get_work=AsyncMock(return_value=work()),
        get_author=AsyncMock(return_value=author('A9')),
        get_discovery_works=AsyncMock(return_value={'results': [work()], 'meta': {'count': 100}}),
        get_authors_batch=AsyncMock(return_value=[author()]),
    )


async def test_topic_only_discovery_needs_no_graph_or_coauthor_path():
    upstream = client()
    result = await discover(upstream, 'I1', ['T1'])
    assert result['results'][0]['author']['id'] == 'A1'
    assert result['results'][0]['connection'] == {'status': 'not_checked'}
    assert result['results'][0]['works'][0]['id'] == 'W1'
    assert result['reference_authors'] == []
    assert result['coverage']['more_works_available'] is True
    upstream.get_author.assert_not_awaited()


async def test_other_institution_coauthors_and_former_members_are_not_campus_results():
    upstream = client()
    upstream.get_discovery_works.return_value = {'results': [work(authors=[authorship('A1', 'I1'), authorship('A2', 'I2'), authorship('A3', 'I1')])]}
    upstream.get_authors_batch.return_value = [author('A1'), author('A2'), author('A3', 'I3')]
    result = await discover(upstream, 'I1', ['T1'])
    assert [r['author']['id'] for r in result['results']] == ['A1']
    assert 'A2' not in upstream.get_authors_batch.call_args.args[0]
    assert result['results'][0]['affiliation_evidence']['verified_current'] is False


async def test_paper_references_supply_topics_and_explicit_author_choices():
    upstream = client()
    upstream.get_work.return_value = work('W9', authors=[authorship('A9', 'I2')])
    result = await discover(upstream, 'I1', ['W9'])
    assert result['reference_authors'] == [{'id': 'A9', 'display_name': 'A9'}]
    assert result['query']['references'][0]['display_name'] == 'A robot paper'
    assert result['topics'][0]['id'] == 'T1'
    assert len(result['results']) == 1


async def test_partial_topic_failure_keeps_useful_results_and_marks_coverage():
    upstream = client()
    upstream.get_topic.side_effect = [topic(), topic('T2')]
    upstream.get_discovery_works.side_effect = [{'results': [work()]}, RuntimeError('source offline')]
    result = await discover(upstream, 'I1', ['T1', 'T2'])
    assert len(result['results']) == 1
    assert result['coverage']['partial'] is True
    assert 'could not be checked' in result['coverage']['note']


async def test_identity_work_scope_cannot_be_bypassed_by_discovery():
    upstream = client()
    with patch('backend.discovery.get_verified_work_ids', return_value={'W2'}):
        result = await discover(upstream, 'I1', ['T1'])
    assert result['results'] == []
    upstream.get_authors_batch.assert_not_awaited()


async def test_candidates_require_actual_matching_publication_topics():
    upstream = client()
    upstream.get_discovery_works.return_value = {'results': [work(topics=[topic('T2')])]}
    result = await discover(upstream, 'I1', ['T1'])
    assert result['results'] == []


@pytest.mark.parametrize('query', [
    'institution_id=A1&reference_ids=T1', 'institution_id=I1&reference_ids=bad',
    'institution_id=I1&reference_ids=T1,evil',
    'institution_id=I1&' + '&'.join('reference_ids=T' + str(i) for i in range(9)),
])
async def test_invalid_discovery_requests_fail_before_any_upstream_access(query):
    with patch('backend.app._client') as upstream:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
            response = await http.get('/api/discover?' + query)
    assert response.status_code == 422
    assert upstream.mock_calls == []


@pytest.mark.parametrize('complete,errors,expected', [(True, 0, 'not_found'), (False, 0, 'incomplete'), (True, 1, 'incomplete')])
async def test_connection_negatives_preserve_bounded_search_status(complete, errors, expected):
    with patch('backend.app._client.get_author', AsyncMock(return_value=author())), patch(
        'backend.app._short_coauthor_paths', AsyncMock(return_value=({}, errors, complete))
    ) as paths:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
            response = await http.get('/api/connections/check?from_id=A9&to_id=A1')
    assert response.json()['status'] == expected
    assert response.json()['max_hops'] == 2
    assert paths.call_args.args[2] == 2


async def test_timeout_is_an_actionable_response_without_false_negative():
    with patch('backend.app.discover', AsyncMock(side_effect=TimeoutError)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
            response = await http.get('/api/discover?institution_id=I1&reference_ids=T1')
    assert response.status_code == 504
    assert 'timed out' in response.json()['detail']


async def test_discovery_cancellation_cancels_inflight_reference_work():
    upstream = client()
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def slow(_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    upstream.get_topic.side_effect = slow
    task = asyncio.create_task(discover(upstream, 'I1', ['T1']))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_unresolved_authorships_and_null_topic_ids_do_not_break_discovery():
    upstream = client()
    upstream.get_discovery_works.return_value = {'results': [work(
        authors=[{'author': {'id': None}, 'institutions': [{'id': None}]}, authorship('A1', 'I1')],
        topics=[{'id': None}, topic()],
    )]}
    result = await discover(upstream, 'I1', ['T1'])
    assert result['results'][0]['author']['id'] == 'A1'


async def test_upstream_discovery_lane_is_institution_scoped_recent_and_paper_only():
    from backend.openalex_client import OpenAlexClient
    upstream = OpenAlexClient()
    with patch.object(upstream, '_get', AsyncMock(return_value={'results': []})) as fetch:
        await upstream.get_discovery_works('I1', 'T1')
        await upstream.get_discovery_works('I1', 'T1')
    assert fetch.await_count == 1
    params = fetch.call_args.args[1]
    assert 'authorships.institutions.id:I1' in params['filter']
    assert 'topics.id:T1' in params['filter']
    assert 'type:article|preprint|' in params['filter']
    assert 'is_retracted:false' in params['filter']
    assert params['sort'] == 'publication_date:desc'
    assert params['per_page'] == 15
