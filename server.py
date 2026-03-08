"""
SMZ Music Player — Core Backend (AI Edition)
Version: 5.0 — New google-genai SDK + No startup ping
"""

import os, sys, json, subprocess, threading, re, random, string, glob, shutil, time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ══════════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════════
HERE = Path(__file__).parent.resolve()
TEMP = HERE / "tmp"

def setup_env():
    try:
        if TEMP.exists(): shutil.rmtree(TEMP)
        TEMP.mkdir(exist_ok=True)
    except Exception as e:
        print(f"  [System] Warning: {e}")

def _find_ffmpeg():
    # 1. First check if we are on a Linux cloud server (like Hugging Face)
    # Most cloud servers have ffmpeg pre-installed in the system path
    if sys.platform != "win32":
        return "ffmpeg"

    # 2. Local Windows logic (keep this so it still works on your PC!)
    for c in [HERE/"ffmpeg.exe", HERE/"ffmpeg"]:
        if c.exists(): return str(c)
    
    lad = os.environ.get("LOCALAPPDATA", "")
    if lad:
        hits = glob.glob(os.path.join(lad,"Microsoft","WinGet","Packages",
                         "Gyan.FFmpeg*","**","ffmpeg.exe"), recursive=True)
        if hits: return hits[0]
        
    return "ffmpeg"

# ══════════════════════════════════════════════════════════
#  GEMINI AI — new google-genai SDK
#  No startup ping (saves quota on every boot)
# ══════════════════════════════════════════════════════════
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

_ai_client     = None
_ai_model_name = None

if GEMINI_KEY:
    try:
        from google import genai
        _ai_client     = genai.Client(api_key=GEMINI_KEY)
        _ai_model_name = GEMINI_MODELS[0]
        print(f"  [AI] Ready 🧠  model={_ai_model_name}")
    except ImportError:
        print("  [AI] Missing: pip install google-genai")
    except Exception as e:
        print(f"  [AI] Init error: {e}")
else:
    print("  [AI] No API key — AI features disabled.")

# ══════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════
def run_cmd(cmd, timeout=45):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, encoding="utf-8", errors="ignore", timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "Timed out", 1
    except Exception as e:
        return "", str(e), 1

def _clean_json_array(text):
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    s, e = text.find("["), text.rfind("]")
    return text[s:e+1] if s != -1 and e > s else text

def _clean_json_obj(text):
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    s, e = text.find("{"), text.rfind("}")
    return text[s:e+1] if s != -1 and e > s else text

def _ai_ask(prompt, max_tokens=512, temperature=0.7):
    """Send prompt to Gemini using new google-genai SDK with model fallback."""
    if not _ai_client:
        raise RuntimeError("AI client not initialised")

    from google.genai import types
    global _ai_model_name

    # --- THE RATE LIMIT FIX ---
    # Pause for 2 seconds before asking Google, to prevent the 429 Quota Error
    time.sleep(2) 

    last_error = None
    for model in GEMINI_MODELS:
        try:
            response = _ai_client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
            )
            _ai_model_name = model
            return response.text
        except Exception as e:
            last_error = e
            print(f"  [AI] '{model}' failed: {e}")
            continue

    raise RuntimeError(f"All models failed. Last: {last_error}")

# ══════════════════════════════════════════════════════════
#  AUDIO ENGINE (ytmusicapi + yt-dlp)
# ══════════════════════════════════════════════════════════
from ytmusicapi import YTMusic
_ytm = YTMusic()

def search_audio(query, limit=10):
    """Uses ytmusicapi for fast, unblockable searching"""
    try:
        search_results = _ytm.search(query, filter="songs", limit=limit)
        results = []
        seen = set()
        
        for item in search_results:
            vid = item.get("videoId")
            if not vid or vid in seen: continue
            seen.add(vid)
            
            title = item.get("title", "Unknown Track")
            artists = ", ".join([a.get("name", "") for a in item.get("artists", [])]) if item.get("artists") else "Unknown Artist"
            
            thumbs = item.get("thumbnails", [])
            thumb = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            
            results.append({
                "id": vid,
                "title": title,
                "uploader": artists,
                "thumbnail": thumb
            })
        return results
    except Exception as e:
        print(f"  [Search] Error: {e}")
        return []

def get_stream(vid):
    """Uses yt-dlp to extract the actual audio URL, spoofing an Android client to bypass blocks"""
    cmd = (f'yt-dlp -f "bestaudio[ext=m4a]/bestaudio/best" '
           f'--extractor-args "youtube:player_client=android" '
           f'--get-url --no-warnings "https://www.youtube.com/watch?v={vid}"')
    
    out, err, _ = run_cmd(cmd, timeout=30)
    url = out.strip().splitlines()[0] if out.strip() else ""
    return url, (err if not url else None)

# ══════════════════════════════════════════════════════════
#  JOB STORE
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
            "Recommend 10 real, well-known songs matching this mood.\n"
            'Reply ONLY with a JSON array of strings: ["Song - Artist", ...]\n'
            "No markdown, no extra text."
        )
        raw   = _ai_ask(prompt, max_tokens=400)
        songs = json.loads(_clean_json_array(raw))
        if not isinstance(songs, list) or not songs:
            raise ValueError("Empty AI response")
    except Exception as e:
        _jobs[jid].update({"error": f"AI failed: {e}", "done": True})
        return

    resolved = []
    for s in songs:
        if isinstance(s, str) and s.strip():
            found = search_audio(s.strip(), limit=2)
            if found: resolved.append(found[0])

    if not resolved:
        _jobs[jid].update({"error": "No tracks resolved.", "done": True})
        return

    _jobs[jid].update({"tracks": resolved, "done": True})

# ══════════════════════════════════════════════════════════
#  FEATURE 2 — TRACK INFO
# ══════════════════════════════════════════════════════════
def get_track_info(title, artist):
    prompt = (
        f'Song: "{title}" by "{artist}".\n'
        "Give a fun music-fan analysis. Respond ONLY as a JSON object:\n"
        '{"vibe":"2-3 sentence mood/sound description",'
        '"tags":["mood","genre","era","tag4","tag5"],'
        '"fun_fact":"one interesting fact",'
        '"similar_artists":["Artist1","Artist2","Artist3"]}'
        "\nNo markdown, pure JSON."
    )
    try:
        raw  = _ai_ask(prompt, max_tokens=400, temperature=0.6)
        data = json.loads(_clean_json_obj(raw))
        for k in ("vibe", "tags", "fun_fact", "similar_artists"):
            data.setdefault(k, "" if k not in ("tags", "similar_artists") else [])
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ══════════════════════════════════════════════════════════
#  FEATURE 3 — SMART RECOMMENDER
# ══════════════════════════════════════════════════════════
def _related_worker(title, artist, jid):
    try:
        prompt = (
            f'User just listened to "{title}" by "{artist}".\n'
            "Suggest 8 different songs with a similar vibe they would love.\n"
            "Rules: different artists, same energy/genre/mood, must exist on YouTube.\n"
            'Reply ONLY as JSON array: ["Song - Artist", ...]. No markdown.'
        )
        raw   = _ai_ask(prompt, max_tokens=300, temperature=0.85)
        songs = json.loads(_clean_json_array(raw))
        if not isinstance(songs, list): raise ValueError("Bad format")
    except Exception:
        songs = [f"{artist} best songs"]

    resolved = []
    for s in songs:
        if isinstance(s, str) and s.strip():
            found = search_audio(s.strip(), limit=1)
            if found: resolved.append(found[0])

    _jobs[jid].update({"tracks": resolved, "done": True})

# ══════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════
class SMZHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200); self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        # Serve frontend
        if p.path in ("/", "/index.html"):
            try:
                fp = (HERE/"static"/"index.html"
                      if (HERE/"static"/"index.html").exists()
                      else HERE/"index.html")
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"error": str(e)}, 404)
            return

        # Search
        if p.path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            self._json(search_audio(q) if q else [])
            return

        # Stream
        if p.path == "/api/stream":
            vid = qs.get("id", [""])[0].strip()
            if not vid: self._json({"error": "No id"}, 400); return
            url, err = get_stream(vid)
            self._json({"url": url} if url else {"error": err or "Unknown error"},
                       200 if url else 500)
            return

        # Job poll
        if p.path == "/api/job":
            jid = qs.get("id", [""])[0].strip()
            job = _jobs.get(jid)
            self._json(job if job else {"error": "Not found", "done": True},
                       200 if job else 404)
            return

        # Track Info
        if p.path == "/api/info":
            title  = qs.get("title",  [""])[0].strip()
            artist = qs.get("artist", [""])[0].strip()
            if not title:      self._json({"ok": False, "error": "No title"}, 400); return
            if not _ai_client: self._json({"ok": False, "error": "AI not available"}, 503); return
            self._json(get_track_info(title, artist or "Unknown Artist"))
            return

        # Related / Recommender
        if p.path == "/api/related":
            title  = qs.get("title",  [""])[0].strip()
            artist = qs.get("artist", [""])[0].strip()
            if not title: self._json([], 400); return
            jid = _new_job("rel")
            threading.Thread(target=_related_worker,
                             args=(title, artist or "Unknown Artist", jid),
                             daemon=True).start()
            self._json({"job_id": jid})
            return

        super().do_GET()

    def do_POST(self):
        p      = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:    body = json.loads(self.rfile.read(length)) if length else {}
        except: body = {}

        # Mood Playlist
        if p.path == "/api/mood":
            feeling = body.get("feeling", "").strip()
            if not feeling: self._json({"error": "No feeling"}, 400); return
            jid = _new_job("mood")
            threading.Thread(target=_mood_worker, args=(feeling, jid), daemon=True).start()
            self._json({"job_id": jid})
            return

        self._json({"error": "Not found"}, 404)

# ══════════════════════════════════════════════════════════
#  BOOT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    setup_env()
    
    # Hugging Face Spaces specifically looks for port 7860
    PORT = int(os.environ.get("PORT", 7860)) 
    
    print("\n" + "━"*48)
    print("  ✦  SMZ PLAYER BACKEND  v5.0  ✦")
    print(f"  URL       : http://0.0.0.0:{PORT}")
    print(f"  Features  : Mood Playlist | Track Info | Recommender")
    print("━"*48 + "\n")
    
    # Start the server on 0.0.0.0 to ensure it's accessible via the tunnel
    server = HTTPServer(("0.0.0.0", PORT), SMZHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopping...")
        server.server_close()


