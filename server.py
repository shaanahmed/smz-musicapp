"""
SMZ Music Player — Core Backend
Version: 7.0 — JioSaavn ONLY
- Search: saavn.dev public API (no API key needed)
- Stream: direct MP3 URLs from Saavn (no IP blocks, no yt-dlp)
- AI: Groq (llama models)
"""

import os, json, threading, re, random, string, time
import urllib.request, urllib.parse
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ══════════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════════
HERE = Path(__file__).parent.resolve()

# ══════════════════════════════════════════════════════════
#  GROQ AI
# ══════════════════════════════════════════════════════════
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
]
_ai_client     = None
_ai_model_name = None

if GROQ_KEY:
    try:
        from groq import Groq
        _ai_client     = Groq(api_key=GROQ_KEY)
        _ai_model_name = GROQ_MODELS[0]
        print(f"  [AI] Groq ready 🧠  model={_ai_model_name}")
    except ImportError:
        print("  [AI] Missing: pip install groq")
    except Exception as e:
        print(f"  [AI] Init error: {e}")
else:
    print("  [AI] No GROQ_API_KEY — AI features disabled.")

# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
def _http_get(url, timeout=10):
    """Simple HTTP GET → parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"  [HTTP] {url[:70]}... → {e}")
        return None

def _clean_json_array(text):
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    s, e = text.find("["), text.rfind("]")
    return text[s:e+1] if s != -1 and e > s else text

def _clean_json_obj(text):
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    s, e = text.find("{"), text.rfind("}")
    return text[s:e+1] if s != -1 and e > s else text

# ══════════════════════════════════════════════════════════
#  AI ASK
# ══════════════════════════════════════════════════════════
def _ai_ask(prompt, max_tokens=512, temperature=0.7):
    if not _ai_client:
        raise RuntimeError("AI not initialised — set GROQ_API_KEY")
    global _ai_model_name
    last_error = None
    for model in GROQ_MODELS:
        try:
            res = _ai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            _ai_model_name = model
            print(f"  [AI] ✓ {model}")
            return res.choices[0].message.content
        except Exception as e:
            last_error = e
            err = str(e)
            print(f"  [AI] {model} failed: {err[:100]}")
            if "rate_limit" in err.lower() or "429" in err:
                time.sleep(3)
                continue
            break
    raise RuntimeError(f"All models failed: {last_error}")

# ══════════════════════════════════════════════════════════
#  JIOSAAVN API
#  saavn.dev is a free public API — no key, no rate limits
# ══════════════════════════════════════════════════════════
SAAVN = "https://jiosaavn-api-instance-mu.vercel.app/api"

def _best_url(download_urls):
    """Pick best quality stream URL from Saavn downloadUrl array."""
    if not download_urls:
        return ""
    for quality in ["320kbps", "160kbps", "96kbps", "48kbps"]:
        for d in download_urls:
            if d.get("quality") == quality and d.get("url"):
                return d["url"]
    # fallback: just return the last one
    return download_urls[-1].get("url", "")

def _fmt_track(s):
    """Format a Saavn song object into our standard track dict."""
    sid      = s.get("id", "")
    imgs     = s.get("image", [])
    thumb    = imgs[-1].get("url", "") if imgs else ""
    artists  = s.get("artists", {}).get("primary", [])
    uploader = ", ".join(a.get("name","") for a in artists) or "Unknown Artist"
    url      = _best_url(s.get("downloadUrl", []))
    return {
        "id":         sid,
        "title":      s.get("name", "Unknown"),
        "uploader":   uploader,
        "thumbnail":  thumb,
        "stream_url": url,       # direct MP3 — browser plays this directly
        "duration":   int(s.get("duration", 0)),
        "source":     "saavn",
    }

def search_audio(query, limit=20):
    """Search JioSaavn. Returns list of tracks with stream_url pre-filled."""
    try:
        q    = urllib.parse.quote(query)
        data = _http_get(f"{SAAVN}/search/songs?query={q}&limit={limit}")
        if not data:
            return []
        songs = data.get("data", {}).get("results", [])
        results = [_fmt_track(s) for s in songs if s.get("id")]
        print(f"  [Saavn] '{query}' → {len(results)} results")
        return results
    except Exception as e:
        print(f"  [Saavn] Search error: {e}")
        return []

def get_stream_url(sid):
    """
    Get fresh stream URL for a Saavn song by ID.
    Called by /api/stream — returns direct MP3 URL.
    """
    try:
        data = _http_get(f"{SAAVN}/songs/{sid}")
        if not data:
            return ""
        songs = data.get("data", [])
        if not songs:
            return ""
        url = _best_url(songs[0].get("downloadUrl", []))
        print(f"  [Saavn] Stream ✓ {sid}")
        return url
    except Exception as e:
        print(f"  [Saavn] Stream error: {e}")
        return ""

# ══════════════════════════════════════════════════════════
#  JOB STORE  (for async AI jobs)
# ══════════════════════════════════════════════════════════
_jobs: dict = {}

def _new_job(prefix="job"):
    jid = prefix + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    _jobs[jid] = {"done": False, "tracks": [], "error": ""}
    return jid

# ══════════════════════════════════════════════════════════
#  FEATURE 1 — MOOD PLAYLIST
# ══════════════════════════════════════════════════════════
def _mood_worker(feeling, jid):
    try:
        prompt = (
            f"User feeling: '{feeling}'.\n"
            "Recommend 10 real well-known songs matching this mood.\n"
            'Reply ONLY with a JSON array: ["Song Title - Artist", ...]\n'
            "No markdown, no explanation."
        )
        raw   = _ai_ask(prompt, max_tokens=400)
        songs = json.loads(_clean_json_array(raw))
        if not isinstance(songs, list) or not songs:
            raise ValueError("Empty response")
    except Exception as e:
        _jobs[jid].update({"error": f"AI failed: {e}", "done": True})
        return

    resolved = []
    for s in songs:
        if isinstance(s, str) and s.strip():
            found = search_audio(s.strip(), limit=3)
            if found:
                resolved.append(found[0])

    _jobs[jid].update({"tracks": resolved, "done": True})
    print(f"  [Mood] Done — {len(resolved)} tracks")

# ══════════════════════════════════════════════════════════
#  FEATURE 2 — TRACK INFO
# ══════════════════════════════════════════════════════════
def get_track_info(title, artist):
    result = {}
    event  = threading.Event()
    def _worker():
        prompt = (
            f'Song: "{title}" by "{artist}".\n'
            "Respond ONLY as JSON:\n"
            '{"vibe":"2-3 sentence mood/sound description",'
            '"tags":["tag1","tag2","tag3","tag4","tag5"],'
            '"fun_fact":"one interesting fact",'
            '"similar_artists":["Artist1","Artist2","Artist3"]}'
            "\nNo markdown."
        )
        try:
            raw  = _ai_ask(prompt, max_tokens=400, temperature=0.6)
            data = json.loads(_clean_json_obj(raw))
            for k in ("vibe", "tags", "fun_fact", "similar_artists"):
                data.setdefault(k, "" if k not in ("tags","similar_artists") else [])
            result.update({"ok": True, "data": data})
        except Exception as e:
            result.update({"ok": False, "error": str(e)})
        finally:
            event.set()
    threading.Thread(target=_worker, daemon=True).start()
    event.wait(timeout=30)
    return result if result else {"ok": False, "error": "Timed out"}

# ══════════════════════════════════════════════════════════
#  FEATURE 3 — SMART RECOMMENDER
# ══════════════════════════════════════════════════════════
def _related_worker(title, artist, jid):
    try:
        prompt = (
            f'Just listened to "{title}" by "{artist}".\n'
            "Suggest 8 songs with a similar vibe.\n"
            "Rules: different artists, same energy/mood/genre.\n"
            'Reply ONLY as JSON array: ["Song - Artist", ...]. No markdown.'
        )
        raw   = _ai_ask(prompt, max_tokens=300, temperature=0.85)
        songs = json.loads(_clean_json_array(raw))
        if not isinstance(songs, list):
            raise ValueError("Bad format")
    except Exception:
        songs = [f"{artist}"]

    resolved = []
    for s in songs:
        if isinstance(s, str) and s.strip():
            found = search_audio(s.strip(), limit=2)
            if found:
                resolved.append(found[0])

    _jobs[jid].update({"tracks": resolved, "done": True})
    print(f"  [Related] Done — {len(resolved)} tracks")

# ══════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════
class SMZHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        # ── Frontend ──────────────────────────────────────
        if p.path in ("/", "/index.html"):
            try:
                fp = (HERE/"static"/"index.html"
                      if (HERE/"static"/"index.html").exists()
                      else HERE/"index.html")
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"error": str(e)}, 404)
            return

        # ── Search ────────────────────────────────────────
        if p.path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            self._json(search_audio(q) if q else [])
            return

        # ── Stream ────────────────────────────────────────
        # Returns {"url": "https://..."} with the direct Saavn MP3 URL
        # Frontend sets audio.src = url and plays directly
        # No proxying needed — Saavn URLs work from any IP!
        if p.path == "/api/stream":
            sid = qs.get("id", [""])[0].strip()
            if not sid:
                self._json({"error": "No id"}, 400)
                return
            url = get_stream_url(sid)
            if url:
                self._json({"url": url})
            else:
                self._json({"error": "Track not found on JioSaavn"}, 404)
            return

        # ── Job poll ──────────────────────────────────────
        if p.path == "/api/job":
            jid = qs.get("id", [""])[0].strip()
            job = _jobs.get(jid)
            self._json(job if job else {"error": "Not found", "done": True},
                       200 if job else 404)
            return

        # ── Track Info ────────────────────────────────────
        if p.path == "/api/info":
            title  = qs.get("title",  [""])[0].strip()
            artist = qs.get("artist", [""])[0].strip()
            if not title:
                self._json({"ok": False, "error": "No title"}, 400)
                return
            if not _ai_client:
                self._json({"ok": False, "error": "AI not available — set GROQ_API_KEY"}, 503)
                return
            self._json(get_track_info(title, artist or "Unknown Artist"))
            return

        # ── Related ───────────────────────────────────────
        if p.path == "/api/related":
            title  = qs.get("title",  [""])[0].strip()
            artist = qs.get("artist", [""])[0].strip()
            if not title:
                self._json([], 400)
                return
            jid = _new_job("rel")
            threading.Thread(
                target=_related_worker,
                args=(title, artist or "Unknown Artist", jid),
                daemon=True
            ).start()
            self._json({"job_id": jid})
            return

        super().do_GET()

    def do_POST(self):
        p      = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        # ── Mood Playlist ─────────────────────────────────
        if p.path == "/api/mood":
            feeling = body.get("feeling", "").strip()
            if not feeling:
                self._json({"error": "No feeling provided"}, 400)
                return
            jid = _new_job("mood")
            threading.Thread(
                target=_mood_worker,
                args=(feeling, jid),
                daemon=True
            ).start()
            self._json({"job_id": jid})
            return

        self._json({"error": "Not found"}, 404)

# ══════════════════════════════════════════════════════════
#  BOOT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 7860))
    print("\n" + "━"*50)
    print("  ✦  SMZ PLAYER  v7.0  ✦")
    print(f"  URL    : http://0.0.0.0:{PORT}")
    print(f"  Source : JioSaavn 🎵 (direct MP3, no blocks)")
    print(f"  AI     : {'Groq (' + _ai_model_name + ')' if _ai_client else 'disabled (no GROQ_API_KEY)'}")
    print("━"*50 + "\n")
    server = HTTPServer(("0.0.0.0", PORT), SMZHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping...")
        server.server_close()