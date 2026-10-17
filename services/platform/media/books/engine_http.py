"""Network port used only inside the isolated upstream rule worker."""

import json
import threading

from ...contracts import require
from .network import Network

network = Network()
request_count = 0
request_bytes = 0
lock = threading.Lock()


def req(
    url,
    cellphone_mode=False,
    cookies="",
    header=None,
    method=0,
    post_data_type=1,
    post_data="",
    post_json=None,
    return_cookies="",
    proxy="",
    proxy_type="",
    timeout=10,
    file_name="",
    file_obj=None,
    allow_redirects=True,
):
    del cellphone_mode, proxy_type, timeout, allow_redirects
    global request_count, request_bytes
    require(not file_name and not proxy, "book_rule_capability_unsupported", 400)
    with lock:
        request_count += 1
        require(request_count <= 64, "book_request_limit", 413)
    headers = dict(header or {})
    if cookies:
        headers["Cookie"] = cookies
    body = post_data if method == 1 else None
    if method == 1 and post_data_type == 2:
        body = json.dumps(post_json or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if isinstance(body, str):
        body = body.encode("utf-8")
    response = network.fetch(
        url, method="POST" if method == 1 else "GET", headers=headers, body=body
    )
    with lock:
        request_bytes += len(response.content)
        require(request_bytes <= 32 * 1024 * 1024, "book_response_too_large", 413)
    if file_obj is not None:
        file_obj.write(response.content)
        return None
    return response.text, return_cookies, response
