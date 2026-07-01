"""AI-914: Trade Tariff MCP client for the eval harness.

Ported from bench_sheet/arm_openai.py (the verified working arm). JSON-RPC over
the OTT MCP endpoint, OAuth2 client-credentials auth, 401->refresh retry, browser
UA to clear the CloudFront WAF, `service` forced.

Gated: MCP calls only fire when JOURNEY_ALLOW_MCP_CALLS=1, mirroring
JOURNEY_ALLOW_PROVIDER_CALLS, so default (offline) eval runs stay deterministic.
"""
import json, os, time, urllib.request, urllib.parse, urllib.error

MCP_URL = "https://mcp.trade-tariff.service.gov.uk/"
TOKURL = "https://auth.id.trade-tariff.service.gov.uk/oauth2/token"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0 Safari/537.36")
MCP_TOOLS = {"classification_search", "note_mentions", "show_heading",
             "show_chapter", "lookup_commodity", "navigate_hierarchy"}
_tok = {"v": None, "t": 0.0}


class MCPDisabled(RuntimeError):
    pass


def _post(url, data, headers, timeout=180):
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=timeout)


def enabled():
    return os.environ.get("JOURNEY_ALLOW_MCP_CALLS", "").strip() == "1"


def ott_token(force=False):
    if force or not _tok["v"] or time.time() - _tok["t"] > 3000:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": os.environ["OTT_HUB_CLIENT_ID"],
            "client_secret": os.environ["OTT_HUB_CLIENT_SECRET"],
            "scope": "tariff/read"}).encode()
        r = _post(TOKURL, body, {"Content-Type": "application/x-www-form-urlencoded"}, 30)
        _tok["v"] = json.load(r)["access_token"]; _tok["t"] = time.time()
    return _tok["v"]


def mcp(tool, args, service="uk"):
    """Call an MCP tool. Returns the text content, or a JSON error string."""
    if not enabled():
        raise MCPDisabled("set JOURNEY_ALLOW_MCP_CALLS=1 to allow live MCP calls")
    a = dict(args or {}); a.setdefault("service", service)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": a}}).encode()
    for attempt in (1, 2):
        try:
            r = _post(MCP_URL, payload, {
                "Authorization": "Bearer " + ott_token(force=attempt == 2),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": _UA}, 60)
            d = json.load(r); c = d.get("result", {}).get("content", [])
            return c[0]["text"] if c else json.dumps(d)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                continue
            return json.dumps({"error": "HTTP %d" % e.code})
        except Exception as e:  # noqa
            return json.dumps({"error": str(e)[:120]})
    return json.dumps({"error": "mcp failed"})
