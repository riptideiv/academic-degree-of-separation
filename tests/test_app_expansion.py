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
