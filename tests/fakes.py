"""A fake Piazza HTTP session for the Resources tests. No network."""

NID = "msyx97enxo77kr"
STATUS = {
    "networks": [
        {
            "id": NID,
            "school_ext": "purdue",
            "term_key": "fall2026",
            "short_number": "wlece36200",
            "folders": ["hw1"],
        }
    ]
}
PAGE_URL = "https://piazza.com/purdue/fall2026/wlece36200/resources"


class FakeResponse:
    def __init__(self, status_code=200, text="", chunks=(), content_type="application/pdf"):
        self.status_code = status_code
        self.text = text
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size=65_536):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Serves the Resources page and downloads. Queued pages/downloads are
    returned first (one per request), then the defaults."""

    def __init__(self, page_html, download=None):
        self.page_html = page_html
        self.download = download or FakeResponse(chunks=[b"%PDF-1.4 ", b"body"])
        self.queued_pages = []
        self.queued_downloads = []
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url == PAGE_URL:
            html = self.queued_pages.pop(0) if self.queued_pages else self.page_html
            return FakeResponse(text=html)
        if "/class_profile/get_resource/" in url:
            return self.queued_downloads.pop(0) if self.queued_downloads else self.download
        return FakeResponse(status_code=404)


def download_count(session):
    return sum("/class_profile/get_resource/" in url for url in session.urls)
