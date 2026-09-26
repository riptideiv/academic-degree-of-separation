"""Check the public discovery surface through the same routes crawlers request."""

from html.parser import HTMLParser
import json
import struct
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from httpx import ASGITransport, AsyncClient

from backend.app import app


ORIGIN = "https://academic-degree-of-separation.onrender.com"


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.schemas = []
        self.schema = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.schema = ""

    def handle_data(self, data):
        if self.schema is not None:
            self.schema += data

    def handle_endtag(self, tag):
        if tag == "script" and self.schema is not None:
            self.schemas.append(json.loads(self.schema))
            self.schema = None

    def attrs(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


async def test_sitemap_targets_are_crawlable_canonical_pages_with_working_links():
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/sitemap.xml")
        assert response.status_code == 200
        urls = [node.text for node in ElementTree.fromstring(response.text).findall(
            "{*}url/{*}loc"
        )]
        assert set(urls) == {ORIGIN + "/", ORIGIN + "/guide/"}
        assert len(urls) == len(set(urls))
        for url in urls:
            response = await client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            page = Page(response.text)
            assert len(page.attrs("h1")) == 1
            assert [a["href"] for a in page.attrs("link") if a.get("rel") == "canonical"] == [url]
            assert not any("noindex" in a.get("content", "") for a in page.attrs("meta"))
            for schema in page.schemas:
                assert schema["@context"] == "https://schema.org"
                assert schema["url"] == url
            # Navigation remains discoverable in the raw HTML, without executing JS.
            links = [a["href"] for a in page.attrs("a") if a.get("href", "").startswith("/")]
            assert ("/guide/" if url == ORIGIN + "/" else "/") in links
            for link in set(links):
                assert (await client.get(urlsplit(link).path)).status_code == 200


async def test_robots_preserves_public_pages_and_admin_noindex_is_readable():
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/robots.txt")
        assert response.status_code == 200
        robots = RobotFileParser()
        robots.parse(response.text.splitlines())
        assert robots.site_maps() == [ORIGIN + "/sitemap.xml"]
        for path in ("/", "/guide/", "/analytics.html", "/social-preview.png"):
            assert robots.can_fetch("Googlebot", ORIGIN + path)
        assert not robots.can_fetch("Googlebot", ORIGIN + "/api/authors?q=Alice")
        admin = Page((await client.get("/analytics.html")).text)
        assert any(a.get("name") == "robots" and "noindex" in a.get("content", "")
                   for a in admin.attrs("meta"))
        assert (await client.get("/page-that-does-not-exist")).status_code == 404


async def test_social_preview_is_a_real_image_matching_its_metadata():
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for path in ("/", "/guide/"):
            page = Page((await client.get(path)).text)
            metadata = {a.get("property") or a.get("name"): a.get("content")
                        for a in page.attrs("meta")}
            image = await client.get(metadata["og:image"])
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/png"
            assert image.content[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", image.content[16:24]) == (1200, 630)
            assert metadata["twitter:image"] == metadata["og:image"]
