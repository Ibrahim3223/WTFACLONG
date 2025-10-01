# autoshorts_daily.py — Robust long/short builder
# - Topic-locked Gemini (optional)
# - Per-scene stock search (Pexels + optional Pixabay fallback)
# - Durable TTS chain with min-duration guard
# - Karaoke captions (ASS) with word timings or drawtext fallback
# - Aspect control (16:9 / 9:16), longform scenes, CTA overlay, optional BGM
# -*- coding: utf-8 -*-

import os, sys, re, json, time, random, datetime, tempfile, pathlib, subprocess, hashlib, math, shutil
from typing import List, Optional, Tuple, Dict, Any, Set

# ======== Quick helpers ========
def run(cmd, check=True):
    res = subprocess.run(cmd, text=True, capture_output=True)
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr[:4000])
    return res

def ffprobe_dur(p: str) -> float:
    try:
        out = run(["ffprobe","-v","quiet","-show_entries","format=duration","-of","csv=p=0", p]).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0

def _env_int(name: str, default: int) -> int:
    s = os.getenv(name)
    if s is None: return default
    try: return int(float(str(s).strip()))
    except Exception: return default

def _env_float(name: str, default: float) -> float:
    s = os.getenv(name)
    if s is None: return default
    try: return float(str(s).strip())
    except Exception: return default

def _sanitize_lang(val: Optional[str]) -> str:
    val = (val or "").strip()
    if not val: return "en"
    m = re.match(r"([A-Za-z]{2})", val)
    return (m.group(1).lower() if m else "en")

def _sanitize_privacy(val: Optional[str]) -> str:
    v = (val or "").strip().lower()
    return v if v in {"public","unlisted","private"} else "public"

def font_path():
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "C:/Windows/Fonts/arial.ttf"]:
        if pathlib.Path(p).exists(): return p
    return ""

def ffmpeg_has_filter(name: str) -> bool:
    try:
        out = run(["ffmpeg","-hide_banner","-filters"], check=False).stdout
        return bool(re.search(rf"\b{name}\b", out))
    except Exception:
        return False

_HAS_DRAWTEXT  = ffmpeg_has_filter("drawtext")
_HAS_SUBTITLES = ffmpeg_has_filter("subtitles")

# ======== ENV / constants ========
ASPECT_RAW = (os.getenv("ASPECT", "9:16") or "9:16").strip().lower()
LONGFORM   = (os.getenv("LONGFORM", "0") == "1")

if ASPECT_RAW in {"16:9","landscape","widescreen"}:
    VIDEO_W, VIDEO_H = 1920, 1080
    PEXELS_ORIENT = "landscape"
else:
    VIDEO_W, VIDEO_H = 1080, 1920
    PEXELS_ORIENT = "portrait"

CHANNEL_NAME  = os.getenv("CHANNEL_NAME", "DefaultChannel").strip()
MODE          = os.getenv("MODE", "freeform").strip().lower()
LANG          = _sanitize_lang(os.getenv("VIDEO_LANG") or os.getenv("LANG") or "en")
VISIBILITY    = _sanitize_privacy(os.getenv("VISIBILITY"))
ROTATION_SEED = _env_int("ROTATION_SEED", 0)

TARGET_FPS   = _env_int("TARGET_FPS", 30 if VIDEO_W > VIDEO_H else 25)
CRF_VISUAL   = _env_int("CRF_VISUAL", 22)
OUT_DIR      = "out"; pathlib.Path(OUT_DIR).mkdir(exist_ok=True)

# Content size
TARGET_MIN_SEC = _env_float("TARGET_MIN_SEC", 180.0 if LONGFORM else 22.0)
TARGET_MAX_SEC = _env_float("TARGET_MAX_SEC", 300.0 if LONGFORM else 42.0)
SCENE_COUNT    = _env_int("SCENE_COUNT", 9 if LONGFORM else 8)

# Captions
REQUIRE_CAPTIONS = os.getenv("REQUIRE_CAPTIONS","0") == "1"
KARAOKE_CAPTIONS = os.getenv("KARAOKE_CAPTIONS","1") == "1"
CAPTION_MAX_LINE  = _env_int("CAPTION_MAX_LINE",  36 if VIDEO_W>VIDEO_H else 28)
CAPTION_MAX_LINES = _env_int("CAPTION_MAX_LINES",  4 if VIDEO_W>VIDEO_H else  6)
KARAOKE_ACTIVE   = os.getenv("KARAOKE_ACTIVE",   "#3EA6FF")
KARAOKE_INACTIVE = os.getenv("KARAOKE_INACTIVE", "#FFD700")
KARAOKE_OUTLINE  = os.getenv("KARAOKE_OUTLINE",  "#000000")
CAPTION_LEAD_MS  = _env_int("CAPTION_LEAD_MS", 80)

# CTA
CTA_ENABLE    = os.getenv("CTA_ENABLE","1") == "1"
CTA_SHOW_SEC  = _env_float("CTA_SHOW_SEC", 2.8)
CTA_MAX_CHARS = _env_int("CTA_MAX_CHARS", 64)
CTA_TEXT_FORCE= (os.getenv("CTA_TEXT") or "").strip()

# TTS (durable)
VOICE_OPTIONS = {
    "en": ["en-US-JennyNeural","en-US-JasonNeural","en-US-AriaNeural","en-US-GuyNeural","en-GB-SoniaNeural","en-AU-NatashaNeural","en-CA-LiamNeural"],
    "tr": ["tr-TR-EmelNeural","tr-TR-AhmetNeural"]
}
VOICE        = os.getenv("TTS_VOICE", VOICE_OPTIONS.get(LANG, ["en-US-JennyNeural"])[0])
TTS_RATE     = os.getenv("TTS_RATE", "+12%" if not LONGFORM else "+6%")
TTS_MIN_SEC  = _env_float("TTS_MIN_SEC", 2.8 if LONGFORM else 1.4)  # per sentence minimum
TTS_RETRIES  = _env_int("TTS_RETRIES", 3)

# APIs
PEXELS_API_KEY = (os.getenv("PEXELS_API_KEY") or "").strip()
PIXABAY_API_KEY= (os.getenv("PIXABAY_API_KEY") or "").strip()
ALLOW_PIXABAY  = os.getenv("ALLOW_PIXABAY_FALLBACK","1") == "1"

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
USE_GEMINI     = os.getenv("USE_GEMINI","1") == "1"
GEMINI_MODEL   = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
GEMINI_TEMP    = _env_float("GEMINI_TEMP", 0.9)
TOPIC          = re.sub(r'^[\'"]|[\'"]$', '', (os.getenv("TOPIC") or "").strip())

SEARCH_TERMS_ENV = []
def _parse_terms(s: str) -> List[str]:
    s=(s or "").strip()
    if not s: return []
    try:
        j=json.loads(s)
        if isinstance(j,list): return [str(x).strip() for x in j if str(x).strip()]
    except Exception: pass
    s=re.sub(r'^[\[\(]|\s*[\]\)]$','',s)
    return [t.strip() for t in re.split(r'\s*,\s*',s) if t.strip()]
SEARCH_TERMS_ENV = _parse_terms(os.getenv("SEARCH_TERMS",""))

# ======== deps (auto-install) ========
def _pip(p): subprocess.run([sys.executable,"-m","pip","install","-q",p], check=True)
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

# ======== Text helpers ========
def clean_caption_text(s: str) -> str:
    t=(s or "").strip()
    t=t.replace("—","-").replace("–","-").replace("“",'"').replace("”",'"').replace("’","'")
    t=re.sub(r"\s+"," ",t).strip()
    if t and t[0].islower(): t=t[0].upper()+t[1:]
    return t

def wrap_lines(text: str, max_line: int, max_lines: int) -> str:
    text=(text or "").strip()
    if not text: return text
    words=text.split()
    def greedy(width,kcap):
        lines=[];buf=[];L=0
        for w in words:
            add=(1 if buf else 0)+len(w)
            if L+add>width and buf:
                lines.append(" ".join(buf)); buf=[w]; L=len(w)
            else:
                buf.append(w); L+=add
        if buf: lines.append(" ".join(buf))
        return lines[:kcap]
    lines=greedy(max_line,max_lines)
    return "\n".join([ln.strip() for ln in lines if ln.strip()])

CAPTION_COLORS = ["0xFFD700","0xFF6B35","0x00F5FF","0x32CD32","0xFF1493","0x1E90FF","0xFFA500","0xFF69B4"]
def _ff_color(c: str) -> str:
    c=(c or "").strip()
    if c.startswith("#"): return "0x"+c[1:].upper()
    if re.fullmatch(r"0x[0-9A-Fa-f]{6}",c): return c
    return "white"

# ======== TTS (durable) ========
def _rate_to_atempo(rate_str: str, default: float = 1.10) -> float:
    try:
        s=(rate_str or "").strip()
        if s.endswith("%"): return max(0.5,min(2.0,1.0+float(s[:-1])/100.0))
        if s.endswith(("x","X")): return max(0.5,min(2.0,float(s[:-1])))
        return max(0.5,min(2.0,float(s)))
    except Exception:
        return default

def _edge_stream_with_marks(text: str, voice: str, rate_env: str, mp3_out: str) -> List[Dict[str,Any]]:
    import asyncio
    marks=[]
    async def _run():
        audio=bytearray()
        comm=edge_tts.Communicate(text, voice=voice, rate=rate_env)
        async for chunk in comm.stream():
            t=chunk.get("type")
            if t=="audio":
                audio.extend(chunk.get("data",b""))
            elif t=="WordBoundary":
                off=float(chunk.get("offset",0))/10_000_000.0
                dur=float(chunk.get("duration",0))/10_000_000.0
                marks.append({"t0":off,"t1":off+dur,"text":str(chunk.get("text",""))})
        open(mp3_out,"wb").write(bytes(audio))
    try:
        asyncio.run(_run())
    except RuntimeError:
        nest_asyncio.apply(); loop=asyncio.get_event_loop(); loop.run_until_complete(_run())
    return marks

def _mp3_to_wav(mp3: str, wav_out: str, atempo: float):
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", mp3,
        "-ar","48000","-ac","1","-acodec","pcm_s16le",
        "-af", f"dynaudnorm=g=7:f=250,atempo={atempo}",
        wav_out
    ])

def _ensure_min_audio(wav_in: str, min_sec: float, words_count: int) -> Tuple[str,float]:
    dur=ffprobe_dur(wav_in)
    target=max(min_sec, 0.12*max(1,words_count))
    if dur+0.01>=target: return wav_in,dur
    tmp=str(pathlib.Path(wav_in).with_suffix(".pad.wav"))
    pad=max(0.0, target-dur)
    run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", wav_in,
        "-af", f"apad=pad_dur={pad:.3f},atrim=end={target:.3f},aresample=48000",
        "-ar","48000","-ac","1","-c:a","pcm_s16le", tmp
    ])
    return tmp, target

def _merge_marks_to_words(text: str, marks: List[Dict[str,Any]], total: float) -> List[Tuple[str,float]]:
    words=[w for w in re.split(r"\s+",(text or "").strip()) if w]
    if not words: return []
    if marks:
        ms=[m for m in marks if (m.get("t1",0)>m.get("t0",0))]
        if len(ms)>=len(words)*0.6:
            N=min(len(words),len(ms))
            raw=[max(0.02,float(ms[i]["t1"]-ms[i]["t0"])) for i in range(N)]
            scale=(total/(sum(raw) or 1.0))
            out=[(words[i], max(0.05, raw[i]*scale)) for i in range(N)]
            if N<len(words):
                remain=max(0.0, total-sum(d for _,d in out))
                each=(remain/max(1,len(words)-N)) if remain>0 else 0.05
                out += [(words[i], max(0.05, each)) for i in range(N,len(words))]
            return out
    each=max(0.06, total/max(1,len(words)))
    out=[(w,each) for w in words]
    s=sum(d for _,d in out)
    if s>0 and abs(s-total)>0.02:
        out[-1]=(out[-1][0], max(0.05, out[-1][1] + (total-s)))
    return out

def tts_to_wav(text: str, wav_out: str) -> Tuple[float, List[Tuple[str,float]]]:
    """Durable TTS with min-duration pad. Returns (duration, [(word,dur),...])."""
    import asyncio
    text=(text or "").strip()
    if not text:
        run(["ffmpeg","-y","-f","lavfi","-t","1.0","-i","anullsrc=r=48000:cl=mono", wav_out])
        return 1.0, []
    atempo=_rate_to_atempo(TTS_RATE, default=1.08 if LONGFORM else 1.12)
    selected_voice = VOICE if VOICE in VOICE_OPTIONS.get(LANG, []) else VOICE_OPTIONS.get(LANG, ["en-US-JennyNeural"])[0]
    mp3=wav_out.replace(".wav",".mp3")
    last_err=None
    for attempt in range(1, TTS_RETRIES+1):
        try:
            marks=_edge_stream_with_marks(text, selected_voice, TTS_RATE, mp3)
            _mp3_to_wav(mp3, wav_out, atempo)
            wav_fixed, dur=_ensure_min_audio(wav_out, TTS_MIN_SEC, len(text.split()))
            if wav_fixed!=wav_out: shutil.move(wav_fixed, wav_out)
            words=_merge_marks_to_words(text, marks, ffprobe_dur(wav_out))
            pathlib.Path(mp3).unlink(missing_ok=True)
            time.sleep(0.25 if LONGFORM else 0.12)
            return ffprobe_dur(wav_out), words
        except Exception as e:
            last_err=e
            # fallback 1: edge save
            try:
                async def _save():
                    comm=edge_tts.Communicate(text, voice=selected_voice, rate=TTS_RATE)
                    await comm.save(mp3)
                try:
                    asyncio.run(_save())
                except RuntimeError:
                    nest_asyncio.apply(); loop=asyncio.get_event_loop(); loop.run_until_complete(_save())
                _mp3_to_wav(mp3, wav_out, atempo)
                wav_fixed, dur=_ensure_min_audio(wav_out, TTS_MIN_SEC, len(text.split()))
                if wav_fixed!=wav_out: shutil.move(wav_fixed, wav_out)
                words=_merge_marks_to_words(text, [], ffprobe_dur(wav_out))
                pathlib.Path(mp3).unlink(missing_ok=True)
                time.sleep(0.25 if LONGFORM else 0.12)
                return ffprobe_dur(wav_out), words
            except Exception as e2:
                last_err=e2
        time.sleep(0.5*attempt)

    # fallback 2: Google TTS
    try:
        q=requests.utils.quote(text.replace('"','').replace("'",""))
        url=f"https://translate.google.com/translate_tts?ie=UTF-8&q={q}&tl={LANG or 'en'}&client=tw-ob&ttsspeed=1.0"
        r=requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30); r.raise_for_status()
        open(mp3,"wb").write(r.content)
        _mp3_to_wav(mp3, wav_out, atempo)
        wav_fixed, dur=_ensure_min_audio(wav_out, TTS_MIN_SEC, len(text.split()))
        if wav_fixed!=wav_out: shutil.move(wav_fixed, wav_out)
        pathlib.Path(mp3).unlink(missing_ok=True)
        return ffprobe_dur(wav_out), _merge_marks_to_words(text, [], ffprobe_dur(wav_out))
    except Exception:
        pass

    # fallback 3: espeak
    try:
        raw=str(pathlib.Path(wav_out).with_suffix(".es.wav"))
        run(["espeak-ng","-v", f"{LANG}-us" if LANG=="en" else LANG, "-s","165","-p","40","-w", raw, text])
        run(["ffmpeg","-y","-i", raw, "-ar","48000","-ac","1","-af", f"dynaudnorm=g=7:f=250,atempo={atempo}", wav_out])
        wav_fixed, dur=_ensure_min_audio(wav_out, TTS_MIN_SEC, len(text.split()))
        if wav_fixed!=wav_out: shutil.move(wav_fixed, wav_out)
        return ffprobe_dur(wav_out), _merge_marks_to_words(text, [], ffprobe_dur(wav_out))
    except Exception as e3:
        pass

    # final: silence with pad
    run(["ffmpeg","-y","-f","lavfi","-t",str(max(1.0,TTS_MIN_SEC)),"-i","anullsrc=r=48000:cl=mono", wav_out])
    return max(1.0,TTS_MIN_SEC), _merge_marks_to_words(text, [], max(1.0,TTS_MIN_SEC))

# ======== Captions ========
def _ass_time(s: float) -> str:
    h=int(s//3600); s-=h*3600; m=int(s//60); s-=m*60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def _to_ass_hex(c: str) -> str:
    c=c.strip()
    if c.startswith("0x"): c=c[2:]
    if c.startswith("#"):  c=c[1:]
    if len(c)==6: c="00"+c
    rr,gg,bb=c[-6:-4],c[-4:-2],c[-2:]
    return f"&H00{bb}{gg}{rr}"

def _build_karaoke_ass(text: str, seg_dur: float, words: List[Tuple[str,float]], is_hook: bool) -> str:
    fontname="DejaVu Sans"
    if VIDEO_W>VIDEO_H:
        fontsize=44 if is_hook else 40
        margin_v=int(VIDEO_H*(0.12 if is_hook else 0.16))
    else:
        fontsize=58 if is_hook else 52
        margin_v=int(VIDEO_H*(0.14 if is_hook else 0.17))
    outline=4 if is_hook else 3
    words_upper=[(re.sub(r"\s+"," ",w.upper()), d) for w,d in words] or [(w.upper(), seg_dur/max(1,len(text.split()))) for w in (text or "…").split()]
    ds=[max(5,int(round(d*100))) for _,d in words_upper]  # centiseconds
    if sum(ds)==0: ds=[50]*len(words_upper)
    target_cs=int(round(seg_dur*100))-int(round(CAPTION_LEAD_MS/10))
    if target_cs<5*len(ds): target_cs=5*len(ds)
    s=sum(ds); scale=(target_cs/(s or 1.0))
    ds=[max(5,int(round(x*scale))) for x in ds]
    while sum(ds)>target_cs:
        for i in range(len(ds)):
            if sum(ds)<=target_cs: break
            if ds[i]>5: ds[i]-=1
    i=0
    while sum(ds)<target_cs:
        ds[i%len(ds)]+=1; i+=1
    kline="".join([f"{{\\k{ds[i]}}}{words_upper[i][0]} " for i in range(len(ds))]).strip()
    ass=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{fontname},{fontsize},{_to_ass_hex(KARAOKE_INACTIVE)},{_to_ass_hex(KARAOKE_ACTIVE)},{_to_ass_hex(KARAOKE_OUTLINE)},&H7F000000,1,0,0,0,100,100,0,0,1,{outline},0,2,50,50,{margin_v},0

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{_ass_time(seg_dur)},Base,,0,0,{margin_v},,{{\\bord{outline}\\shad0}}{kline}
"""
    return ass

def draw_caption(seg: str, text: str, color: str, font: str, outp: str, is_hook=False, words=None):
    seg_dur=ffprobe_dur(seg)
    frames=max(2,int(round(seg_dur*TARGET_FPS)))
    if KARAOKE_CAPTIONS and _HAS_SUBTITLES:
        ass_txt=_build_karaoke_ass(text, seg_dur, words or [], is_hook)
        ass_path=str(pathlib.Path(seg).with_suffix(".ass"))
        pathlib.Path(ass_path).write_text(ass_txt, encoding="utf-8")
        tmp=str(pathlib.Path(outp).with_suffix(".tmp.mp4"))
        try:
            run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",seg,"-vf",f"subtitles='{ass_path}'",
                 "-r",str(TARGET_FPS),"-vsync","cfr","-an","-c:v","libx264","-preset","medium","-crf",str(max(16,CRF_VISUAL-3)),
                 "-pix_fmt","yuv420p","-movflags","+faststart", tmp])
            enforce_video_exact_frames(tmp, frames, outp)
        finally:
            pathlib.Path(ass_path).unlink(missing_ok=True); pathlib.Path(tmp).unlink(missing_ok=True)
        return
    if _HAS_DRAWTEXT:
        wrapped=wrap_lines(clean_caption_text(text).upper(), CAPTION_MAX_LINE, CAPTION_MAX_LINES)
        tf=str(pathlib.Path(seg).with_suffix(".caption.txt")); pathlib.Path(tf).write_text(wrapped, encoding="utf-8")
        lines=wrapped.split("\n"); n=max(1,len(lines)); maxchars=max((len(l) for l in lines), default=1)
        base=60 if is_hook else 50
        ratio=CAPTION_MAX_LINE/max(1,maxchars); fs=int(base*min(1.0,max(0.50,ratio)))
        if n>=5: fs=int(fs*0.92)
        if n>=6: fs=int(fs*0.88)
        fs=max(22,fs)
        if VIDEO_W>VIDEO_H:
            y="(h*0.74 - text_h/2)" if n>=4 else "(h*0.78 - text_h/2)"
        else:
            y="(h*0.58 - text_h/2)" if n>=4 else "h-h/3-text_h/2"
        font_arg=f":fontfile={font.replace(':','\\:').replace(',','\\,').replace('\\','/')}" if font else ""
        col=_ff_color(color); common=f"textfile='{tf}':fontsize={fs}:x=(w-text_w)/2:y={y}:line_spacing=10"
        shadow=f"drawtext={common}{font_arg}:fontcolor=black@0.85:borderw=0"
        box   =f"drawtext={common}{font_arg}:fontcolor=white@0.0:box=1:boxborderw={(22 if is_hook else 18)}:boxcolor=black@0.65"
        main  =f"drawtext={common}{font_arg}:fontcolor={col}:borderw={(5 if is_hook else 4)}:bordercolor=black@0.9"
        vf=f"{shadow},{box},{main},fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB,trim=start_frame=0:end_frame={frames}"
        tmp=str(pathlib.Path(outp).with_suffix(".tmp.mp4"))
        try:
            run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",seg,"-vf",vf,
                 "-r",str(TARGET_FPS),"-vsync","cfr","-an","-c:v","libx264","-preset","medium","-crf",str(max(16,CRF_VISUAL-3)),
                 "-pix_fmt","yuv420p","-movflags","+faststart", tmp])
            enforce_video_exact_frames(tmp, frames, outp)
        finally:
            pathlib.Path(tf).unlink(missing_ok=True); pathlib.Path(tmp).unlink(missing_ok=True)
        return
    if REQUIRE_CAPTIONS:
        raise RuntimeError("Captions required but 'drawtext'/'subtitles' not present.")
    enforce_video_exact_frames(seg, frames, outp)

# ======== Video building ========
def quantize_to_frames(seconds: float, fps: int=TARGET_FPS) -> Tuple[int,float]:
    frames=max(2,int(round(seconds*fps)))
    return frames, frames/float(fps)

def make_segment(src: str, dur_s: float, outp: str):
    frames,qdur=quantize_to_frames(dur_s)
    fade=max(0.08, min(0.22, qdur/8.0)); fade_out=max(0.0, qdur-fade)
    vf=(f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},eq=brightness=0.02:contrast=1.08:saturation=1.1,"
        f"fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB,"
        f"fade=t=in:st=0:d={fade:.2f},fade=t=out:st={fade_out:.2f}:d={fade:.2f}")
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-stream_loop","-1","-t",f"{qdur:.3f}","-i",src,
         "-vf",vf,"-r",str(TARGET_FPS),"-vsync","cfr","-an","-c:v","libx264","-preset","fast","-crf",str(CRF_VISUAL),
         "-pix_fmt","yuv420p","-movflags","+faststart", outp])

def enforce_video_exact_frames(video_in: str, target_frames: int, outp: str):
    target_frames=max(2,int(target_frames))
    vf=f"fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB,trim=start_frame=0:end_frame={target_frames}"
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",video_in,"-vf",vf,"-r",str(TARGET_FPS),"-vsync","cfr",
         "-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),"-pix_fmt","yuv420p","-movflags","+faststart", outp])

def pad_video_to_duration(video_in: str, target_sec: float, outp: str):
    vdur=ffprobe_dur(video_in)
    if vdur>=target_sec-0.02:
        pathlib.Path(outp).write_bytes(pathlib.Path(video_in).read_bytes()); return
    extra=max(0.0, target_sec - vdur)
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",video_in,
         "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={extra:.3f},fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB[v]",
         "-map","[v]","-r",str(TARGET_FPS),"-vsync","cfr","-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),
         "-pix_fmt","yuv420p","-movflags","+faststart", outp])

def concat_videos_filter(files: List[str], outp: str):
    if not files: raise RuntimeError("concat_videos_filter: empty")
    inputs=[]; filters=[]
    for i,p in enumerate(files):
        inputs+=["-i",p]; filters.append(f"[{i}:v]fps={TARGET_FPS},settb=AVTB,setpts=N/{TARGET_FPS}/TB[v{i}]")
    filtergraph=";".join(filters)+";"+"".join(f"[v{i}]" for i in range(len(files)))+f"concat=n={len(files)}:v=1:a=0[v]"
    run(["ffmpeg","-y","-hide_banner","-loglevel","error", *inputs, "-filter_complex", filtergraph, "-map","[v]",
         "-r",str(TARGET_FPS),"-vsync","cfr","-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),
         "-pix_fmt","yuv420p","-movflags","+faststart", outp])

def concat_audios(files: List[str], outp: str):
    if not files: raise RuntimeError("concat_audios: empty")
    lst=str(pathlib.Path(outp).with_suffix(".txt"))
    with open(lst,"w",encoding="utf-8") as f:
        for p in files: f.write(f"file '{p}'\n")
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",lst,"-c","copy", outp])
    pathlib.Path(lst).unlink(missing_ok=True)

def lock_audio_duration(audio_in: str, target_frames: int, outp: str):
    dur=target_frames/float(TARGET_FPS)
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",audio_in,"-af",f"atrim=end={dur:.6f},asetpts=N/SR/TB",
         "-ar","48000","-ac","1","-c:a","pcm_s16le", outp])

def mux(video: str, audio: str, outp: str):
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",video,"-i",audio,"-map","0:v:0","-map","1:a:0",
         "-c:v","copy","-c:a","aac","-b:a","256k","-movflags","+faststart","-muxpreload","0","-muxdelay","0",
         "-avoid_negative_ts","make_zero", outp])

# ======== Pexels / Pixabay ========
def _pexels_headers():
    if not PEXELS_API_KEY: raise RuntimeError("PEXELS_API_KEY missing")
    return {"Authorization": PEXELS_API_KEY}

def _is_vertical_ok(w: int, h: int) -> bool:
    if VIDEO_W>VIDEO_H:  # landscape
        min_w=int(os.getenv("PEXELS_MIN_WIDTH","1280"))
        return (w>=h) and (w>=min_w)
    else:
        min_h=_env_int("PEXELS_MIN_HEIGHT",1280)
        strict=os.getenv("PEXELS_STRICT_VERTICAL","1")=="1"
        return (h>w if strict else h>=w) and h>=min_h

def _pexels_search(query: str, locale: str, page: int=1, per_page: int=30) -> List[Tuple[int,str,int,int,float]]:
    url="https://api.pexels.com/videos/search"
    r=requests.get(url, headers=_pexels_headers(), params={
        "query": query, "per_page": per_page, "page": page,
        "orientation": PEXELS_ORIENT, "size": "large", "locale": locale
    }, timeout=30)
    if r.status_code!=200: return []
    data=r.json() or {}
    out=[]
    for v in data.get("videos",[]):
        vid=int(v.get("id",0)); dur=float(v.get("duration",0.0))
        if dur < _env_int("PEXELS_MIN_DURATION",3) or dur > _env_int("PEXELS_MAX_DURATION",13): continue
        pf=[]
        for x in (v.get("video_files",[]) or []):
            w=int(x.get("width",0)); h=int(x.get("height",0))
            if _is_vertical_ok(w,h): pf.append((w,h,x.get("link")))
        if not pf: continue
        pf.sort(key=lambda t: (abs(t[1]-1600), -(t[0]*t[1])))
        w,h,link=pf[0]; out.append((vid, link, w, h, dur))
    return out

def _pexels_popular(locale: str, page: int=1, per_page: int=40) -> List[Tuple[int,str,int,int,float]]:
    url="https://api.pexels.com/videos/popular"
    r=requests.get(url, headers=_pexels_headers(), params={"per_page": per_page,"page": page}, timeout=30)
    if r.status_code!=200: return []
    data=r.json() or {} ; out=[]
    for v in data.get("videos",[]):
        vid=int(v.get("id",0)); dur=float(v.get("duration",0.0))
        if dur < _env_int("PEXELS_MIN_DURATION",3) or dur > _env_int("PEXELS_MAX_DURATION",13): continue
        pf=[]
        for x in (v.get("video_files",[]) or []):
            w=int(x.get("width",0)); h=int(x.get("height",0))
            if _is_vertical_ok(w,h): pf.append((w,h,x.get("link")))
        if not pf: continue
        pf.sort(key=lambda t:(abs(t[1]-1600), -(t[0]*t[1])))
        w,h,link=pf[0]; out.append((vid,link,w,h,dur))
    return out

def _pixabay_fallback(q: str, need: int, locale: str) -> List[Tuple[int,str]]:
    if not (ALLOW_PIXABAY and PIXABAY_API_KEY): return []
    try:
        r=requests.get("https://pixabay.com/api/videos/", params={
            "key": PIXABAY_API_KEY, "q": q, "safesearch":"true","per_page": min(50,max(10,need*4)),
            "video_type":"film", "order":"popular"
        }, timeout=30)
        if r.status_code!=200: return []
        data=r.json() or {}; outs=[]
        for h in data.get("hits",[]):
            dur=float(h.get("duration",0.0))
            if dur < _env_int("PEXELS_MIN_DURATION",3) or dur > _env_int("PEXELS_MAX_DURATION",13): continue
            vids=h.get("videos",{})
            chosen=None
            for qual in ("large","medium","small","tiny"):
                v=vids.get(qual)
                if not v: continue
                w,hh=int(v.get("width",0)), int(v.get("height",0))
                if _is_vertical_ok(w,hh): chosen=(w,hh,v.get("url")); break
            if chosen: outs.append((int(h.get("id",0)), chosen[2]))
        return outs[:need]
    except Exception:
        return []

def _rank_and_dedup(items: List[Tuple[int,str,int,int,float]], qtokens: Set[str]) -> List[Tuple[int,str]]:
    cand=[]
    for vid,link,w,h,dur in items:
        tokens=set(re.findall(r"[a-z0-9]+",(link or "").lower()))
        overlap=len(tokens & qtokens)
        score=overlap*2.0 + (1.0 if 2.0<=dur<=12.0 else 0.0) + (1.0 if h>=1440 else 0.0)
        cand.append((score,vid,link))
    cand.sort(key=lambda x:x[0], reverse=True)
    out=[]; seen=set()
    for _,vid,link in cand:
        if vid in seen: continue
        seen.add(vid); out.append((vid,link))
    return out

def build_per_scene_queries(sentences: List[str], fallback_terms: List[str], topic: Optional[str]=None) -> List[str]:
    STOP=set("a an the and or but if while of to in on at from by with for about into over after before between during under above across around through this that these those is are was were be been being have has had do does did can could should would may might will your you we our they their he she it its as than then so very more most many much just also only even still yet".split())
    GENER={"great","good","bad","things","stuff","nice"}
    def tok(s):
        s=re.sub(r"[^A-Za-z0-9 ]+"," ", (s or "").lower())
        return [w for w in s.split() if len(w)>=4 and w not in STOP and w not in GENER]
    queries=[]
    fb=[re.sub(r"[^A-Za-z0-9 ]+"," ",t).strip().lower() for t in (fallback_terms or []) if t]
    fb=[ " ".join([w for w in x.split() if w not in STOP][:2]) for x in fb if x]
    fb_idx=0
    for s in sentences:
        ts=tok(s)
        picked=None
        if len(ts)>=2: picked=f"{ts[-2]} {ts[-1]}"
        elif len(ts)==1: picked=ts[0]
        if (not picked or len(picked)<4) and fb:
            picked=fb[fb_idx % len(fb)]; fb_idx+=1
        queries.append(picked or "macro detail")
    if topic:
        ts=tok(topic); base=" ".join(ts[:2]) if ts else ""
        if base: queries.append(base)
    for g in ["city timelapse","ocean waves","forest path","night skyline","macro detail"]:
        if g not in queries: queries.append(g)
    return queries[:max(len(sentences)+3, 10)]

def build_pexels_pool(topic: str, sentences: List[str], search_terms: List[str], need: int, rotation_seed: int=0) -> List[Tuple[int,str]]:
    random.seed(rotation_seed or int(time.time()))
    locale="tr-TR" if LANG.startswith("tr") else "en-US"
    per_scene=build_per_scene_queries(sentences, search_terms, topic=topic)
    topic_tokens=set(re.findall(r"[a-z0-9]+", (topic or "").lower()))
    queries=[]
    seen=set()
    for q in per_scene + search_terms:
        q=(q or "").strip().lower()
        if q and q not in seen:
            seen.add(q); queries.append(q)
    pool=[]
    for q in queries:
        qtok=set(re.findall(r"[a-z0-9]+", q))
        merged=[]
        for page in (1,2,3):
            merged += _pexels_search(q, locale, page=page, per_page=_env_int("PEXELS_PER_PAGE",30))
            if len(merged) >= need*3: break
        ranked=_rank_and_dedup(merged, qtok|topic_tokens)
        pool += ranked[:max(3, need//2)]
        if len(pool) >= need*2: break
    if len(pool) < need:
        merged=[]
        for page in (1,2,3):
            merged += _pexels_popular(locale, page=page, per_page=40)
            if len(merged) >= need*3: break
        pool += _rank_and_dedup(merged, set())[:need*2 - len(pool)]
    if len(pool) < need:
        fb=(queries[-1] if queries else "city")
        pool += _pixabay_fallback(fb, need - len(pool), locale)
    dedup=[]; seen=set()
    for vid,link in pool:
        if vid in seen: continue
        seen.add(vid); dedup.append((vid,link))
    return dedup[:max(need, len(sentences))]

# ======== Gemini (optional) ========
def _gemini_call(prompt: str, model: str, temp: float) -> dict:
    headers={"Content-Type":"application/json","x-goog-api-key":GEMINI_API_KEY}
    payload={"contents":[{"parts":[{"text":prompt}]}], "generationConfig":{"temperature":temp}}
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r=requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code!=200: raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    data=r.json(); txt=""
    try: txt=data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception: txt=json.dumps(data)
    m=re.search(r"\{(?:.|\n)*\}", txt)
    if not m: raise RuntimeError("Gemini parse error: no JSON block")
    raw=re.sub(r"^```json\s*|\s*```$","", m.group(0).strip(), flags=re.MULTILINE)
    return json.loads(raw)

def build_via_gemini(topic_lock: str, user_terms: List[str]) -> Tuple[str,List[str],List[str],str,str,List[str]]:
    if not (USE_GEMINI and GEMINI_API_KEY):
        base=[
            "Small changes add up fast.",
            "Start with one corner of your day.",
            "Swap the hardest step for the easiest win.",
            "Put the item where your hand already goes.",
            "Cut one hidden waste you won't miss.",
            "Repeat it tomorrow at the same time.",
            "Let the habit click before scaling.",
            "What would you try first?"
        ]
        return topic_lock, [clean_caption_text(s) for s in base[:SCENE_COUNT]], user_terms[:8], "", "", []
    template = (
        "Create a 25–40s YouTube Short.\n"
        "Return STRICT JSON keys: topic, sentences (7–8), search_terms (4–10), title, description, tags.\n"
        "- Sentence 1 = HOOK (<=10 words, question/claim)\n"
        "- Sentence 8 = soft CTA for comments (no 'subscribe/like')\n"
        "- 6–12 words per sentence, concrete & visual."
        if not LONGFORM else
        "Create a 3–5 minute YouTube video.\n"
        "Return STRICT JSON keys: topic, sentences (6–10), search_terms (6–12), title, description, tags.\n"
        "- Each 'sentences' item is a SCENE (2–3 sentences, 35–60 words).\n"
        "- Scene 1 = sharp HOOK; last = soft CTA for comments."
    )
    jitter=((ROTATION_SEED or int(time.time()))%13)*0.01
    temp=max(0.6,min(1.2,GEMINI_TEMP + (jitter - 0.06)))
    prompt=f"""{template}

Language: {LANG}
TOPIC (hard lock): {topic_lock}
Seed search terms (use/expand): {", ".join(user_terms[:10]) if user_terms else "(none)"}

Return ONLY JSON.
"""
    data=_gemini_call(prompt, GEMINI_MODEL, temp)
    topic=topic_lock
    sentences=[clean_caption_text(s) for s in (data.get("sentences") or [])][:SCENE_COUNT]
    terms=data.get("search_terms") or []
    if isinstance(terms,str): terms=[terms]
    terms=[re.sub(r"[^A-Za-z0-9 ]+"," ", t).strip().lower() for t in terms if str(t).strip()][:10]
    title=(data.get("title") or "").strip()
    desc=(data.get("description") or "").strip()
    tags=[t.strip() for t in (data.get("tags") or []) if isinstance(t,str) and t.strip()]
    return topic, sentences, terms, title, desc, tags

# ======== CTA overlay ========
def overlay_cta_tail(video_in: str, text: str, outp: str, show_sec: float, font: str):
    vdur=ffprobe_dur(video_in)
    if vdur<=0.1 or not text.strip():
        pathlib.Path(outp).write_bytes(pathlib.Path(video_in).read_bytes()); return
    t0=max(0.0, vdur-max(0.8, show_sec))
    tf=str(pathlib.Path(outp).with_suffix(".cta.txt"))
    wrapped=wrap_lines(text.upper(), max_line=26, max_lines=3)
    pathlib.Path(tf).write_text(wrapped, encoding="utf-8")
    font_arg=f":fontfile={font.replace(':','\\:').replace(',','\\,').replace('\\','/')}" if font else ""
    y_frac=0.16 if VIDEO_W>VIDEO_H else 0.18
    common=f"textfile='{tf}':fontsize=52:x=(w-text_w)/2:y=h*{y_frac}:line_spacing=10"
    box=f"drawtext={common}{font_arg}:fontcolor=white@0.0:box=1:boxborderw=18:boxcolor=black@0.55:enable='gte(t,{t0:.3f})'"
    main=f"drawtext={common}{font_arg}:fontcolor={_ff_color('#3EA6FF')}:borderw=5:bordercolor=black@0.9:enable='gte(t,{t0:.3f})'"
    vf=f"{box},{main},fps={TARGET_FPS},setpts=N/{TARGET_FPS}/TB"
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",video_in,"-vf",vf,"-r",str(TARGET_FPS),"-vsync","cfr",
         "-an","-c:v","libx264","-preset","medium","-crf",str(CRF_VISUAL),"-pix_fmt","yuv420p","-movflags","+faststart", outp])
    pathlib.Path(tf).unlink(missing_ok=True)

# ======== YouTube upload ========
def yt_service():
    cid=os.getenv("YT_CLIENT_ID"); csec=os.getenv("YT_CLIENT_SECRET"); rtok=os.getenv("YT_REFRESH_TOKEN")
    if not (cid and csec and rtok): raise RuntimeError("Missing YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN")
    creds=Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token",
                      client_id=cid, client_secret=csec, scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request()); return build("youtube","v3",credentials=creds, cache_discovery=False)

def upload_youtube(video_path: str, meta: dict) -> str:
    y=yt_service()
    body={"snippet":{"title":meta["title"],"description":meta["description"],"tags":meta.get("tags",[]),
                     "categoryId":"27","defaultLanguage":meta.get("defaultLanguage",LANG),
                     "defaultAudioLanguage":meta.get("defaultAudioLanguage",LANG)},
          "status":{"privacyStatus":meta.get("privacy",VISIBILITY),"selfDeclaredMadeForKids":False}}
    media=MediaFileUpload(video_path,chunksize=-1,resumable=True)
    req=y.videos().insert(part="snippet,status", body=body, media_body=media)
    resp=req.execute()
    return resp.get("id","")

# ======== Meta helpers ========
def build_long_description(channel: str, topic: str, sentences: List[str], tags: List[str]) -> Tuple[str,str,List[str]]:
    hook=(sentences[0].rstrip(" .!?") if sentences else topic or channel)
    title=(hook[:1].upper()+hook[1:])[:95]
    para=" ".join(sentences)
    expl=(f"{para} This video explores “{topic}” with clear, visual steps. "
          "Rewatch to catch micro-details and comment your own tip.")
    tagset=["#shorts","#learn","#visual","#education"]
    for w in re.findall(r"[A-Za-z]{3,}", (topic or ""))[:5]:
        t="#"+w.lower()
        if t not in tagset: tagset.append(t)
    body=(f"{expl}\n\n— Key beats —\n"+ "\n".join([f"• {s}" for s in sentences[:8]]) + "\n\n" + " ".join(tagset))[:4900]
    yt_tags=[]
    for h in tagset:
        k=h[1:]
        if k and k not in yt_tags: yt_tags.append(k)
        if len(yt_tags)>=15: break
    return title, body, yt_tags

# ======== MAIN ========
def main():
    print(f"==> {CHANNEL_NAME} | ASPECT={'16:9' if VIDEO_W>VIDEO_H else '9:16'} | LONGFORM={LONGFORM}")
    font=font_path()

    # 1) Script
    topic_lock = TOPIC or "Interesting Visual Explainers"
    user_terms = SEARCH_TERMS_ENV
    if USE_GEMINI and GEMINI_API_KEY:
        try:
            tpc, sentences, search_terms, ttl, desc, tags = build_via_gemini(topic_lock, user_terms)
        except Exception as e:
            print(f"Gemini error: {str(e)[:160]}")
            tpc=topic_lock
            sentences=[
                "Small changes add up fast.",
                "Start with one corner of your day.",
                "Swap the hardest step for the easiest win.",
                "Put the item where your hand already goes.",
                "Cut one hidden waste you won't miss.",
                "Repeat it tomorrow at the same time.",
                "Let the habit click before scaling.",
                "What would you try first?"
            ][:SCENE_COUNT]
            search_terms=user_terms[:8]; ttl=""; desc=""; tags=[]
    else:
        tpc=topic_lock
        sentences=[
            "Small changes add up fast.",
            "Start with one corner of your day.",
            "Swap the hardest step for the easiest win.",
            "Put the item where your hand already goes.",
            "Cut one hidden waste you won't miss.",
            "Repeat it tomorrow at the same time.",
            "Let the habit click before scaling.",
            "What would you try first?"
        ][:SCENE_COUNT]
        search_terms=user_terms[:8]; ttl=""; desc=""; tags=[]

    if sentences:
        # polish hook/cta
        hook=clean_caption_text(sentences[0]); ws=hook.split()
        if len(ws)>_env_int("HOOK_MAX_WORDS",10): hook=" ".join(ws[:_env_int("HOOK_MAX_WORDS",10)])
        if not re.search(r"[?!]$",hook): hook=hook.rstrip(".")+"?"
        sentences[0]=hook
        if not re.search(r"[.!?]$", sentences[-1].strip()):
            sentences[-1]=sentences[-1].strip()+"."

    print(f"📊 Sentences: {len(sentences)}")

    # 2) TTS
    tmp=tempfile.mkdtemp(prefix="autoshorts_")
    wavs=[]; metas=[]
    print("🎤 TTS…")
    for i,s in enumerate(sentences):
        text=clean_caption_text(s)
        w=str(pathlib.Path(tmp)/f"sent_{i:02d}.wav")
        d, words = tts_to_wav(text, w)
        wavs.append(w); metas.append((text, d, words))
        print(f"   {i+1}/{len(sentences)}: {d:.2f}s")

    # 3) Stock pool
    need=max(len(metas), SCENE_COUNT)
    pool=build_pexels_pool(tpc, [m[0] for m in metas], search_terms, need=need, rotation_seed=ROTATION_SEED)
    if not pool: raise RuntimeError("Pexels: empty pool")
    # download
    print("⬇️ Download clips…")
    downloads={}
    for idx,(vid,link) in enumerate(pool[:need]):
        try:
            f=str(pathlib.Path(tmp)/f"pool_{idx:02d}_{vid}.mp4")
            with requests.get(link, stream=True, timeout=120) as rr:
                rr.raise_for_status()
                with open(f,"wb") as w:
                    for ch in rr.iter_content(8192): w.write(ch)
            if pathlib.Path(f).stat().st_size>300_000: downloads[vid]=f
        except Exception as e:
            print(f"  ! {vid} download fail: {e}")
    if not downloads: raise RuntimeError("No clips downloaded.")
    chosen=[downloads[k] for k in list(downloads.keys())][:len(metas)]
    if len(chosen)<len(metas):  # reuse if needed
        keys=list(downloads.keys())
        while len(chosen)<len(metas):
            chosen.append(downloads[keys[len(chosen)%len(keys)]])

    # 4) Build segments + captions
    print("🎬 Segments…")
    fontp=font_path()
    segs=[]
    for i, ((text,dur,words), src) in enumerate(zip(metas, chosen)):
        raw=str(pathlib.Path(tmp)/f"seg_{i:02d}.mp4")
        make_segment(src, dur, raw)
        colored=str(pathlib.Path(tmp)/f"segsub_{i:02d}.mp4")
        draw_caption(raw, text, CAPTION_COLORS[i%len(CAPTION_COLORS)], fontp, colored, is_hook=(i==0), words=words)
        segs.append(colored)

    # 5) Concat + sync
    print("🎞️ Assemble…")
    vcat=str(pathlib.Path(tmp)/"video_concat.mp4"); concat_videos_filter(segs, vcat)
    acat=str(pathlib.Path(tmp)/"audio_concat.wav"); concat_audios(wavs, acat)
    adur=ffprobe_dur(acat); vdur=ffprobe_dur(vcat)
    if vdur+0.02<adur:
        vp=str(pathlib.Path(tmp)/"video_padded.mp4")
        pad_video_to_duration(vcat, adur, vp); vcat=vp
    a_frames=max(2,int(round(ffprobe_dur(acat)*TARGET_FPS)))
    v_exact=str(pathlib.Path(tmp)/"video_exact.mp4"); enforce_video_exact_frames(vcat, a_frames, v_exact); vcat=v_exact
    a_exact=str(pathlib.Path(tmp)/"audio_exact.wav"); lock_audio_duration(acat, a_frames, a_exact); acat=a_exact
    print(f"🔒 Locked A/V: video={ffprobe_dur(vcat):.3f}s | audio={ffprobe_dur(acat):.3f}s")

    # 6) CTA tail
    try:
        if CTA_ENABLE:
            cta = CTA_TEXT_FORCE or ("Which tip would you try first?" if not LANG.startswith("tr") else "İlk hangisini denersin?")
            vcta=str(pathlib.Path(tmp)/"video_cta.mp4")
            overlay_cta_tail(vcat, cta[:CTA_MAX_CHARS], vcta, CTA_SHOW_SEC, fontp)
            v_exact2=str(pathlib.Path(tmp)/"video_exact_cta.mp4"); enforce_video_exact_frames(vcta, a_frames, v_exact2); vcat=v_exact2
    except Exception as e:
        print(f"CTA overlay skipped: {e}")

    # 7) Mux
    ts=datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe=re.sub(r'[^A-Za-z0-9]+','_', tpc)[:60] or "Video"
    outp=f"{OUT_DIR}/{CHANNEL_NAME}_{safe}_{ts}.mp4"
    print("🔄 Mux…"); mux(vcat, acat, outp)
    print(f"✅ Saved: {outp} ({ffprobe_dur(outp):.2f}s)")

    # 8) Upload (optional)
    title, description, yt_tags = build_long_description(CHANNEL_NAME, tpc, [m[0] for m in metas], [])
    meta={"title": title, "description": description, "tags": yt_tags, "privacy": VISIBILITY,
          "defaultLanguage": LANG, "defaultAudioLanguage": LANG}
    try:
        if os.getenv("UPLOAD_TO_YT","1")=="1":
            print("📤 Uploading to YouTube…")
            vid_id=upload_youtube(outp, meta)
            print(f"🎉 YouTube Video ID: {vid_id}\n🔗 https://youtube.com/watch?v={vid_id}")
        else:
            print("⏭️ Upload disabled (UPLOAD_TO_YT != 1)")
    except Exception as e:
        print(f"❌ Upload skipped: {e}")

    try: shutil.rmtree(tmp); print("🧹 Cleaned temp files")
    except: pass

if __name__ == "__main__":
    main()
