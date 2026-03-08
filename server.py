"""
SMZ Music Player — Core Backend (AI Edition)
Version: 5.2 — Fix: stream uses tv_embedded client, AI uses longer backoff + model fallback
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
    if sys.platform != "win32":
        return "ffmpeg"
    for c in [HERE/"ffmpeg.exe", HERE/"ffmpeg"]:
        if c.exists(): return str(c)
    lad = os.environ.get("LOCALAPPDATA", "")
    if lad:
        hits = glob.glob(os.path.join(lad,"Microsoft","WinGet","Packages",
                         "Gyan.FFmpeg*","**","ffmpeg.exe"), recursive=True)
        if hits: return hits[0]
    return "ffmpeg"

# ══════════════════════════════════════════════════════════
#  YTMUSICAPI — optional, with safe fallback
# ══════════════════════════════════════════════════════════
_ytm = None
try:
    from ytmusicapi import YTMusic
    _ytm = YTMusic()
    print("  [Search] ytmusicapi ready ✓")
except ImportError:
    print("  [Search] ytmusicapi not installed — using yt-dlp fallback")
except Exception as e:
    print(f"  [Search] ytmusicapi init failed ({e}) — using yt-dlp fallback")

# ══════════════════════════════════════════════════════════
#  GROQ AI — fast, free, reliable
# ══════════════════════════════════════════════════════════
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

# Models in priority order — falls back if one is overloaded
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

# ══════════════════════════════════════════════════════════
#  AI ASK — single, correct definition
#  NOTE: time.sleep runs in background threads only (mood/related workers)
#  so it never blocks the main server thread
# ══════════════════════════════════════════════════════════
def _ai_ask(prompt, max_tokens=512, temperature=0.7):
    """
    Send prompt to Groq with model fallback.
    Groq is extremely fast — no sleep needed, rarely rate-limits on free tier.
    Always call from a worker thread, never from the main server thread.
    """
    if not _ai_client:
        raise RuntimeError("AI client not initialised — set GROQ_API_KEY")

    global _ai_model_name

    last_error = None
    for model in GROQ_MODELS:
        try:
            response = _ai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            _ai_model_name = model
            print(f"  [AI] ✓ Success with {model}")
            return response.choices[0].message.content
        except Exception as e:
            last_error = e
            err_str = str(e)
            print(f"  [AI] '{model}' failed: {err_str[:120]}")
            # Only retry on rate limit errors
            if "rate_limit" in err_str.lower() or "429" in err_str:
                time.sleep(3)
                continue
            break

    raise RuntimeError(f"All Groq models failed. Last error: {last_error}")

# ══════════════════════════════════════════════════════════
#  AUDIO ENGINE
# ══════════════════════════════════════════════════════════
def search_audio(query, limit=10):
    """
    Primary: ytmusicapi (fast, rarely blocked)
    Fallback: yt-dlp (slower but reliable)
    """
    # --- PRIMARY: ytmusicapi ---
    if _ytm:
        try:
            search_results = _ytm.search(query, filter="songs", limit=limit)
            results = []
            for item in search_results:
                vid = item.get("videoId")
                if not vid:
                    continue
                results.append({
                    "id": vid,
                    "title": item.get("title", "Unknown Track"),
                    "uploader": ", ".join([a["name"] for a in item.get("artists", [])]) if item.get("artists") else "Unknown Artist",
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                })
            if results:
                return results
            print(f"  [Search] ytmusicapi returned 0 results for '{query}', trying yt-dlp...")
        except Exception as e:
            print(f"  [Search] ytmusicapi error: {e} — falling back to yt-dlp")

    # --- FALLBACK: yt-dlp ---
    return _search_ytdlp(query, limit)

def _search_ytdlp(query, limit=10):
    """yt-dlp based search — reliable fallback."""
    safe_q = query.replace('"', '\\"')
    cmd = (
        f'yt-dlp "ytsearch{limit}:{safe_q}" '
        f'--no-warnings --no-download --print-json '
        f'--skip-download 2>/dev/null'
    )
    out, err, code = run_cmd(cmd, timeout=30)
    results = []
    for line in out.strip().splitlines():
        try:
            d = json.loads(line)
            vid = d.get("id") or d.get("webpage_url", "").split("v=")[-1]
            if not vid:
                continue
            results.append({
                "id": vid,
                "title": d.get("title", "Unknown Track"),
                "uploader": d.get("uploader", "Unknown Artist"),
                "thumbnail": d.get("thumbnail", f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
            })
        except Exception:
            continue
    if not results and err:
        print(f"  [Search/yt-dlp] Error: {err[:200]}")
    return results

def get_stream(vid):
    """
    Stream fix for cloud server IPs (Render, HF, etc.)
    tv_embedded is the most reliable client for server IPs — no sign-in needed.
    Falls back through multiple clients automatically.
    """
    # tv_embedded works best on cloud IPs — YouTube treats it as an embedded TV player
    for client in ["tv_embedded", "mediaconnect", "web_creator", "web", "mweb"]:
        cmd = (
            f'yt-dlp -f "bestaudio[ext=m4a]/bestaudio/best" '
            f'--extractor-args "youtube:player_client={client}" '
            f'--get-url --no-warnings --no-check-certificates '
            f'"https://www.youtube.com/watch?v={vid}"'
        )
        out, err, code = run_cmd(cmd, timeout=35)
        url = out.strip().splitlines()[0] if out.strip() else ""
        if url and url.startswith("http"):
            print(f"  [Stream] ✓ Got URL via client={client} for {vid}")
            return url, None
        print(f"  [Stream] ✗ client={client} failed for {vid}: {err[:120]}")

    # Last resort: try without specifying client at all
    cmd = (
        f'yt-dlp -f "bestaudio/best" '
        f'--get-url --no-warnings --no-check-certificates '
        f'"https://www.youtube.com/watch?v={vid}"'
    )
    out, err, _ = run_cmd(cmd, timeout=35)
    url = out.strip().splitlines()[0] if out.strip() else ""
    if url and url.startswith("http"):
        print(f"  [Stream] ✓ Got URL via default client for {vid}")
        return url, None

    return "", "All player clients failed — video may be geo-blocked or unavailable"

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
            if found:
                resolved.append(found[0])

    if not resolved:
        _jobs[jid].update({"error": "No tracks resolved.", "done": True})
        return

    _jobs[jid].update({"tracks": resolved, "done": True})

# ══════════════════════════════════════════════════════════
#  FEATURE 2 — TRACK INFO
# ══════════════════════════════════════════════════════════
def get_track_info(title, artist):
    # get_track_info is called directly from do_GET (main thread),
    # so we run it in a thread to avoid blocking other requests
    result = {}
    event  = threading.Event()

    def _worker():
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
            result.update({"ok": True, "data": data})
        except Exception as e:
            result.update({"ok": False, "error": str(e)})
        finally:
            event.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    event.wait(timeout=30)  # wait up to 30s then give up
    if not result:
        return {"ok": False, "error": "AI request timed out"}
    return result

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
        if not isinstance(songs, list):
            raise ValueError("Bad format")
    except Exception:
        songs = [f"{artist} best songs"]

    resolved = []
    for s in songs:
        if isinstance(s, str) and s.strip():
            found = search_audio(s.strip(), limit=1)
            if found:
                resolved.append(found[0])

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
            if not _ai_client: self._json({"ok": False, "error": "AI not available — set GEMINI_API_KEY"}, 503); return
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

    PORT = int(os.environ.get("PORT", 7860))

    print("\n" + "━"*48)
    print("  ✦  SMZ PLAYER BACKEND  v5.1  ✦")
    print(f"  URL       : http://0.0.0.0:{PORT}")
    print(f"  Features  : Mood Playlist | Track Info | Recommender")
    print(f"  Search    : {'ytmusicapi + yt-dlp fallback' if _ytm else 'yt-dlp only'}")
    print(f"  AI        : {'Gemini ready' if _ai_client else 'disabled (no API key)'}")
    print("━"*48 + "\n")

    server = HTTPServer(("0.0.0.0", PORT), SMZHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopping...")
        server.server_close()