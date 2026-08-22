"""
Shared page fetching.

Both sources sit behind bot protection, so the same two-step applies: try a
plain HTTP request first (free), fall back to Firecrawl (1 credit per page).
Holland2Stay sometimes lets the direct request through; Funda never does.
"""

import logging
import os

import requests

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

log = logging.getLogger("fetcher")


class FetchError(Exception):
    """A page could not be retrieved."""


class SiteUnavailable(FetchError):
    """
    The site answered, but with an error or maintenance page.

    This is transient by nature - the next cycle may well succeed - so callers
    should skip the source quietly instead of raising the alarm.
    """


def direct_get(url, expect=None, timeout=30):
    """
    Try to fetch a URL without spending a credit.

    Returns the body, or None if the request failed or came back as an
    anti-bot interstitial. `expect` is a marker that must appear in a genuine
    response.
    """
    try:
        response = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None
    if expect and expect not in response.text:
        return None
    return response.text


def firecrawl(url, formats, timeout=180):
    """Fetch a page through Firecrawl. Costs 1 credit on the basic proxy."""
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise FetchError("FIRECRAWL_API_KEY is not set")

    payload = {
        "url": url,
        "formats": formats,
        # Funda returns HTTP 500 on the stealth proxy and works fine on basic,
        # which is also the cheaper of the two.
        "proxy": "basic",
        "maxAge": 0,
        "onlyMainContent": True,
    }
    try:
        response = requests.post(
            FIRECRAWL_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FetchError(f"Firecrawl request failed for {url}: {exc}") from exc

    if response.status_code != 200:
        raise FetchError(
            f"Firecrawl HTTP {response.status_code} for {url}: {response.text[:200]}"
        )

    body = response.json()
    if not body.get("success"):
        raise FetchError(f"Firecrawl returned failure for {url}: {str(body)[:200]}")

    data = body.get("data", {})

    # Firecrawl reports a fetch as successful even when the site itself served
    # an error or maintenance page, so check what the site actually said.
    status = data.get("metadata", {}).get("statusCode")
    if isinstance(status, int) and status >= 400:
        title = data.get("metadata", {}).get("title") or ""
        raise SiteUnavailable(f"{url} returned HTTP {status} ({title.strip() or 'no title'})")

    return data
