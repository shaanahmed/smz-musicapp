"""
SMZ Music Player — Core Backend (AI Edition)
Version: 6.0 — Multi-source: JioSaavn + YouTube Music + SoundCloud
"""

import os, sys, json, subprocess, threading, re, random, string, glob, shutil, time
import urllib.request, urllib.parse
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

# ══════════════════════════════════════════════════════════
#  YTMUSICAPI — optional, with safe fallback
# ══════════════════════════════════════════════════════════
_ytm = None
try:
    from ytmusicapi import YTMusic
    _ytm = YTMusic()
    print("  [Search] ytmusicapi ready ✓")
except ImportError:
    print("  [Search] ytmusicapi not installed — will use yt-dlp fallback")
except Exception as e:
    print(f"  [Search] ytmusicapi init failed ({e}) — will use yt-dlp fallback")

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

def _http_get(url, timeout=8):
    """Simple HTTP GET, returns parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [HTTP] GET {url[:60]}... failed: {e}")
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
            print(f"  [AI] ✓ {model}")
            return response.choices[0].message.content
        except Exception as e:
            last_error = e
            err_str = str(e)
            print(f"  [AI] '{model}' failed: {err_str[:120]}")
            if "rate_limit" in err_str.lower() or "429" in err_str:
                time.sleep(3)
                continue
            break
    raise RuntimeError(f"All Groq models failed. Last: {last_error}")

# ══════════════════════════════════════════════════════════
#  SOURCE 1 — JIOSAAVN
# ══════════════════════════════════════════════════════════
SAAVN_API = "https://saavn.dev/api"

def _search_saavn(query, limit=5):
    """Search JioSaavn via saavn.dev public API."""
    try:
        q = urllib.parse.quote(query)
        data = _http_get(f"{SAAVN_API}/search/songs?query={q}&limit={limit}")
        if not data:
            return []
        songs = data.get("data", {}).get("results", [])
        results = []
        for s in songs:
            sid = s.get("id", "")
            if not sid:
                continue
            # Pick highest quality stream URL
            dl_urls = s.get("downloadUrl", [])
            stream_url = ""
            for quality in ["320kbps", "160kbps", "96kbps"]:
                for d in dl_urls:
                    if d.get("quality") == quality:
                        stream_url = d.get("url", "")
                        break
                if stream_url:
                    break
            if not stream_url and dl_urls:
                stream_url = dl_urls[-1].get("url", "")

            imgs = s.get("image", [])
            thumb = imgs[-1].get("url", "") if imgs else ""

            results.append({
                "id":         f"saavn_{sid}",
                "title":      s.get("name", "Unknown"),
                "uploader":   ", ".join([a.get("name", "") for a in s.get("artists", {}).get("primary", [])]) or "Unknown Artist",
                "thumbnail":  thumb,
                "source":     "saavn",
                "stream_url": stream_url,
                "duration":   s.get("duration", 0),
            })
        print(f"  [Saavn] Found {len(results)} for '{query}'")
        return results
    except Exception as e:
        print(f"  [Saavn] Search error: {e}")
        return []

# ══════════════════════════════════════════════════════════
#  SOURCE 2 — YOUTUBE MUSIC
# ══════════════════════════════════════════════════════════
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
]

def _search_youtube(query, limit=5):
    """Search YouTube Music via ytmusicapi or yt-dlp fallback."""
    results = []
    if _ytm:
        try:
            hits = _ytm.search(query, filter="songs", limit=limit)
            for item in hits:
                vid = item.get("videoId")
                if not vid:
                    continue
                results.append({
                    "id":         f"yt_{vid}",
                    "title":      item.get("title", "Unknown"),
                    "uploader":   ", ".join([a["name"] for a in item.get("artists", [])]) or "Unknown Artist",
                    "thumbnail":  f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                    "source":     "youtube",
                    "stream_url": "",
                    "duration":   item.get("duration_seconds", 0),
                })
            print(f"  [YouTube] Found {len(results)} for '{query}'")
            return results
        except Exception as e:
            print(f"  [YouTube] ytmusicapi error: {e}")

    # yt-dlp fallback
    safe_q = query.replace('"', '\\"')
    cmd = (f'yt-dlp "ytsearch{limit}:{safe_q}" '
           f'--no-warnings --no-download --print-json --skip-download 2>/dev/null')
    out, _, _ = run_cmd(cmd, timeout=20)
    for line in out.strip().splitlines():
        try:
            d = json.loads(line)
            vid = d.get("id", "")
            if not vid: continue
            results.append({
                "id":         f"yt_{vid}",
                "title":      d.get("title", "Unknown"),
                "uploader":   d.get("uploader", "Unknown Artist"),
                "thumbnail":  d.get("thumbnail", f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"),
                "source":     "youtube",
                "stream_url": "",
                "duration":   d.get("duration", 0),
            })
        except Exception:
            continue
    print(f"  [YouTube/yt-dlp] Found {len(results)} for '{query}'")
    return results

def _stream_youtube(vid):
    """Get stream URL via Piped API then yt-dlp fallback."""
    for instance in PIPED_INSTANCES:
        try:
            data = _http_get(f"{instance}/streams/{vid}", timeout=8)
            if not data: continue
            streams = data.get("audioStreams", [])
            if streams:
                streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                url = streams[0].get("url", "")
                if url:
                    print(f"  [Stream/YT] ✓ Piped {instance}")
                    return url
        except Exception as e:
            print(f"  [Stream/YT] Piped failed: {e}")

    for client in ["tv_embedded", "web", "mweb"]:
        cmd = (f'yt-dlp -f "bestaudio[ext=m4a]/bestaudio/best" '
               f'--extractor-args "youtube:player_client={client}" '
               f'--get-url --no-warnings --no-check-certificates '
               f'"https://www.youtube.com/watch?v={vid}"')
        out, err, _ = run_cmd(cmd, timeout=20)
        url = out.strip().splitlines()[0] if out.strip() else ""
        if url and url.startswith("http"):
            print(f"  [Stream/YT] ✓ yt-dlp client={client}")
            return url
    return ""

# ══════════════════════════════════════════════════════════
#  SOURCE 3 — SOUNDCLOUD
# ══════════════════════════════════════════════════════════
SC_CLIENT_ID = "iZIs9mchVcX5lhVRyQGGAYlNPVldzAoX"

def _search_soundcloud(query, limit=5):
    """Search SoundCloud via public API."""
    try:
        q = urllib.parse.quote(query)
        url = (f"https://api-v2.soundcloud.com/search/tracks"
               f"?q={q}&limit={limit}&client_id={SC_CLIENT_ID}")
        data = _http_get(url, timeout=8)
        if not data:
            return []
        results = []
        for item in data.get("collection", []):
            tid = item.get("id", "")
            if not tid or not item.get("streamable", False):
                continue
            results.append({
                "id":         f"sc_{tid}",
                "title":      item.get("title", "Unknown"),
                "uploader":   item.get("user", {}).get("username", "Unknown Artist"),
                "thumbnail":  (item.get("artwork_url") or
                               item.get("user", {}).get("avatar_url", "")).replace("large", "t500x500"),
                "source":     "soundcloud",
                "stream_url": "",
                "duration":   item.get("duration", 0) // 1000,
            })
        print(f"  [SoundCloud] Found {len(results)} for '{query}'")
        return results
    except Exception as e:
        print(f"  [SoundCloud] Search error: {e}")
        return []

def _stream_soundcloud(track_id):
    """Get stream URL for a SoundCloud track."""
    try:
        direct = (f"https://api.soundcloud.com/tracks/{track_id}/stream"
                  f"?client_id={SC_CLIENT_ID}")
        req = urllib.request.Request(direct, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"  [Stream/SC] ✓ {track_id}")
            return r.url
    except Exception as e:
        print(f"  [Stream/SC] {e}")
    return ""

# ══════════════════════════════════════════════════════════
#  UNIFIED SEARCH — all sources in parallel
# ══════════════════════════════════════════════════════════
def search_audio(query, limit=10):
    """Search JioSaavn + YouTube Music + SoundCloud simultaneously."""
    per_source = max(3, limit // 3)
    results_map = {"saavn": [], "youtube": [], "soundcloud": []}

    def _run(fn, key, q, lim):
        try:
            results_map[key] = fn(q, lim)
        except Exception as e:
            print(f"  [Search/{key}] Thread error: {e}")

    threads = [
        threading.Thread(target=_run, args=(_search_saavn,     "saavn",      query, per_source), daemon=True),
        threading.Thread(target=_run, args=(_search_youtube,   "youtube",    query, per_source), daemon=True),
        threading.Thread(target=_run, args=(_search_soundcloud,"soundcloud", query, per_source), daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=12)

    # Interleave: saavn[0], yt[0], sc[0], saavn[1], yt[1], sc[1]...
    saavn  = results_map["saavn"]
    yt     = results_map["youtube"]
    sc     = results_map["soundcloud"]
    combined = []
    for i in range(max(len(saavn), len(yt), len(sc))):
        if i < len(saavn): combined.append(saavn[i])
        if i < len(yt):    combined.append(yt[i])
        if i < len(sc):    combined.append(sc[i])

    print(f"  [Search] Total={len(combined)} (Saavn={len(saavn)}, YT={len(yt)}, SC={len(sc)})")
    return combined[:limit * 2]

# ══════════════════════════════════════════════════════════
#  UNIFIED STREAM — route by source prefix
# ══════════════════════════════════════════════════════════
def get_stream(track_id):
    """Route stream by prefix: saavn_ / yt_ / sc_"""
    if track_id.startswith("saavn_"):
        sid = track_id[6:]
        try:
            data = _http_get(f"{SAAVN_API}/songs/{sid}")
            if data:
                dl = data.get("data", [{}])[0].get("downloadUrl", [])
                for quality in ["320kbps", "160kbps", "96kbps"]:
                    for d in dl:
                        if d.get("quality") == quality:
                            url = d.get("url", "")
                            if url:
                                print(f"  [Stream/Saavn] ✓ {sid}")
                                return url, None
        except Exception as e:
            print(f"  [Stream/Saavn] {e}")
        return "", "Saavn stream failed"

    elif track_id.startswith("yt_"):
        vid = track_id[3:]
        url = _stream_youtube(vid)
        return (url, None) if url else ("", "YouTube stream failed")

    elif track_id.startswith("sc_"):
        tid = track_id[3:]
        url = _stream_soundcloud(tid)
        return (url, None) if url else ("", "SoundCloud stream failed")

    else:
        # Legacy plain YouTube ID
        url = _stream_youtube(track_id)
        return (url, None) if url else ("", "Stream failed")

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
            found = search_audio(s.strip(), limit=3)
            if found: resolved.append(found[0])

    _jobs[jid].update({"tracks": resolved, "done": True})

# ══════════════════════════════════════════════════════════
#  FEATURE 2 — TRACK INFO
# ══════════════════════════════════════════════════════════
def get_track_info(title, artist):
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
    threading.Thread(target=_worker, daemon=True).start()
    event.wait(timeout=30)
    return result if result else {"ok": False, "error": "Timed out"}

# ══════════════════════════════════════════════════════════
#  FEATURE 3 — SMART RECOMMENDER
# ══════════════════════════════════════════════════════════
def _related_worker(title, artist, jid):
    try:
        prompt = (
            f'User just listened to "{title}" by "{artist}".\n'
            "Suggest 8 different songs with a similar vibe they would love.\n"
            "Rules: different artists, same energy/genre/mood.\n"
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
            found = search_audio(s.strip(), limit=3)
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

        if p.path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            self._json(search_audio(q) if q else [])
            return

        if p.path == "/api/stream":
            vid = qs.get("id", [""])[0].strip()
            if not vid: self._json({"error": "No id"}, 400); return
            url, err = get_stream(vid)
            self._json({"url": url} if url else {"error": err or "Unknown error"},
                       200 if url else 500)
            return

        if p.path == "/api/job":
            jid = qs.get("id", [""])[0].strip()
            job = _jobs.get(jid)
            self._json(job if job else {"error": "Not found", "done": True},
                       200 if job else 404)
            return

        if p.path == "/api/info":
            title  = qs.get("title",  [""])[0].strip()
            artist = qs.get("artist", [""])[0].strip()
            if not title:      self._json({"ok": False, "error": "No title"}, 400); return
            if not _ai_client: self._json({"ok": False, "error": "AI not available"}, 503); return
            self._json(get_track_info(title, artist or "Unknown Artist"))
            return

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
    print("\n" + "━"*52)
    print("  ✦  SMZ PLAYER BACKEND  v6.0  ✦")
    print(f"  URL     : http://0.0.0.0:{PORT}")
    print(f"  Sources : JioSaavn 🎵 | YouTube ▶️ | SoundCloud ☁️")
    print(f"  Search  : {'ytmusicapi + yt-dlp' if _ytm else 'yt-dlp only'}")
    print(f"  AI      : {'Groq (' + _ai_model_name + ')' if _ai_client else 'disabled'}")
    print("━"*52 + "\n")
    server = HTTPServer(("0.0.0.0", PORT), SMZHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopping...")
        server.server_close()