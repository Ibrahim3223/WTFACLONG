# autoshorts_daily.py — Longform-safe: decoupled 5s visuals, no captions, min-length guard
# -*- coding: utf-8 -*-
import os, sys, re, json, time, random, datetime, tempfile, pathlib, subprocess, hashlib, math, shutil
from typing import List, Optional, Tuple, Dict, Any, Set

# ==== LONGFORM/ASPECT SWITCH (unchanged base) =========================================
ASPECT_RAW = (os.getenv("ASPECT", "9:16") or "9:16").strip().lower()
LONGFORM   = (os.getenv("LONGFORM", os.getenv("LONGFORM_ENABLE", "0")) == "1")
if ASPECT_RAW in {"16:9", "landscape", "widescreen"}:
    VIDEO_W, VIDEO_H = 1920, 1080
    PEXELS_ORIENT = "landscape"
else:
    VIDEO_W, VIDEO_H = 1080, 1920
    PEXELS_ORIENT = "portrait"

# ---- focus-entity cooldown (same) ----
ENTITY_COOLDOWN_DAYS = int(os.getenv("ENTITY_COOLDOWN_DAYS", os.getenv("NOVELTY_WINDOW", "30")))
_GENERIC_SKIP = {
    "country","countries","people","history","stories","story","facts","fact","amazing","weird","random","culture","cultural",
    "animal","animals","nature","wild","pattern","patterns","science","eco","habit","habits","waste","tip","tips","daily","news",
    "world","today","minute","short","video","watch","more","better","twist","comment","voice","narration","hook","topic",
    "secret","secrets","unknown","things","life","lived","modern","time","times","explained","guide","quick","fix","fixes"
}
def _tok_words_loose(s: str) -> List[str]:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return [w for w in s.split() if len(w) >= 3]
def _derive_focus_entity(topic: str, mode: str, sentences: list[str]) -> str:
    txt = " ".join(sentences or []) + " " + (topic or "")
    words = _tok_words_loose(txt)
    from collections import Counter as _C
    cnt = _C([w for w in words if w not in _GENERIC_SKIP])
    if not cnt: return ""
    bigrams = _C([" ".join(words[i:i+2]) for i in range(len(words)-1)])
    for bg,_ in bigrams.most_common(10):
        if all(w not in _GENERIC_SKIP for w in bg.split()) and len(bg) >= 7:
            parts = [w for w in bg.split() if w not in _GENERIC_SKIP]
            if parts: return parts[-1]
    for w,_ in cnt.most_common(20):
        if len(w) >= 4: return w
    return next(iter(cnt.keys())) if cnt else ""
def _entity_key(mode: str, ent: str) -> str:
    ent = re.sub(r"[^a-z0-9]+","-", (ent or "").lower()).strip("-")
    mode = (mode or "").lower()
    return f"{mode}:{ent}" if ent else ""
def _entities_state_load() -> dict:
    try:
        gst = _global_topics_load()
    except Exception:
        gst = {}
    ents = (gst.get("entities") if isinstance(gst, dict) else None) or {}
    if not isinstance(ents, dict): ents = {}
    return ents
def _entities_state_save(ents: dict):
    try:
        gst = _global_topics_load()
    except Exception:
        gst = {}
    if isinstance(gst, dict):
        gst["entities"] = ents
        if len(ents) > 12000:
            oldest = sorted(ents.items(), key=lambda kv: kv[1])[:2000]
            for k,_ in oldest: ents.pop(k, None)
            gst["entities"] = ents
        _global_topics_save(gst)
def _entity_in_cooldown(key: str, days: int) -> bool:
    if not key or days <= 0: return False
    ents = _entities_state_load()
    ts = ents.get(key)
    if not ts: return False
    try:
        age = time.time() - float(ts)
    except Exception:
        return False
    return age < days * 86400
def _entity_touch(key: str):
    if not key: return
    ents = _entities_state_load()
    ents[key] = time.time()
    _entities_state_save(ents)

# ---------- helpers ----------
def _env_int(name: str, default: int) -> int:
    s = os.getenv(name)
    if s is None: return default
    s = str(s).strip()
    if s == "" or s.lower() == "none": return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except Exception:
            return default
def _env_float(name: str, default: float) -> float:
    s = os.getenv(name)
    if s is None: return default
    s = str(s).strip()
    if s == "" or s.lower() == "none": return default
    try:
        return float(s)
    except Exception:
        return default
def _sanitize_lang(val: Optional[str]) -> str:
    val = (val or "").strip()
    if not val: return "en"
    m = re.match(r"([A-Za-z]{2})", val)
    return (m.group(1).lower() if m else "en")
def _sanitize_privacy(val: Optional[str]) -> str:
    v = (val or "").strip().lower()
    return v if v in {"public", "unlisted", "private"} else "public"

# Karaoke offsets (kept for compatibility though captions are off by default)
KARAOKE_OFFSET_MS = int(os.getenv("KARAOKE_OFFSET_MS", "0"))
KARAOKE_SPEED = float(os.getenv("KARAOKE_SPEED", "1.0"))
def _adj_time(t_seconds: float) -> float:
    return max(0.0, (t_seconds + KARAOKE_OFFSET_MS / 1000.0) / max(KARAOKE_SPEED, 1e-6))

# ==================== ENV / constants ====================
VOICE_STYLE    = os.getenv("TTS_STYLE", "narration-professional")
TARGET_MIN_SEC = _env_float("TARGET_MIN_SEC", 180.0 if LONGFORM else 22.0)
TARGET_MAX_SEC = _env_float("TARGET_MAX_SEC", 300.0 if LONGFORM else 42.0)

CHANNEL_NAME   = os.getenv("CHANNEL_NAME", "DefaultChannel")
MODE           = os.getenv("MODE", "freeform").strip().lower()
LANG           = _sanitize_lang(os.getenv("VIDEO_LANG") or os.getenv("LANG") or "en")
VISIBILITY     = _sanitize_privacy(os.getenv("VISIBILITY"))
ROTATION_SEED  = _env_int("ROTATION_SEED", 0)

# === Captions OFF by default for longform ===
CAPTIONS_ENABLE   = os.getenv("CAPTIONS_ENABLE", "0") == "1"   # NEW: global switch
REQUIRE_CAPTIONS  = False  # enforce off regardless of old flag
KARAOKE_CAPTIONS  = False  # enforce off

OUT_DIR        = "out"; pathlib.Path(OUT_DIR).mkdir(exist_ok=True)

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
USE_GEMINI     = os.getenv("USE_GEMINI", "1") == "1"
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_PROMPT  = (os.getenv("GEMINI_PROMPT") or "").strip()
GEMINI_TEMP    = _env_float("GEMINI_TEMP", 0.85)

# ---- CTA ----
CTA_ENABLE      = os.getenv("CTA_ENABLE", "1") == "1"
CTA_SHOW_SEC    = _env_float("CTA_SHOW_SEC", 2.8)
CTA_MAX_CHARS   = _env_int("CTA_MAX_CHARS", 64)
CTA_TEXT_FORCE  = (os.getenv("CTA_TEXT") or "").strip()

# ---- Topic & user seed terms ----
TOPIC_RAW = os.getenv("TOPIC", "").strip()
TOPIC = re.sub(r'^[\'"]|[\'"]$', '', TOPIC_RAW).strip()
def _parse_terms(s: str) -> List[str]:
    s = (s or "").strip()
    if not s: return []
    try:
        data = json.loads(s)
        if isinstance(data, list): return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    s = re.sub(r'^[\[\(]|\s*[\]\)]$', '', s)
    parts = re.split(r'\s*,\s*', s)
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()]
SEARCH_TERMS_ENV = _parse_terms(os.getenv("SEARCH_TERMS", ""))

TARGET_FPS       = int(os.getenv("TARGET_FPS", "25"))
CRF_VISUAL       = 22

# ---------- Pexels ayarları ----------
PEXELS_PER_PAGE            = int(os.getenv("PEXELS_PER_PAGE", "30"))
PEXELS_MAX_USES_PER_CLIP   = int(os.getenv("PEXELS_MAX_USES_PER_CLIP", "1"))
PEXELS_ALLOW_REUSE         = os.getenv("PEXELS_ALLOW_REUSE", "1") == "1"   # default ON (we handle reuse rules separately)
PEXELS_ALLOW_LANDSCAPE     = os.getenv("PEXELS_ALLOW_LANDSCAPE", "1") == "1"
PEXELS_MIN_DURATION        = int(os.getenv("PEXELS_MIN_DURATION", "3"))
PEXELS_MAX_DURATION        = int(os.getenv("PEXELS_MAX_DURATION", "13"))
PEXELS_MIN_HEIGHT          = int(os.getenv("PEXELS_MIN_HEIGHT",   "1280"))
PEXELS_STRICT_VERTICAL     = os.getenv("PEXELS_STRICT_VERTICAL", "1") == "1"

ALLOW_PIXABAY_FALLBACK     = os.getenv("ALLOW_PIXABAY_FALLBACK", "1") == "1"
PIXABAY_API_KEY            = os.getenv("PIXABAY_API_KEY", "").strip()

# === NOVELTY ===
NOVELTY_ENFORCE       = os.getenv("NOVELTY_ENFORCE", "1") == "1"
NOVELTY_WINDOW        = _env_int("NOVELTY_WINDOW", 40)
NOVELTY_JACCARD_MAX   = _env_float("NOVELTY_JACCARD_MAX", 0.55)
NOVELTY_RETRIES       = _env_int("NOVELTY_RETRIES", 4)

# === BGM ===
BGM_ENABLE  = os.getenv("BGM_ENABLE", "0") == "1"
BGM_DB      = _env_float("BGM_DB", -26.0)
BGM_DUCK_DB = _env_float("BGM_DUCK_DB", -12.0)
BGM_FADE    = _env_float("BGM_FADE", 0.8)
BGM_DIR     = os.getenv("BGM_DIR", "bgm").strip()
BGM_URLS    = _parse_terms(os.getenv("BGM_URLS", ""))

# === NEW: Visual montage options (decoupled) ===
VIS_SLOT_SEC          = _env_float("VIS_SLOT_SEC", 5.0)   # every 5 seconds a new b-roll
VIS_MAX_REUSE_PER_CLIP= _env_int("VIS_MAX_REUSE_PER_CLIP", 3)
VIS_NO_BACK_TO_BACK   = os.getenv("VIS_NO_BACK_TO_BACK", "1") == "1"
MIN_SCENES_LONGFORM   = _env_int("MIN_SCENES_LONGFORM", 10)  # require at least 10 scenes for longform attempt
WPS_BASE_EST          = _env_float("TTS_WPS_EST", 2.1)   # words/sec base (adjusted by atempo)

# ==================== deps (auto-install) ====================
def _pip(p): subprocess.run([sys.executable, "-m", "pip", "install", "-q", p], check=True)
try:
    import requests
except ImportError:
    _pip("requests"); import requests
try:
    import edge_tts, nest_asyncio
except ImportError:
    _pip("edge-tts"); _pip("nest_asyncio"); import edge_tts, nest_asyncio
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    _pip("google-api-python-client"); from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
except ImportError:
    _pip("google-auth"); from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

# ==================== Voices ====================
VOICE_OPTIONS = {
    "en": [
        "en-US-JennyNeural","en-US-JasonNeural","en-US-AriaNeural","en-US-GuyNeural",
        "en-AU-NatashaNeural","en-GB-SoniaNeural","en-CA-LiamNeural","en-US-DavisNeural","en-US-AmberNeural"
    ],
    "tr": ["tr-TR-EmelNeural","tr-TR-AhmetNeural"]
}
VOICE = os.getenv("TTS_VOICE", VOICE_OPTIONS.get(LANG, ["en-US-JennyNeural"])[0])

# ==================== Utils ====================
def run(cmd, check=True):
    res = subprocess.run(cmd, text=True, capture_output=True)
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr[:4000])
    return res
def ffprobe_dur(p):
    try:
        out = run(["ffprobe","-v","quiet","-show_entries","format=duration","-of","csv=p=0", p]).stdout.strip()
        return float(out) if out else 0.0
    except:
        return 0.0
def ffmpeg_has_filter(name: str) -> bool:
    try:
        out = run(["ffmpeg","-hide_banner","-filters"], check=False).stdout
        return bool(re.search(rf"\b{name}\b", out))
    except Exception:
        return False
_HAS_DRAWTEXT   = ffmpeg_has_filter("drawtext")
_HAS_SUBTITLES  = ffmpeg_has_filter("subtitles")
_HAS_SIDECHAIN  = ffmpeg_has_filter("sidechaincompress")
def font_path():
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "C:/Windows/Fonts/arial.ttf"]:
        if pathlib.Path(p).exists():
            return p
    return ""
def _ff_sanitize_font(font_path_str: str) -> str:
    if not font_path_str: return ""
    return font_path_str.replace(":", r"\:").replace(",", r"\,").replace("\\", "/")
def _ff_sanitize_path(path_str: str) -> str:
    if not path_str: return ""
    return path_str.replace("\\", "/").replace(":", r"\:").replace(",", r"\,")

def normalize_sentence(raw: str) -> str:
    s = (raw or "").strip()
    s = s.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in s.split("\n"))
    s = s.replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"').replace("’", "'")
    s = re.sub(r"[\u200B-\u200D\uFEFF]", "", s)
    return s

# ---------- CTA keyword helpers (unchanged) ----------
_STOP_EN = set("the a an and or but if while of to in on at from by with for about into over after before between during under above across around through this that these those is are was were be been being have has had do does did can could should would may might will your you we our they their he she it its as than then so very more most many much just also only even still yet".split())
_STOP_TR = set("ve ya ama eğer iken ile için üzerine altında üzerinde arasında boyunca sonra önce boyunca altında üstünde hakkında üzerinden arasında bu şu o bir birisi şunlar bunlar biz siz onlar var yok çok daha en ise çünkü gibi kadar zaten sadece yine hâlâ".split())
def _kw_tokens(text: str, lang: str) -> list[str]:
    t = re.sub(r"[^A-Za-zçğıöşüÇĞİÖŞÜ0-9 ]+", " ", (text or "")).lower()
    ws = [w for w in t.split() if len(w) >= 4 and w not in (_STOP_TR if lang.startswith("tr") else _STOP_EN)]
    return ws
def _top_keywords(topic: str, sentences: list[str], lang: str, k: int = 6) -> list[str]:
    from collections import Counter
    cnt = Counter()
    for s in [topic] + list(sentences or []):
        for w in _kw_tokens(s, lang):
            cnt[w] += 1
    bigr = Counter()
    toks_all = _kw_tokens(" ".join([topic] + sentences), lang)
    for i in range(len(toks_all)-1):
        bigr[toks_all[i] + " " + toks_all[i+1]] += 1
    scored = []
    for w,c in cnt.items(): scored.append((c, w))
    for bg,c in bigr.items(): scored.append((c*2, bg))
    scored.sort(reverse=True)
    out=[]
    for _,w in scored:
        if w not in out: out.append(w)
        if len(out) >= k: break
    return out
def build_contextual_cta(topic: str, sentences: list[str], lang: str) -> str:
    if CTA_TEXT_FORCE: return CTA_TEXT_FORCE.strip()
    kws = _top_keywords(topic or "", sentences or [], lang)
    a = (kws[0] if kws else (topic or "").lower())
    b = (kws[1] if len(kws) > 1 else "")
    rng = random.Random((ROTATION_SEED or int(time.time())) + len("".join(sentences)))
    if lang.startswith("tr"):
        templates = [
            lambda a,b: f"Sence en şaşırtan neydi: {a} mı {b} mi?" if b else f"Sence en şaşırtan neydi: {a}?",
            lambda a,b: f"{a} için daha iyi bir fikir var mı? Yorumla!",
            lambda a,b: f"İlk hangisini denerdin: {a} mı {b} mi?" if b else f"{a} sence işe yarar mı?",
            lambda a,b: f"3 kelimeyle yorumla: {a}",
            lambda a,b: f"Detayı yakaladın mı? Nerede? Yaz 📝"
        ]
    else:
        templates = [
            lambda a,b: f"Which surprised you more: {a} or {b}?" if b else f"What surprised you most about {a}?",
            lambda a,b: f"Got a smarter fix for {a}? Drop it below!",
            lambda a,b: f"First pick: {a} or {b}?" if b else f"Would you try {a} first?",
            lambda a,b: f"Sum it up in 3 words: {a}",
            lambda a,b: f"Spot the tiny clue? Where? Comment!"
        ]
    for _ in range(10):
        t = templates[rng.randrange(len(templates))](a, b).strip()
        t = re.sub(r"\s+", " ", t)
        if len(t) <= CTA_MAX_CHARS:
            return t
    return (templates[0](a,b))[:CTA_MAX_CHARS]

# ==================== State (unchanged) ====================
STATE_FILE = f"state_{re.sub(r'[^A-Za-z0-9]+','_',CHANNEL_NAME)}.json"
GLOBAL_TOPIC_STATE = "state_global_topics.json"
LEGACY_STATE_FILE = f"state_{CHANNEL_NAME}.json"
LEGACY_GLOBAL_STATE = "state_global.json"
def _load_json(path, default): 
    try: return json.load(open(path, "r", encoding="utf-8"))
    except: return default
def _save_json(path, data):
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    pathlib.Path(path).write_text(txt, encoding="utf-8")
    try:
        if path == STATE_FILE: pathlib.Path(LEGACY_STATE_FILE).write_text(txt, encoding="utf-8")
        if path == GLOBAL_TOPIC_STATE: pathlib.Path(LEGACY_GLOBAL_STATE).write_text(txt, encoding="utf-8")
    except Exception: pass
def _state_load() -> dict:
    if pathlib.Path(STATE_FILE).exists(): return _load_json(STATE_FILE, {"recent": [], "used_pexels_ids": []})
    if pathlib.Path(LEGACY_STATE_FILE).exists():
        st = _load_json(LEGACY_STATE_FILE, {"recent": [], "used_pexels_ids": []}); _save_json(STATE_FILE, st); return st
    return {"recent": [], "used_pexels_ids": []}
def _state_save(st: dict):
    st["recent"] = st.get("recent", [])[-1200:]
    st["used_pexels_ids"] = st.get("used_pexels_ids", [])[-5000:]
    _save_json(STATE_FILE, st)
def _global_topics_load() -> dict:
    default = {"recent_topics": []}
    if pathlib.Path(GLOBAL_TOPIC_STATE).exists(): return _load_json(GLOBAL_TOPIC_STATE, default)
    if pathlib.Path(LEGACY_GLOBAL_STATE).exists():
        gst = _load_json(LEGACY_GLOBAL_STATE, default); _save_json(GLOBAL_TOPIC_STATE, gst); return gst
    return default
def _global_topics_save(gst: dict):
    gst["recent_topics"] = gst.get("recent_topics", [])[-4000:]
    _save_json(GLOBAL_TOPIC_STATE, gst)
def _hash12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]
def _record_recent(h: str, mode: str, topic: str, fp: Optional[List[str]] = None):
    st = _state_load()
    rec = {"h":h,"mode":mode,"topic":topic,"ts":time.time()}
    if fp: rec["fp"] = list(fp)
    st.setdefault("recent", []).append(rec)
    _state_save(st)
    gst = _global_topics_load()
    if topic and topic not in gst["recent_topics"]:
        gst["recent_topics"].append(topic); _global_topics_save(gst)
def _blocklist_add_pexels(ids: List[int], days=30):
    st = _state_load(); now = int(time.time())
    for vid in ids: st.setdefault("used_pexels_ids", []).append({"id": int(vid), "ts": now})
    cutoff = now - days*86400
    st["used_pexels_ids"] = [x for x in st.get("used_pexels_ids", []) if x.get("ts",0) >= cutoff]
    _state_save(st)
def _blocklist_get_pexels() -> set:
    st = _state_load(); return {int(x["id"]) for x in st.get("used_pexels_ids", [])}
def _recent_topics_for_prompt(limit=20) -> List[str]:
    gst = _global_topics_load(); topics = list(reversed(gst.get("recent_topics", [])))
    uniq=[]; 
    for t in topics:
        if t and t not in uniq: uniq.append(t)
        if len(uniq) >= limit: break
    return uniq

# ---- novelty helpers (unchanged) ----
def _tok_words(s: str) -> List[str]:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return [w for w in s.split() if len(w) >= 3]
def _trigrams(words: List[str]) -> Set[str]:
    return {" ".join(words[i:i+3]) for i in range(len(words)-2)} if len(words) >= 3 else set()
def _sentences_fp(sentences: List[str]) -> Set[str]:
    ws = _tok_words(" ".join(sentences or []))
    return _trigrams(ws)
def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b: return 0.0
    inter = len(a & b); union = len(a | b)
    return (inter / union) if union else 0.0
def _recent_fps_from_state(limit: int = NOVELTY_WINDOW) -> List[Set[str]]:
    st = _state_load(); out=[]
    for item in reversed(st.get("recent", [])):
        fp = item.get("fp")
        if isinstance(fp, list): out.append(set(fp))
        if len(out) >= limit: break
    return out
def _novelty_ok(sentences: List[str]) -> Tuple[bool, List[str]]:
    if not NOVELTY_ENFORCE: return True, []
    cur = _sentences_fp(sentences)
    if not cur: return True, []
    for fp in _recent_fps_from_state(NOVELTY_WINDOW):
        sim = _jaccard(cur, fp)
        if sim > NOVELTY_JACCARD_MAX:
            common = list(cur & fp); terms = []
            for tri in common[:40]:
                for w in tri.split():
                    if len(w) >= 4 and w not in terms: terms.append(w)
                    if len(terms) >= 12: break
                if len(terms) >= 12: break
            return False, terms
    return True, []

# ==================== TTS ====================
def _rate_to_atempo(rate_str: str, default: float = 1.10) -> float:
    try:
        if not rate_str: return default
        rate_str = rate_str.strip()
        if rate_str.endswith("%"):
            val = float(rate_str.replace("%","")); return max(0.5, min(2.0, 1.0 + val/100.0))
        if rate_str.endswith(("x","X")): return max(0.5, min(2.0, float(rate_str[:-1])))
        v = float(rate_str); return max(0.5, min(2.0, v))
    except Exception:
        return default

def _edge_stream_tts(text: str, voice: str, rate_env: str, mp3_out: str) -> List[Dict[str,Any]]:
    import asyncio
    marks: List[Dict[str,Any]] = []
    async def _run():
        audio = bytearray()
        comm = edge_tts.Communicate(text, voice=voice, rate=rate_env)
        async for chunk in comm.stream():
            t = chunk.get("type")
            if t == "audio": audio.extend(chunk.get("data", b""))
            elif t == "WordBoundary":
                off = float(chunk.get("offset", 0))/10_000_000.0
                dur = float(chunk.get("duration",0))/10_000_000.0
                marks.append({"t0": off, "t1": off+dur, "text": str(chunk.get("text",""))})
        open(mp3_out, "wb").write(bytes(audio))
    try:
        asyncio.run(_run())
    except RuntimeError:
        nest_asyncio.apply(); loop = asyncio.get_event_loop(); loop.run_until_complete(_run())
    return marks

def _merge_marks_to_words(text: str, marks: List[Dict[str,Any]], total: float) -> List[Tuple[str,float]]:
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    if not words: return []
    out=[]
    if marks:
        ms = [m for m in marks if (m.get("t1",0) > m.get("t0",0))]
        if len(ms) >= len(words)*0.6:
            N = min(len(words), len(ms))
            raw_durs = [max(0.02, float(ms[i]["t1"]-ms[i]["t0"])) for i in range(N)]
            sum_raw = sum(raw_durs) if raw_durs else 0.0
            scale = (total / sum_raw) if sum_raw > 0 else 1.0
            for i in range(N): out.append((words[i], max(0.05, raw_durs[i]*scale)))
            remain = max(0.0, total - sum(d for _,d in out))
            if len(words) > N and remain>0:
                each = remain/(len(words)-N)
                for i in range(N, len(words)):
                    out.append((words[i], max(0.05, each)))
        else:
            out=[]
    if not out:
        each = max(0.05, total/max(1,len(words)))
        out = [(w, each) for w in words]
        s = sum(d for _,d in out)
        if s>0 and abs(s-total)>0.02:
            out[-1] = (out[-1][0], max(0.05, out[-1][1] + (total-s)))
    return out

def tts_to_wav(text: str, wav_out: str) -> Tuple[float, List[Tuple[str,float]]]:
    import asyncio
    from aiohttp.client_exceptions import WSServerHandshakeError
    text = (text or "").strip()
    if not text:
        run(["ffmpeg","-y","-f","lavfi","-t","0.6","-i","anullsrc=r=48000:cl=mono", wav_out])
        return 0.6, []
    mp3 = wav_out.replace(".wav", ".mp3")
    rate_env = os.getenv("TTS_RATE", "+12%")
    atempo = _rate_to_atempo(rate_env, default=1.12)
    available = VOICE_OPTIONS.get(LANG, ["en-US-JennyNeural"])
    selected_voice = VOICE if VOICE in available else available[0]
    marks: List[Dict[str,Any]] = []
    try:
        marks = _edge_stream_tts(text, selected_voice, rate_env, mp3)
        run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", mp3, "-ar","48000","-ac","1","-acodec","pcm_s16le",
            "-af", f"dynaudnorm=g=7:f=250,atempo={atempo}",
            wav_out
        ])
        pathlib.Path(mp3).unlink(missing_ok=True)
        dur = ffprobe_dur(wav_out) or 0.0
        words = _merge_marks_to_words(text, marks, dur)
        return dur, words
    except WSServerHandshakeError as e:
        if getattr(e, "status", None) != 401 and "401" not in str(e):
            print(f"⚠️ edge-tts stream fail: {e}")
    except Exception as e:
        print(f"⚠️ edge-tts stream fail: {e}")
    try:
        async def _edge_save_simple():
            comm = edge_tts.Communicate(text, voice=selected_voice, rate=rate_env)
            await comm.save(mp3)
        try:
            asyncio.run(_edge_save_simple())
        except RuntimeError:
            nest_asyncio.apply(); loop = asyncio.get_event_loop(); loop.run_until_complete(_edge_save_simple())
        run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", mp3, "-ar","48000","-ac","1","-acodec","pcm_s16le",
            "-af", f"dynaudnorm=g=7:f=300,atempo={atempo}",
            wav_out
        ])
        pathlib.Path(mp3).unlink(missing_ok=True)
        dur = ffprobe_dur(wav_out) or 0.0
        words = _merge_marks_to_words(text, [], dur)
        return dur, words
    except Exception as e:
        print(f"⚠️ edge-tts 401 → fallback TTS")
    try:
        q = requests.utils.quote(text.replace('"','').replace("'",""))
        lang_code = LANG or "en"
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={q}&tl={lang_code}&client=tw-ob&ttsspeed=1.0"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=30); r.raise_for_status()
        open(mp3, "wb").write(r.content)
        run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", mp3, "-ar","48000","-ac","1","-acodec","pcm_s16le",
            "-af", f"dynaudnorm=g=6:f=300,atempo={atempo}", wav_out
        ])
        pathlib.Path(mp3).unlink(missing_ok=True)
        dur = ffprobe_dur(wav_out) or 0.0
        words = _merge_marks_to_words(text, [], dur)
        return dur, words
    except Exception as e2:
        print(f"❌ TTS tüm yollar başarısız, kısa sessizlik üretilecek: {e2}")
        run(["ffmpeg","-y","-f","lavfi","-t","1.0","-i","anullsrc=r=48000:cl=mono", wav_out])
        return 1.0, []

# ==================== Video helpers ====================
def quantize_to_frames(seconds: float, fps: int = TARGET_FPS) -> Tuple[int, float]:
    frames = max(2, int(round(seconds * fps)))
    return frames, frames / float(fps)

def make_segment(src: str, dur_s: float, outp: str):
    frames, qdur = quantize_to_frames(dur_s, TARGET_FPS)
    fade = max(0.08, min(0.22, qdur/8.0))
    fade_out_st = max(0.0, qdur - fade)
    vf = (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},"
        "eq=brightness=0.02:contrast=1.08:saturation=1.1,"
        f"fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB,"
        f"fade=t=in:st=0:d={fade:.2f},fade=t=out:st={fade_out_st:.2f}:d={fade:.2f}"
    )
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-stream_loop","-1","-t", f"{qdur:.3f}",
        "-i", src,
        "-vf", vf, "-r", str(TARGET_FPS), "-vsync","cfr",
        "-an","-c:v","libx264","-preset","fast","-crf",str(CRF_VISUAL),
        "-pix_fmt","yuv420p","-movflags","+faststart",
        outp
    ])

def enforce_video_exact_frames(video_in: str, target_frames: int, outp: str):
    target_frames = max(2, int(target_frames))
    vf = f"fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB,trim=start_frame=0:end_frame={target_frames}"
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", video_in,
        "-vf", vf, "-r", str(TARGET_FPS), "-vsync","cfr",
        "-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),
        "-pix_fmt","yuv420p","-movflags","+faststart",
        outp
    ])

def overlay_cta_tail(video_in: str, text: str, outp: str, show_sec: float, font: str):
    vdur = ffprobe_dur(video_in)
    if vdur <= 0.1 or not text.strip():
        pathlib.Path(outp).write_bytes(pathlib.Path(video_in).read_bytes())
        return
    t0 = max(0.0, vdur - max(0.8, show_sec))
    tf = str(pathlib.Path(outp).with_suffix(".cta.txt"))
    wrapped = (text or "").strip().upper()
    wrapped = re.sub(r"\s+", " ", wrapped)
    open(tf, "w", encoding="utf-8").write(wrapped)
    font_arg = f":fontfile={_ff_sanitize_font(font)}" if font else ""
    y_frac = 0.16 if VIDEO_W > VIDEO_H else 0.18
    tf_ff = _ff_sanitize_path(tf)
    common = f"textfile='{tf_ff}':fontsize=52:x=(w-text_w)/2:y=h*{y_frac}:line_spacing=10"
    box    = f"drawtext={common}{font_arg}:fontcolor=white@0.0:box=1:boxborderw=18:boxcolor=black@0.55:enable='gte(t,{t0:.3f})'"
    main   = f"drawtext={common}{font_arg}:fontcolor=0x3EA6FF:borderw=5:bordercolor=black@0.9:enable='gte(t,{t0:.3f})'"
    vf     = f"{box},{main},fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB"
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", video_in, "-vf", vf,
        "-r", str(TARGET_FPS), "-vsync","cfr",
        "-an","-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),
        "-pix_fmt","yuv420p","-movflags","+faststart", outp
    ])
    pathlib.Path(tf).unlink(missing_ok=True)

# ==================== Audio concat (re-encode safe) ====================
def concat_audios(files: List[str], outp: str):
    if not files: raise RuntimeError("concat_audios: empty file list")
    lst = str(pathlib.Path(outp).with_suffix(".txt"))
    with open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    # Re-encode to PCM to avoid header/trim problems on some runners
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-f","concat","-safe","0","-i", lst,
        "-ar","48000","-ac","1",
        "-c:a","pcm_s16le",
        outp
    ])
    pathlib.Path(lst).unlink(missing_ok=True)

def lock_audio_duration(audio_in: str, target_frames: int, outp: str):
    dur = target_frames / float(TARGET_FPS)
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", audio_in, "-af", f"atrim=end={dur:.6f},asetpts=N/SR/TB",
        "-ar","48000","-ac","1","-c:a","pcm_s16le",
        outp
    ])

def mux(video: str, audio: str, outp: str):
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", video, "-i", audio,
        "-map","0:v:0","-map","1:a:0",
        "-c:v","copy","-c:a","aac","-b:a","256k",
        "-movflags","+faststart","-muxpreload","0","-muxdelay","0",
        "-avoid_negative_ts","make_zero",
        outp
    ])

# ==================== Template selection ====================
def _select_template_key(topic: str) -> str:
    t = (topic or "").lower()
    geo_kw = ("country", "geograph", "city", "capital", "border", "population", "continent", "flag")
    if any(k in t for k in geo_kw): return "country_facts"
    return "_default"

# ==================== Gemini prompts (tuned for longform) ====================
ENHANCED_GEMINI_TEMPLATES = {
    "_default": """Create a 25–40s YouTube Short.
Return STRICT JSON with keys: topic, sentences (7–8), search_terms (4–10), title, description, tags.
- Sentence 1 = a punchy HOOK (≤10 words).
- Sentence 8 = a SOFT CTA for comments.
- 6–12 words per sentence. No filler.""",
    "country_facts": """Create amazing country/city facts.
Return STRICT JSON with keys: topic, sentences (7–8), search_terms (4–10), title, description, tags.
- Sentence 1 = short HOOK (≤10 words).
- Sentence 8 = soft CTA for comments.
- Concrete & visual facts."""
}
LONGFORM_TEMPLATES = {
    "_default": """Create a 3–5 minute YouTube video.
Return STRICT JSON with keys: topic, sentences (8–12), search_terms (6–12), title, description, tags.
Rules:
- 'sentences' MUST be 8–12 SCENES. Each scene is 2–3 sentences totaling ~55–85 words (not shorter).
- Start with a sharp HOOK (≤12 words opening) then 1–2 supportive lines.
- End with a soft CTA for comments (no 'subscribe/like' words).
- Each scene advances one concrete idea with visual specifics.
- Language = same as input; keep it natural.""",
    "country_facts": """Create a 3–5 minute video of specific country/city facts.
Return STRICT JSON with keys: topic, sentences (8–12), search_terms (6–12), title, description, tags.
Rules:
- SCENES: 8–12; each 55–85 words.
- Start with a crisp HOOK. End with a soft CTA for comments.
- Facts must be concrete and visual."""
}
BANNED_PHRASES = ["one clear tip","see it","learn it","plot twist","soap-opera narration","repeat once","takeaway action","in 60 seconds","just the point","crisp beats"]
def _content_score(sentences: List[str]) -> float:
    if not sentences: return 0.0
    bad = 0
    for s in sentences:
        low = (s or "").lower()
        if any(bp in low for bp in BANNED_PHRASES): bad += 1
        if len(low.split()) < 5: bad += 0.5
    return max(0.0, 10.0 - (bad * 1.4))

def _terms_normalize(terms: List[str]) -> List[str]:
    out=[]; seen=set()
    BAD={"great","nice","good","bad","things","stuff","concept","concepts","idea","ideas"}
    for t in terms or []:
        tt = re.sub(r"[^A-Za-z0-9 ]+"," ", str(t)).strip().lower()
        tt = " ".join([w for w in tt.split() if w and len(w)>2 and w not in BAD])[:64]
        if not tt: continue
        if tt not in seen:
            seen.add(tt); out.append(tt)
    return out[:12]

def _derive_terms_from_text(topic: str, sentences: List[str]) -> List[str]:
    pool=set()
    def tok(s):
        s=re.sub(r"[^A-Za-z0-9 ]+"," ", s.lower())
        return [w for w in s.split() if len(w)>3]
    for s in [topic] + sentences:
        ws=tok(s or "")
        for i in range(len(ws)-1): pool.add(ws[i]+" "+ws[i+1])
    base=list(pool); random.shuffle(base)
    return _terms_normalize(base)[:8]

def _gemini_call(prompt: str, model: str, temp: float) -> dict:
    if not GEMINI_API_KEY: raise RuntimeError("GEMINI_API_KEY missing")
    headers = {"Content-Type":"application/json","x-goog-api-key":GEMINI_API_KEY}
    payload = {"contents":[{"parts":[{"text": prompt}]}],
               "generationConfig":{"temperature":temp}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    txt = ""
    try: txt = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception: txt = json.dumps(data)
    m = re.search(r"\{(?:.|\n)*\}", txt)
    if not m: raise RuntimeError("Gemini response parse error (no JSON)")
    raw = re.sub(r"^```json\s*|\s*```$", "", m.group(0).strip(), flags=re.MULTILINE)
    return json.loads(raw)

def _estimate_seconds_from_words(sentences: List[str]) -> float:
    words = sum(len(re.findall(r"\b\w+\b", s)) for s in (sentences or []))
    atempo = _rate_to_atempo(os.getenv("TTS_RATE", "+12%"), default=1.12)
    wps = max(1.6, min(3.5, WPS_BASE_EST * atempo))
    return words / wps if words > 0 else 0.0

def build_via_gemini(channel_name: str, topic_lock: str, user_terms: List[str], banlist: List[str], force_longer: bool=False) -> Tuple[str,List[str],List[str],str,str,List[str]]:
    tpl_key = _select_template_key(topic_lock)
    template = (LONGFORM_TEMPLATES if LONGFORM else ENHANCED_GEMINI_TEMPLATES)[tpl_key]
    if LONGFORM and force_longer:
        # strengthen the length constraints
        template += "\n\nHARD LENGTH RULE: Ensure total reading time ≥ 3 minutes; if short, expand scenes with vivid specifics (55–95 words each) and keep 10–12 scenes."
    avoid = "\n".join(f"- {b}" for b in banlist[:15]) if banlist else "(none)"
    terms_hint = ", ".join(user_terms[:10]) if user_terms else "(none)"
    extra = (("\nADDITIONAL STYLE:\n"+GEMINI_PROMPT) if GEMINI_PROMPT else "")
    guardrails = """
RULES (MANDATORY):
- STAY ON TOPIC exactly as provided.
- Return ONLY JSON, keys: topic, sentences, search_terms, title, description, tags."""
    jitter = ((ROTATION_SEED or int(time.time())) % 13) * 0.01
    temp = max(0.6, min(1.2, GEMINI_TEMP + (jitter - 0.06)))
    prompt = f"""{template}

Channel: {channel_name}
Language: {LANG}
TOPIC (hard lock): {topic_lock}
Seed search terms (use and expand): {terms_hint}
Avoid overlap for 180 days:
{avoid}{extra}
{guardrails}
"""
    data = _gemini_call(prompt, GEMINI_MODEL, temp)
    topic   = topic_lock
    sentences = [normalize_sentence(s) for s in (data.get("sentences") or [])]
    sentences = [s for s in sentences if s][: (12 if LONGFORM else 8)]
    terms = data.get("search_terms") or []
    if isinstance(terms, str): terms=[terms]
    terms = _terms_normalize(terms)
    if not terms: terms = _derive_terms_from_text(topic, sentences)
    if user_terms:
        seed = _terms_normalize(user_terms)
        terms = _terms_normalize(seed + terms)
    title = (data.get("title") or "").strip()
    desc  = (data.get("description") or "").strip()
    tags  = [t.strip() for t in (data.get("tags") or []) if isinstance(t,str) and t.strip()]
    return topic, sentences, terms, title, desc, tags

# ==================== Per-scene queries (unchanged) ====================
_STOP = set("""
a an the and or but if while of to in on at from by with for about into over after before between during under above across around through
this that these those is are was were be been being have has had do does did can could should would may might will shall
you your we our they their he she it its as than then so such very more most many much just also only even still yet
""".split())
_GENERIC_BAD = {"great","good","bad","big","small","old","new","many","more","most","thing","things","stuff"}
def _proper_phrases(texts: List[str]) -> List[str]:
    phrases=[]
    for t in texts:
        for m in re.finditer(r"(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", t or ""):
            phrase = re.sub(r"^(The|A|An)\s+", "", m.group(0))
            ws = [w.lower() for w in phrase.split()]
            for i in range(len(ws)-1): phrases.append(f"{ws[i]} {ws[i+1]}")
    seen=set(); out=[]
    for p in phrases:
        if p not in seen:
            seen.add(p); out.append(p)
    return out
def _domain_synonyms(all_text: str) -> List[str]:
    t = (all_text or "").lower(); s = set()
    if any(k in t for k in ["bridge","tunnel","arch","span"]):
        s.update(["suspension bridge","cable stayed","stone arch","viaduct","aerial city bridge"])
    if any(k in t for k in ["ocean","coast","tide","wave","storm"]):
        s.update(["ocean waves","coastal storm","rocky coast","lighthouse coast"])
    if any(k in t for k in ["timelapse","growth","melt","cloud"]):
        s.update(["city timelapse","plant growth","melting ice","cloud timelapse"])
    if any(k in t for k in ["mechanism","gears","pulley","cam"]):
        s.update(["macro gears","belt pulley","cam follower","robotic arm macro"])
    return list(s)
def build_per_scene_queries(sentences: List[str], fallback_terms: List[str], topic: Optional[str]=None) -> List[str]:
    topic = (topic or "").strip()
    texts_cap = [topic] + sentences
    texts_all = " ".join([topic] + sentences)
    phrase_pool = _proper_phrases(texts_cap) + _domain_synonyms(texts_all)
    def _tok4(s: str) -> List[str]:
        s = re.sub(r"[^A-Za-z0-9 ]+", " ", (s or "").lower())
        toks = [w for w in s.split() if len(w) >= 4 and w not in _STOP and w not in _GENERIC_BAD]
        return toks
    fb=[]
    for t in (fallback_terms or []):
        t = re.sub(r"[^A-Za-z0-9 ]+"," ", str(t)).strip().lower()
        if not t: continue
        ws = [w for w in t.split() if w not in _STOP and w not in _GENERIC_BAD]
        if ws: fb.append(" ".join(ws[:2]))
    topic_keys = _tok4(topic)[:2]
    topic_key_join = " ".join(topic_keys) if topic_keys else ""
    queries=[]; fb_idx = 0
    fb_strong = [t for t in (fallback_terms or []) if t]
    for s in sentences:
        s_low = " " + (s or "").lower() + " "
        picked=None
        for ph in phrase_pool:
            if f" {ph} " in s_low: picked = ph; break
        if not picked:
            toks = _tok4(s)
            if len(toks) >= 2: picked = f"{toks[-2]} {toks[-1]}"
            elif len(toks) == 1: picked = toks[0]
        if (not picked or len(picked) < 4) and (fb_strong or fb):
            seedlist = (fb_strong if fb_strong else fb)
            picked = seedlist[fb_idx % len(seedlist)]; fb_idx += 1
        if (not picked or len(picked) < 4) and topic_key_join:
            picked = topic_key_join
        if not picked or picked in ("great","nice","good","bad","things","stuff"):
            picked = "macro detail"
        if len(picked.split()) > 2:
            w = picked.split(); picked = f"{w[-2]} {w[-1]}"
        queries.append(picked)
    return queries

def _simplify_query(q: str, keep: int = 4) -> str:
    q = (q or "").lower()
    q = re.sub(r"[^a-z0-9 ]+", " ", q)
    toks = [t for t in q.split() if t and t not in _STOP]
    return " ".join(toks[:keep]) if toks else (q.strip()[:40] if q else "")
def _gen_topic_query_candidates(topic: str, terms: List[str]) -> List[str]:
    out: List[str] = []
    base = _simplify_query(topic, keep=4)
    if base: out += [base, _simplify_query(base, keep=2)]
    for t in (terms or []):
        tt = _simplify_query(t, keep=2)
        if tt and tt not in out: out.append(tt)
    if base:
        for w in base.split():
            if w not in out: out.append(w)
    for g in ["city timelapse","ocean waves","forest path","night skyline","macro detail","street crowd","mountain landscape"]:
        if g not in out: out.append(g)
    return out[:20]

# ==================== Pexels (robust) ====================
_USED_PEXELS_IDS_RUNTIME: Set[int] = set()
def _pexels_headers():
    if not PEXELS_API_KEY: raise RuntimeError("PEXELS_API_KEY missing")
    return {"Authorization": PEXELS_API_KEY}
def _is_vertical_ok(w: int, h: int) -> bool:
    if VIDEO_W > VIDEO_H:
        min_w = int(os.getenv("PEXELS_MIN_WIDTH", "1280"))
        return (w >= h) and (w >= min_w)
    else:
        if PEXELS_STRICT_VERTICAL:
            return h > w and h >= PEXELS_MIN_HEIGHT
        return (h >= PEXELS_MIN_HEIGHT) and (h >= w or PEXELS_ALLOW_LANDSCAPE)
def _pexels_search(query: str, locale: str, page: int = 1, per_page: int = None) -> List[Tuple[int, str, int, int, float]]:
    per_page = per_page or max(10, min(80, PEXELS_PER_PAGE))
    url = "https://api.pexels.com/videos/search"
    r = requests.get(
        url, headers=_pexels_headers(),
        params={"query": query, "per_page": per_page, "page": page,
                "orientation": PEXELS_ORIENT,"size":"large","locale": locale},
        timeout=30
    )
    if r.status_code != 200:
        return []
    data = r.json() or {}
    out=[]
    for v in data.get("videos", []):
        vid = int(v.get("id", 0))
        dur = float(v.get("duration",0.0))
        if dur < PEXELS_MIN_DURATION or dur > PEXELS_MAX_DURATION:
            continue
        pf=[]
        for x in (v.get("video_files", []) or []):
            w = int(x.get("width",0)); h = int(x.get("height",0))
            if _is_vertical_ok(w, h): pf.append((w,h,x.get("link")))
        if not pf: continue
        pf.sort(key=lambda t: (abs(t[1]-1600), -(t[0]*t[1])))
        w,h,link = pf[0]
        out.append((vid, link, w, h, dur))
    return out
def _pexels_popular(locale: str, page: int = 1, per_page: int = 40) -> List[Tuple[int, str, int, int, float]]:
    url = "https://api.pexels.com/videos/popular"
    r = requests.get(url, headers=_pexels_headers(), params={"per_page": per_page, "page": page}, timeout=30)
    if r.status_code != 200: return []
    data = r.json() or {}
    out=[]
    for v in data.get("videos", []):
        vid = int(v.get("id", 0))
        dur = float(v.get("duration",0.0))
        if dur < PEXELS_MIN_DURATION or dur > PEXELS_MAX_DURATION: continue
        pf=[]
        for x in (v.get("video_files", []) or []):
            w = int(x.get("width",0)); h = int(x.get("height",0))
            if _is_vertical_ok(w, h): pf.append((w,h,x.get("link")))
        if not pf: continue
        pf.sort(key=lambda t: (abs(t[1]-1600), -(t[0]*t[1])))
        w,h,link = pf[0]
        out.append((vid, link, w, h, dur))
    return out
def _pixabay_fallback(q: str, need: int, locale: str) -> List[Tuple[int, str]]:
    if not (ALLOW_PIXABAY_FALLBACK and PIXABAY_API_KEY): return []
    try:
        params = {"key": PIXABAY_API_KEY, "q": q, "safesearch":"true",
                  "per_page": min(50, max(10, need*4)), "video_type":"film", "order":"popular"}
        r = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
        if r.status_code != 200: return []
        data = r.json() or {}
        outs=[]
        for h in data.get("hits", []):
            dur = float(h.get("duration",0.0))
            if dur < PEXELS_MIN_DURATION or dur > PEXELS_MAX_DURATION: continue
            vids = h.get("videos", {})
            chosen=None
            for qual in ("large","medium","small","tiny"):
                v = vids.get(qual)
                if not v: continue
                w,hh = int(v.get("width",0)), int(v.get("height",0))
                if _is_vertical_ok(w, hh):
                    chosen = (w,hh,v.get("url")); break
            if chosen: outs.append( (int(h.get("id",0)), chosen[2]) )
        return outs[:need]
    except Exception:
        return []
def _rank_and_dedup(items: List[Tuple[int, str, int, int, float]], qtokens: Set[str], block: Set[int]) -> List[Tuple[int,str]]:
    cand=[]
    for vid, link, w, h, dur in items:
        if vid in block or vid in _USED_PEXELS_IDS_RUNTIME: continue
        tokens = set(re.findall(r"[a-z0-9]+", (link or "").lower()))
        overlap = len(tokens & qtokens)
        score = overlap*2.0 + (1.0 if 2.0 <= dur <= 12.0 else 0.0) + (1.0 if h >= 1440 else 0.0)
        cand.append((score, vid, link))
    cand.sort(key=lambda x: x[0], reverse=True)
    out=[]; seen=set()
    for _, vid, link in cand:
        if vid in seen: continue
        seen.add(vid); out.append((vid, link))
    return out

def build_pexels_pool(topic: str, sentences: List[str], search_terms: List[str], need: int, rotation_seed: int = 0) -> List[Tuple[int,str]]:
    random.seed(rotation_seed or int(time.time()))
    locale = "tr-TR" if LANG.startswith("tr") else "en-US"
    block = _blocklist_get_pexels()
    per_scene = build_per_scene_queries(sentences, search_terms, topic=topic)
    topic_cands = _gen_topic_query_candidates(topic, search_terms)
    queries = []
    seen_q=set()
    for q in per_scene + topic_cands:
        q = (q or "").strip()
        if q and q not in seen_q:
            seen_q.add(q); queries.append(q)
    pool: List[Tuple[int,str]] = []
    qtokens_cache: Dict[str, Set[str]] = {}
    for q in queries:
        qtokens_cache[q] = set(re.findall(r"[a-z0-9]+", q.lower()))
        merged: List[Tuple[int, str, int, int, float]] = []
        for page in (1, 2, 3):
            merged += _pexels_search(q, locale, page=page, per_page=PEXELS_PER_PAGE)
            if len(merged) >= need*3: break
        ranked = _rank_and_dedup(merged, qtokens_cache[q], block)
        pool += ranked[:max(3, need//2)]
        if len(pool) >= need*2: break
    if len(pool) < need:
        merged=[]
        for page in (1,2,3):
            merged += _pexels_popular(locale, page=page, per_page=40)
            if len(merged) >= need*3: break
        pop_rank = _rank_and_dedup(merged, set(), block)
        pool += pop_rank[:need*2 - len(pool)]
    if len(pool) < need:
        fallback_q = (queries[-1] if queries else _simplify_query(topic, keep=1)) or "city"
        pix = _pixabay_fallback(fallback_q, need - len(pool), locale)
        pool += pix
    seen=set(); dedup=[]
    for vid, link in pool:
        if vid in seen: continue
        seen.add(vid); dedup.append((vid, link))
    fresh = [(vid,link) for vid,link in dedup if vid not in block]
    rest  = [(vid,link) for vid,link in dedup if vid in block]
    final = (fresh + rest)[:max(need, len(sentences))]
    print(f"   Pexels candidates: q={len(queries)} | pool={len(final)} (fresh={len(fresh)})")
    return final

# ==================== YouTube (unchanged) ====================
def yt_service():
    cid  = os.getenv("YT_CLIENT_ID")
    csec = os.getenv("YT_CLIENT_SECRET")
    rtok = os.getenv("YT_REFRESH_TOKEN")
    if not (cid and csec and rtok): raise RuntimeError("Missing YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN")
    creds = Credentials(
        token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token",
        client_id=cid, client_secret=csec, scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)
def upload_youtube(video_path: str, meta: dict) -> str:
    y = yt_service()
    body = {"snippet": {
                "title": meta["title"], "description": meta["description"], "tags": meta.get("tags", []),
                "categoryId": "27",
                "defaultLanguage": meta.get("defaultLanguage", LANG),
                "defaultAudioLanguage": meta.get("defaultAudioLanguage", LANG)
            },
            "status": {"privacyStatus": meta.get("privacy", VISIBILITY), "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    try:
        req = y.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = req.execute()
        return resp.get("id", "")
    except HttpError as e:
        raise RuntimeError(f"YouTube upload error: {e}")

# ==================== Long SEO Description (unchanged-ish) ====================
def build_long_description(channel: str, topic: str, sentences: List[str], tags: List[str]) -> Tuple[str, str, List[str]]:
    hook = (sentences[0].rstrip(" .!?") if sentences else topic or channel)
    title = (hook[:1].upper() + hook[1:])[:95]
    para = " ".join(sentences)
    explainer = (f"{para} "
        f"This video explores “{topic}” with clear, visual steps. "
        f"Rewatch to catch tiny details, save for later, and share with someone who’ll enjoy it.")
    tagset = []
    base_terms = [w for w in re.findall(r"[A-Za-z]{3,}", (topic or ""))][:5]
    for t in base_terms: tagset.append("#" + t.lower())
    tagset += ["#learn", "#visual", "#broll", "#education"]
    if tags:
        for t in tags[:10]:
            tclean = re.sub(r"[^A-Za-z0-9]+","", t).lower()
            if tclean and ("#"+tclean) not in tagset: tagset.append("#"+tclean)
    body = (f"{explainer}\n\n— Key scenes —\n"
            + "\n".join([f"• {s}" for s in sentences[:12]]) +
            "\n\n— Why it works —\nOne concrete idea per visual beat helps retention.\n\n"
            + " ".join(tagset))
    if len(body) > 4900: body = body[:4900]
    yt_tags = []
    for h in tagset:
        k = h[1:]
        if k and k not in yt_tags: yt_tags.append(k)
        if len(yt_tags) >= 15: break
    return title, body, yt_tags

# ==================== HOOK/CTA polish ====================
HOOK_MAX_WORDS = _env_int("HOOK_MAX_WORDS", 10)
def _polish_hook_cta(sentences: List[str]) -> List[str]:
    if not sentences: return sentences
    ss = sentences[:]
    hook = normalize_sentence(ss[0])
    words = hook.split()
    if len(words) > HOOK_MAX_WORDS:
        hook = " ".join(words[:HOOK_MAX_WORDS])
    if not re.search(r"[?!]$", hook):
        if hook.split()[0:1] and hook.split()[0].lower() not in {"why","how","did","are","is","can"}:
            hook = hook.rstrip(".") + "?"
    ss[0] = hook
    if ss and not re.search(r'[.!?]$', ss[-1].strip()):
        ss[-1] = ss[-1].strip() + '.'
    return ss

# ==================== BGM helpers (unchanged) ====================
def _pick_bgm_source(tmpdir: str) -> Optional[str]:
    try:
        p = pathlib.Path(BGM_DIR)
        if p.exists():
            files = [str(x) for x in p.glob("*.mp3")] + [str(x) for x in p.glob("*.wav")]
            if files:
                random.shuffle(files)
                return files[0]
    except Exception:
        pass
    urls = list(BGM_URLS or [])
    random.shuffle(urls)
    for u in urls:
        try:
            ext = ".mp3" if ".mp3" in u.lower() else ".wav"
            outp = str(pathlib.Path(tmpdir) / f"bgm_src{ext}")
            with requests.get(u, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(outp, "wb") as f:
                    for ch in r.iter_content(8192): f.write(ch)
            if pathlib.Path(outp).stat().st_size > 100_000:
                return outp
        except Exception:
            continue
    return None
def _make_bgm_looped(src: str, dur: float, out_wav: str):
    fade = max(0.3, float(BGM_FADE))
    endst = max(0.0, dur - fade)
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-stream_loop","-1","-i", src, "-t", f"{dur:.3f}",
        "-af", f"loudnorm=I=-21:TP=-2.0:LRA=11,"
               f"afade=t=in:st=0:d={fade:.2f},afade=t=out:st={endst:.2f}:d={fade:.2f},"
               "aresample=48000,pan=mono|c0=0.5*FL+0.5*FR",
        "-ar","48000","-ac","1","-c:a","pcm_s16le", out_wav
    ])
def _duck_and_mix(voice_in: str, bgm_in: str, outp: str):
    bgm_gain_db   = float(os.getenv("BGM_GAIN_DB", "-10"))
    thr           = float(os.getenv("BGM_DUCK_THRESH", "0.03"))
    ratio         = float(os.getenv("BGM_DUCK_RATIO",  "10"))
    attack_ms     = int(os.getenv("BGM_DUCK_ATTACK_MS","6"))
    release_ms    = int(os.getenv("BGM_DUCK_RELEASE_MS","180"))
    sc = (f"sidechaincompress=threshold={thr}:ratio={ratio}:attack={attack_ms}:release={release_ms}:"
          f"makeup=1.0:level_in=1.0:level_sc=1.0")
    if _HAS_SIDECHAIN:
        filter_complex = (f"[1:a]volume={bgm_gain_db}dB[b];"
                          f"[b][0:a]{sc}[duck];"
                          f"[0:a][duck]amix=inputs=2:duration=shortest,aresample=48000,alimiter=limit=0.98[mix]")
    else:
        filter_complex = (f"[1:a]volume={bgm_gain_db}dB[b];"
                          f"[0:a][b]amix=inputs=2:duration=shortest,aresample=48000,alimiter=limit=0.98[mix]")
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", voice_in, "-i", bgm_in,
        "-filter_complex", filter_complex, "-map","[mix]",
        "-ar","48000","-ac","1","-c:a","pcm_s16le", outp
    ])

# ==================== Debug meta ====================
def _dump_debug_meta(path: str, obj: dict):
    try: pathlib.Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception: pass

# ==================== MAIN (longform-safe pipeline) ====================
def main():
    print(f"==> {CHANNEL_NAME} | MODE={MODE} | longform-safe build")
    if CAPTIONS_ENABLE:
        print("ℹ️ CAPTIONS_ENABLE=1, fakat uzun videoda önerilmez. Devre dışı bırakmak için CAPTIONS_ENABLE=0 kullanın.")
    random.seed(ROTATION_SEED or int(time.time()))
    topic_lock = TOPIC or "Interesting Visual Explainers"
    user_terms = SEARCH_TERMS_ENV

    # 1) İçerik üretimi + kalite + NOVELTY + LENGTH GUARD
    attempts = 0; best=None; best_score=-1.0; banlist = _recent_topics_for_prompt()
    forced = False
    while attempts < max(5, NOVELTY_RETRIES+2):
        attempts += 1
        try:
            tpc, sents, search_terms, ttl, desc, tags = build_via_gemini(
                CHANNEL_NAME, topic_lock, user_terms, banlist, force_longer=(LONGFORM and forced)
            )
        except Exception as e:
            print(f"Gemini error: {str(e)[:200]}")
            tpc = topic_lock; sents=[]; search_terms=user_terms or []; ttl=""; desc=""; tags=[]
        sents = _polish_hook_cta(sents)

        # Length guard pre-check (estimate)
        if LONGFORM and (len(sents) < MIN_SCENES_LONGFORM or _estimate_seconds_from_words(sents) < TARGET_MIN_SEC*0.9):
            print("⚠️ Estimated too short or too few scenes → retry with stronger length rules.")
            forced = True
            banlist = [tpc] + banlist
            continue

        ok, avoid_terms = _novelty_ok(sents)
        if not ok and attempts < (NOVELTY_RETRIES+2):
            print(f"⚠️ Similar to recent videos → rebuilding with bans: {avoid_terms[:8]}")
            banlist = [*avoid_terms, *banlist]
            continue

        score = _content_score(sents)
        print(f"📝 Content: {tpc} | scenes={len(sents)} | est={_estimate_seconds_from_words(sents):.1f}s | score={score:.2f}")
        if score > best_score:
            best = (tpc, sents, search_terms, ttl, desc, tags); best_score = score
        if score >= 7.2 and ok: break
        else:
            banlist = [tpc] + banlist
            time.sleep(0.4)

    tpc, sentences, search_terms, ttl, desc, tags = best if best else (topic_lock, [], user_terms or [], "", "", [])
    sig = f"{CHANNEL_NAME}|{tpc}|{sentences[0] if sentences else ''}"
    fp = sorted(list(_sentences_fp(sentences)))[:500]
    _record_recent(_hash12(sig), MODE, tpc, fp=fp)
    try:
        __ent = _derive_focus_entity(tpc, MODE, sentences); __ek = _entity_key(MODE, __ent); _entity_touch(__ek)
    except Exception: pass

    _dump_debug_meta(f"{OUT_DIR}/meta_{re.sub(r'[^A-Za-z0-9]+','_',CHANNEL_NAME)}.json", {
        "channel": CHANNEL_NAME, "topic": tpc, "sentences": sentences, "search_terms": search_terms,
        "lang": LANG, "model": GEMINI_MODEL, "ts": time.time()
    })
    print(f"📊 Scenes: {len(sentences)}")

    # 2) TTS (kelime zamanları alınır; ama captions kullanılmıyor)
    tmp = tempfile.mkdtemp(prefix="longform_safe_")
    wavs, metas = [], []
    print("🎤 TTS…")
    for i, s in enumerate(sentences):
        base = normalize_sentence(s)
        w = str(pathlib.Path(tmp) / f"sent_{i:02d}.wav")
        d, words = tts_to_wav(base, w)
        wavs.append(w); metas.append((base, d, words))
        print(f"   {i+1}/{len(sentences)}: {d:.2f}s")

    total_audio = sum(d for _,d,_ in metas)
    # 2.1) If still short, add natural breathing silences to reach TARGET_MIN_SEC
    if LONGFORM and total_audio < TARGET_MIN_SEC:
        deficit = TARGET_MIN_SEC - total_audio
        print(f"⏱️ Audio short by {deficit:.1f}s → inserting short pauses between sentences.")
        pauses = []
        n_slots = max(1, len(wavs)-1)
        add_each = min(1.6, max(0.6, deficit / max(1,n_slots)))
        for i in range(n_slots):
            pth = str(pathlib.Path(tmp) / f"pause_{i:02d}.wav")
            run(["ffmpeg","-y","-f","lavfi","-t", f"{add_each:.3f}",
                 "-i","anullsrc=r=48000:cl=mono","-ar","48000","-ac","1","-c:a","pcm_s16le", pth])
            pauses.append(pth)
        # interleave
        interleaved=[]
        for i, w in enumerate(wavs):
            interleaved.append(w)
            if i < len(pauses): interleaved.append(pauses[i])
        wavs = interleaved
        total_audio = sum(ffprobe_dur(x) for x in wavs)
        print(f"   New est audio: {total_audio:.1f}s")

    # 3) Per-scene queries & Pexels pool (we'll build for visual montage length)
    per_scene_queries = build_per_scene_queries([m[0] for m in metas], (search_terms or user_terms or []), topic=tpc)
    print("🔎 Per-scene queries:")
    for q in per_scene_queries[:min(12,len(per_scene_queries))]: print(f"   • {q}")
    # We decide need based on visual slots (rough), but pool can be smaller due to reuse rules.
    # We'll compute slots after audio concat; for now fetch a healthy pool:
    approx_slots = max(36, int(math.ceil(max(TARGET_MIN_SEC, total_audio) / VIS_SLOT_SEC)))
    need_clips = min(50, max(12, approx_slots // 2))
    pool: List[Tuple[int,str]] = build_pexels_pool(
        topic=tpc, sentences=[m[0] for m in metas],
        search_terms=(search_terms or user_terms or []),
        need=need_clips, rotation_seed=ROTATION_SEED
    )
    if not pool: raise RuntimeError("Pexels: no suitable clips (after all fallbacks).")

    # 4) Download pool
    print("⬇️ Download pool…")
    downloads: Dict[int,str] = {}
    for idx, (vid, link) in enumerate(pool):
        try:
            f = str(pathlib.Path(tmp) / f"pool_{idx:02d}_{vid}.mp4")
            with requests.get(link, stream=True, timeout=120) as rr:
                rr.raise_for_status()
                with open(f, "wb") as wfd:
                    for ch in rr.iter_content(8192): wfd.write(ch)
            if pathlib.Path(f).stat().st_size > 300_000:
                downloads[vid] = f
        except Exception as e:
            print(f"⚠️ download fail ({vid}): {e}")
    if not downloads: raise RuntimeError("Pexels pool empty after downloads.")
    print(f"   Downloaded unique clips: {len(downloads)}")

    # 5) Build AUDIO master (re-encode safe concat)
    print("🔊 Concat audio…")
    acat = str(pathlib.Path(tmp) / "audio_concat.wav")
    concat_audios(wavs, acat)
    adur = ffprobe_dur(acat)
    print(f"   Audio total: {adur:.2f}s")

    # 6) VISUAL MONTAGE (decoupled 5s slots)
    print("🎬 Visual montage (every %.1fs)…" % VIS_SLOT_SEC)
    total_frames = max(2, int(round(adur * TARGET_FPS)))
    slot_frames  = max(2, int(round(VIS_SLOT_SEC * TARGET_FPS)))
    num_slots    = max(1, int(math.ceil(total_frames / slot_frames)))
    # schedule with reuse<=3 and no back-to-back
    ids = list(downloads.keys())
    usage = {vid:0 for vid in ids}
    seq: List[int] = []
    prev = None
    for i in range(num_slots):
        # pick candidate with min usage, not equal to prev (if required)
        cands = sorted(ids, key=lambda k: (usage[k], random.random()))
        chosen = None
        for vid in cands:
            if VIS_NO_BACK_TO_BACK and prev is not None and vid == prev:
                continue
            if usage[vid] < VIS_MAX_REUSE_PER_CLIP:
                chosen = vid; break
        if chosen is None:
            # relax reuse if needed
            chosen = min(ids, key=lambda k: usage[k])
            if VIS_NO_BACK_TO_BACK and prev is not None and chosen == prev and len(ids) > 1:
                # force different by swapping with next best
                alt = sorted(ids, key=lambda k: (usage[k]+(0 if k!=prev else 999), random.random()))[0]
                chosen = alt
        seq.append(chosen); usage[chosen] += 1; prev = chosen

    segs=[]
    for i, vid in enumerate(seq):
        src = downloads[vid]
        seg_dur = (slot_frames / float(TARGET_FPS)) if (i < num_slots-1) else (total_frames/float(TARGET_FPS) - (slot_frames*(num_slots-1)/float(TARGET_FPS)))
        seg_dur = max(0.2, seg_dur)
        outp = str(pathlib.Path(tmp) / f"slot_{i:03d}.mp4")
        make_segment(src, seg_dur, outp)
        segs.append(outp)

    # 7) Concatenate visuals and lock to audio frames
    print("🎞️ Assemble video…")
    def concat_videos_filter(files: List[str], outp: str):
        if not files: raise RuntimeError("concat_videos_filter: empty")
        inputs = []; filters = []
        for i, p in enumerate(files):
            inputs += ["-i", p]
            filters.append(f"[{i}:v]fps={TARGET_FPS},settb=AVTB,setpts=N/{TARGET_FPS}/TB[v{i}]")
        filtergraph = ";".join(filters) + ";" + "".join(f"[v{i}]" for i in range(len(files))) + f"concat=n={len(files)}:v=1:a=0[v]"
        run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            *inputs, "-filter_complex", filtergraph, "-map","[v]",
            "-r", str(TARGET_FPS), "-vsync","cfr",
            "-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),
            "-pix_fmt","yuv420p","-movflags","+faststart", outp
        ])
    vcat = str(pathlib.Path(tmp) / "video_concat.mp4")
    concat_videos_filter(segs, vcat)

    # lock exact frames to audio
    a_frames = max(2, int(round(adur * TARGET_FPS)))
    vcat_exact = str(pathlib.Path(tmp) / "video_exact.mp4")
    enforce_video_exact_frames(vcat, a_frames, vcat_exact); vcat = vcat_exact
    # CTA tail
    try:
        if CTA_ENABLE:
            cta_text = build_contextual_cta(tpc, [m[0] for m in metas], LANG)
            if cta_text:
                print(f"💬 CTA: {cta_text}")
                vcat_cta = str(pathlib.Path(tmp) / "video_cta.mp4")
                overlay_cta_tail(vcat, cta_text, vcat_cta, CTA_SHOW_SEC, font_path())
                vcat_exact2 = str(pathlib.Path(tmp) / "video_exact_cta.mp4")
                enforce_video_exact_frames(vcat_cta, a_frames, vcat_exact2)
                vcat = vcat_exact2
    except Exception as e:
        print(f"⚠️ CTA overlay skipped: {e}")

    # 8) Mux
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_topic = re.sub(r'[^A-Za-z0-9]+', '_', tpc)[:60] or "Longform"
    outp = f"{OUT_DIR}/{CHANNEL_NAME}_{safe_topic}_{ts}.mp4"
    print("🔄 Mux…"); mux(vcat, acat, outp)
    final = ffprobe_dur(outp); print(f"✅ Saved: {outp} ({final:.2f}s)")

    # 9) Metadata & upload
    title, description, yt_tags = build_long_description(CHANNEL_NAME, tpc, [m[0] for m in metas], tags)
    meta = {"title": title,"description": description,"tags": yt_tags,"privacy": VISIBILITY,
            "defaultLanguage": LANG,"defaultAudioLanguage": LANG}
    try:
        if os.getenv("UPLOAD_TO_YT","1") == "1":
            print("📤 Uploading to YouTube…")
            vid_id = upload_youtube(outp, meta)
            print(f"🎉 YouTube Video ID: {vid_id}\n🔗 https://youtube.com/watch?v={vid_id}")
        else:
            print("⏭️ Upload disabled (UPLOAD_TO_YT != 1)")
    except Exception as e:
        print(f"❌ Upload skipped: {e}")

    # 10) Blocklist Pexels IDs
    try:
        _blocklist_add_pexels(list(set(seq)), days=30)
    except Exception as e:
        print(f"⚠️ Blocklist save warn: {e}")

    # 11) Cleanup
    try: shutil.rmtree(tmp); print("🧹 Cleaned temp files")
    except: pass

if __name__ == "__main__":
    main()
