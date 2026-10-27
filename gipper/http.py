"""One small JSON-over-HTTPS client for every provider: stdlib only, retries on
throttling and 5xx, and the key only ever in a header and scrubbed from errors."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from gipper.config import redact

# Cloudflare in front of Runpod's API rejects Python's default agent (error 1010)
AGENT = "gipper/0.1 (+https://github.com/lavallee/gipper)"


class ProviderError(RuntimeError):
    pass


def request(method, url, body=None, headers=None, params=None, retries=3, timeout=60):
    if params:
        url += "?" + urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()})
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AGENT, **(headers or {})}
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            msg = redact(e.read().decode(errors="replace")[:500])
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise ProviderError(f"{method} {url.split('?')[0]}: HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise ProviderError(f"{method} {url.split('?')[0]}: {getattr(e, 'reason', e)}") from None
