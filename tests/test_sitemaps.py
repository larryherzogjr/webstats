import unittest
from unittest.mock import patch

from webstats.sitemaps import _discover_site, parse_sitemap, sitemap_declarations


class SitemapTests(unittest.TestCase):
    def test_declarations_accept_only_same_site_https(self):
        text = """
Sitemap: https://example.com/main.xml
Sitemap: https://www.example.com/news.xml
Sitemap: http://example.com/insecure.xml
Sitemap: https://example.com:8443/private.xml
Sitemap: https://other.example/map.xml
"""
        self.assertEqual(
            sitemap_declarations(text, "example.com"),
            [
                "https://example.com/main.xml",
                "https://www.example.com/news.xml",
            ],
        )

    def test_parser_handles_indexes_and_ignores_image_locations(self):
        index = b"""<?xml version='1.0'?>
<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
  <sitemap><loc>https://example.com/posts.xml</loc></sitemap>
</sitemapindex>"""
        parsed_index = parse_sitemap(index, "https://example.com/sitemap.xml")
        self.assertEqual(parsed_index["kind"], "sitemapindex")
        self.assertEqual(parsed_index["sitemaps"], ["https://example.com/posts.xml"])

        urlset = b"""<?xml version='1.0'?>
<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'
        xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>
  <url><loc>https://example.com/story</loc><lastmod>2026-09-28</lastmod>
    <image:image><image:loc>https://example.com/cover.jpg</image:loc></image:image>
  </url>
</urlset>"""
        parsed_urls = parse_sitemap(urlset, "https://example.com/posts.xml")
        self.assertEqual(parsed_urls["urls"], [{
            "url": "https://example.com/story", "lastmod": "2026-09-28",
        }])

    def test_discovery_follows_same_site_index_and_deduplicates_paths(self):
        documents = {
            "https://example.com/index.xml": b"""
              <sitemapindex><sitemap><loc>https://example.com/posts.xml</loc></sitemap>
              <sitemap><loc>https://elsewhere.test/foreign.xml</loc></sitemap></sitemapindex>
            """,
            "https://example.com/posts.xml": b"""
              <urlset><url><loc>https://example.com/one?source=map</loc>
              <lastmod>2026-09-28</lastmod></url></urlset>
            """,
        }

        def download(url, _site, _timeout):
            return {"url": url, "status": "available", "body": documents[url]}

        robots = {"text": "Sitemap: https://example.com/index.xml"}
        with patch("webstats.sitemaps._download_bytes", side_effect=download):
            result = _discover_site("example.com", robots, 1)
        self.assertEqual(result["status"], "available")
        self.assertEqual([row["path"] for row in result["pages"]], ["/one"])
        self.assertEqual(len(result["documents"]), 2)


if __name__ == "__main__":
    unittest.main()
