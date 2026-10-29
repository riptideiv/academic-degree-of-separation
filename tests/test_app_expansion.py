"""Public application routes and shared identity contracts."""
from html.parser import HTMLParser
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient
import pytest

from backend.app import app


class Assets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src') if tag == 'script' else (
            attrs.get('href') if tag == 'link' and attrs.get('rel') in ('stylesheet', 'icon') else None)
        if value and not urlsplit(value).netloc:
            self.urls.append(value)


@pytest.mark.parametrize('path', ['/discover', '/connections', '/saved', '/researchers/A123'])
async def test_application_routes_have_working_absolute_assets(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(path)
        assert response.status_code == 200
        for url in Assets(response.text).urls:
            assert url.startswith('/')
            assert (await client.get(url)).status_code == 200
        assert (await client.get('/researchers/not-an-id')).status_code == 404
        assert (await client.get('/unknown-route')).status_code == 404
        assert (await client.get('/api/not-an-endpoint')).status_code == 404


async def test_profile_preserves_affiliation_uncertainty_and_public_source_ids():
    author = {'id': 'https://openalex.org/A123', 'display_name': 'Example Researcher',
              'last_known_institutions': [{'id': 'https://openalex.org/I123', 'display_name': 'Example U'}],
              'affiliations': [{'institution': {'id': 'https://openalex.org/I123', 'display_name': 'Example U'}, 'years': [2023]}],
              'topics': [{'id': 'https://openalex.org/T123', 'display_name': 'Robotics'}]}
    with patch('backend.app._client.get_author', AsyncMock(return_value=author)) as fetch:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/researchers/A123')
            assert response.status_code == 200
            result = response.json()
            assert result['id'] == 'A123'
            assert result['topics'][0]['id'] == 'T123'
            assert result['affiliation_evidence']['verified_current'] is False
            assert result['affiliation_evidence']['years'] == [2023]
            assert result['openalex_url'] == 'https://openalex.org/A123'
            assert 'email' not in result
            assert (await client.get('/api/researchers/W123')).status_code == 422
            fetch.assert_awaited_once_with('A123')


async def test_profile_exposes_multiple_last_known_affiliations_without_choosing_one_as_current():
    author = {'id': 'https://openalex.org/A123', 'display_name': 'Example',
              'last_known_institutions': [
                  {'id': 'https://openalex.org/I1', 'display_name': 'First institute'},
                  {'id': 'https://openalex.org/I2', 'display_name': 'Selected university'}]}
    with patch('backend.app._client.get_author', AsyncMock(return_value=author)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            result = (await client.get('/api/researchers/A123')).json()
    assert [item['display_name'] for item in result['affiliations']] == ['First institute', 'Selected university']
    assert all(not item['verified_current'] for item in result['affiliations'])


async def test_recent_publications_forward_sort_and_reject_invalid_sort():
    with patch('backend.app._client.get_author_works', AsyncMock(return_value=[])) as fetch:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/authors/A123/works?sort=recent')).status_code == 200
            assert (await client.get('/api/authors/A123/works?sort=arbitrary')).status_code == 422
    fetch.assert_awaited_once_with('A123', limit=10, sort='recent')


async def test_recent_publications_keep_reviewed_identity_scope():
    works = [
        {'id': 'https://openalex.org/W1', 'title': 'Old cited', 'publication_year': 2019, 'cited_by_count': 100,
         'authorships': [{'author': {'id': 'https://openalex.org/A123'}}]},
        {'id': 'https://openalex.org/W2', 'title': 'Recent', 'publication_year': 2025, 'cited_by_count': 1,
         'authorships': [{'author': {'id': 'https://openalex.org/A123'}}]},
        {'id': 'https://openalex.org/W3', 'title': 'Outside identity', 'publication_year': 2026,
         'authorships': [{'author': {'id': 'https://openalex.org/A123'}}]},
    ]
    with patch('backend.app.get_verified_work_ids', return_value={'W1', 'W2'}), \
         patch('backend.app._client.get_works_batch', AsyncMock(return_value=works)), \
         patch('backend.app._client.get_author_works', AsyncMock()) as unscoped:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            result = (await client.get('/api/authors/A123/works?sort=recent&limit=1')).json()
    assert [work['id'] for work in result] == ['W2']
    unscoped.assert_not_awaited()
