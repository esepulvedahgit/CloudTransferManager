"""Read-only deployment regression: run inside backend against the private proxy.

docker compose exec -T --user ctm backend python < scripts/check_transfer_proxy.py
Unauthenticated requests must reach FastAPI (401), never redirect or create jobs.
"""

import ssl
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


# This internal-only probe bypasses certificate verification because the LAN
# certificate is for the public application hostname, not Docker's service name.
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()), NoRedirect()
)
for method in ("GET", "POST"):
    request = urllib.request.Request(
        "https://reverse-proxy/api/v1/transfers",
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"Host": "transfer.cloudlab.cl:8443", "Content-Type": "application/json"},
    )
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        assert response.status == 401, (
            f"{method}: expected backend 401, got {response.status}; "
            f"redirect={response.headers.get('Location')}"
        )
        assert response.headers.get("Location") is None
    print(f"{method}: reaches backend without redirect (401)", flush=True)
