"""Measure what the provider actually charges for files: token counts by dimension/size,
and how long the first question about a freshly uploaded file really takes.
Stdlib only. Reads the key from the daemon's .env; never prints it."""
import json, os, statistics, struct, sys, time, urllib.request

ENV = "/opt/apps/gemini-mcp/.env"
KEY = next(l.split("=", 1)[1].strip() for l in open(ENV) if l.startswith("GEMINI_API_KEYS=")).split(",")[0]
BASE = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta"
MODEL = "gemini-3.6-flash"
H = {"x-goog-api-key": KEY, "Content-Type": "application/json"}

def call(method, url, body=None, headers=None, timeout=300, tries=5):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, method=method,
                                         headers={**H, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
            last = e
            print(f"  [{e.code} attempt {attempt + 1}/{tries}] retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError) as e:  # noqa: PERF203
            if attempt == tries - 1:
                raise
            last = e
            print(f"  [{e!r:,} attempt {attempt + 1}/{tries}] retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)
    raise last

def upload(path):
    mime = "image/png" if path.endswith(".png") else "image/jpeg"
    size = os.path.getsize(path)
    _, hd, _ = call("POST", f"{UPLOAD}/files",
                    json.dumps({"file": {"display_name": os.path.basename(path)}}).encode(),
                    {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime})
    t0 = time.time()
    _, _, data = call("PUT", hd["x-goog-upload-url"], open(path, "rb").read(),
                      {"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                       "X-Goog-Upload-Command": "upload, finalize",
                       "content-type": "application/octet-stream"})
    f = json.loads(data)["file"]
    polls = 0
    while f["state"] != "ACTIVE" and polls < 60:
        time.sleep(1); polls += 1
        _, _, data = call("GET", f"{BASE}/{f['name']}")
        f = json.loads(data)
    return f, time.time() - t0, polls

def pixel_size(path):
    b = open(path, "rb").read()
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", b[16:24]); return f"{w}x{h}"
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF: i += 1; continue
        m = b[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            h, w = struct.unpack(">HH", b[i + 5:i + 9]); return f"{w}x{h}"
        i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0] if m not in (0xD8, 0xD9) else 2
    return "?"

def count(uri, mime):
    _, _, data = call("POST", f"{BASE}/models/{MODEL}:countTokens",
                      json.dumps({"contents": [{"parts": [
                          {"file_data": {"mime_type": mime, "file_uri": uri}}]}]}).encode())
    return json.loads(data).get("totalTokens")

def ask(uri, mime, prompt, think=None):
    gc = {"thinkingConfig": {"thinkingLevel": think}} if think else {}
    body = json.dumps({"contents": [{"parts": [
        {"file_data": {"mime_type": mime, "file_uri": uri}}, {"text": prompt}]}],
        "generationConfig": gc}).encode()
    t0 = time.time()
    _, _, data = call("POST", f"{BASE}/models/{MODEL}:generateContent", body)
    dt = time.time() - t0
    u = json.loads(data).get("usageMetadata", {})
    return dt, u

files = sys.argv[1:]
print(f"{'file':20} {'px':>11} {'bytes':>9} {'tokens':>7} {'ratio tok/B':>11} {'upload+active':>13}")
state = {}
for p in files:
    f, dt, polls = upload(p)
    n = count(f["uri"], f["mimeType"])
    state[p] = (f, n)
    px = pixel_size(p)
    print(f"{os.path.basename(p):20} {px:>11} {os.path.getsize(p):>9} {n:>7} "
          f"{os.path.getsize(p)/max(1,n):>11.1f} {dt:>12.1f}s")
    sys.stdout.flush()
json.dump({p: state[p][0]["uri"] for p in state}, open("/tmp/probe2/uris.json", "w"))
print("\n-- latency: first vs repeat question on a fresh file --")
f, n = state[files[0]]
for label, prompt, think in [("first  (default thinking)", "What is the dominant color? One word.", None),
                             ("second (same question)", "What is the dominant color? One word.", None),
                             ("third  (other question)", "Is this image mostly noise? Yes or no.", None),
                             ("fourth (thinking low)", "What is the dominant color? One word.", "low")]:
    dt, u = ask(f["uri"], f["mimeType"], prompt, think)
    print(f"{label:28} {dt:6.1f}s  in={u.get('promptTokenCount')} out={u.get('candidatesTokenCount')} "
          f"thoughts={u.get('thoughtsTokenCount', 0)}")
    sys.stdout.flush()
