"""Decisive probe: is the slow first answer 'provider ingestion' or model thinking?
Fresh uploads, asked immediately, with thinking low vs default; media_resolution counts."""
import json, time, urllib.request

ENV = "/opt/apps/gemini-mcp/.env"
KEY = next(l.split("=", 1)[1].strip() for l in open(ENV) if l.startswith("GEMINI_API_KEYS=")).split(",")[0]
BASE = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta"
MODEL = "gemini-3.6-flash"
H = {"x-goog-api-key": KEY, "Content-Type": "application/json"}

def call(method, url, body=None, headers=None, timeout=300):
    req = urllib.request.Request(url, data=body, method=method, headers={**H, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {k.lower(): v for k, v in r.headers.items()}, r.read()

def upload(path):
    mime = "image/png" if path.endswith(".png") else "application/pdf"
    size = __import__("os").path.getsize(path)
    hd, _ = call("POST", f"{UPLOAD}/files",
                 json.dumps({"file": {"display_name": path.split("/")[-1]}}).encode(),
                 {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                  "X-Goog-Upload-Header-Content-Length": str(size),
                  "X-Goog-Upload-Header-Content-Type": mime})
    _, data = call("PUT", hd["x-goog-upload-url"], open(path, "rb").read(),
                   {"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                    "content-type": "application/octet-stream"})
    f = json.loads(data)["file"]
    while f["state"] != "ACTIVE":
        time.sleep(1)
        _, data = call("GET", f"{BASE}/{f['name']}")
        f = json.loads(data)
    return f

def ask(f, prompt, think=None, med=None):
    part = {"file_data": {"mime_type": f["mimeType"], "file_uri": f["uri"]}}
    if med:
        part["media_resolution"] = {"level": med}
    gc = {"thinkingConfig": {"thinkingLevel": think}} if think else {}
    t0 = time.time()
    try:
        _, data = call("POST", f"{BASE}/models/{MODEL}:generateContent", json.dumps(
            {"contents": [{"parts": [part, {"text": prompt}]}], "generationConfig": gc}).encode())
    except urllib.error.HTTPError as e:
        return -1.0, {"error": f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"}
    u = json.loads(data).get("usageMetadata", {})
    return time.time() - t0, u

def count(f, med=None):
    part = {"file_data": {"mime_type": f["mimeType"], "file_uri": f["uri"]}}
    if med:
        part["media_resolution"] = {"level": med}
    try:
        _, data = call("POST", f"{BASE}/models/{MODEL}:countTokens",
                       json.dumps({"contents": [{"parts": [part]}]}).encode())
        return json.loads(data).get("totalTokens")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:160].decode(errors='replace')}"

