import json as jsonlib
import os
from urllib.parse import urlsplit

import requests
from flask import Flask
from werkzeug.test import Client
from werkzeug.wrappers import Response as WerkzeugResponse

# Render wrapper should not require these env vars to be pre-set.
os.environ.setdefault("S1_URL", "http://internal-s1")
os.environ.setdefault("S2_URL", "http://internal-s2")
os.environ.setdefault("S3_URL", "http://internal-s3")

import load_balancer as lb
import server1
import server2
import server3


class InternalResponse:
    def __init__(self, response: WerkzeugResponse):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.get_data(as_text=True)

    def json(self):
        return jsonlib.loads(self.text) if self.text else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} {self.text}")


S1_BASE = lb.server_urls["S1"].rstrip("/")
S2_BASE = lb.server_urls["S2"].rstrip("/")
S3_BASE = lb.server_urls["S3"].rstrip("/")

INTERNAL_CLIENTS = {
    S1_BASE: Client(server1.app, WerkzeugResponse),
    S2_BASE: Client(server2.app, WerkzeugResponse),
    S3_BASE: Client(server3.app, WerkzeugResponse),
}

_real_post = lb.requests.post
_real_put = lb.requests.put
_real_delete = lb.requests.delete


def _match_internal_target(url: str):
    normalized_url = url.rstrip("/")
    for base_url, client in INTERNAL_CLIENTS.items():
        if normalized_url == base_url or normalized_url.startswith(base_url + "/"):
            suffix = normalized_url[len(base_url):] or "/"
            return client, suffix
    return None, None


def _internal_request(method: str, url: str, json=None, timeout=None, **kwargs):
    client, path = _match_internal_target(url)
    if client is None:
        if method == "POST":
            return _real_post(url, json=json, timeout=timeout, **kwargs)
        if method == "PUT":
            return _real_put(url, json=json, timeout=timeout, **kwargs)
        if method == "DELETE":
            return _real_delete(url, timeout=timeout, **kwargs)
        raise ValueError(f"Unsupported method: {method}")

    parsed = urlsplit(url)
    full_path = path
    if parsed.query:
        full_path = f"{full_path}?{parsed.query}"

    payload = None
    headers = {}
    if json is not None:
        payload = jsonlib.dumps(json)
        headers["Content-Type"] = "application/json"

    response = client.open(
        path=full_path,
        method=method,
        data=payload,
        headers=headers,
    )
    return InternalResponse(response)


lb.requests.post = lambda url, json=None, timeout=None, **kwargs: _internal_request(
    "POST", url, json=json, timeout=timeout, **kwargs
)
lb.requests.put = lambda url, json=None, timeout=None, **kwargs: _internal_request(
    "PUT", url, json=json, timeout=timeout, **kwargs
)
lb.requests.delete = lambda url, timeout=None, **kwargs: _internal_request(
    "DELETE", url, timeout=timeout, **kwargs
)

app = Flask(__name__)
app.wsgi_app = lb.app.wsgi_app
