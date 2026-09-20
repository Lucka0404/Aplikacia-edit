"""
=========================================================================
 AI VIDEO EDITOR – ORCHESTRAČNÝ SERVER (FastAPI)
=========================================================================
Tento server prijíma webhook z mobilnej aplikácie (napr. Apple Shortcuts
alebo jednoduchý web formulár), spracuje video podľa zvolených parametrov
(formát + zapnuté/vypnuté funkcie) a poskladá finálny "edit blueprint"
pre Shotstack renderovacie API.

Nasadenie: Render.com alebo Railway.app (obe majú free tier vhodný na
tento typ workloadu, keďže reálne renderovanie beží na strane Shotstacku,
nie na našom serveri).
=========================================================================
"""

import os
import json
import time
import uuid
import logging
from enum import Enum
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    # Podpora pre Render "Secret Files": ak existuje /etc/secrets/.env, načíta sa odtiaľ.
    # Inak sa skúsi klasický .env v root priečinku (lokálny vývoj).
    # Ak používaš Render "Environment Group" / "Environment Variables", tento krok
    # nič nepokazí — premenné budú v os.environ už aj bez neho.
    if os.path.exists("/etc/secrets/.env"):
        load_dotenv("/etc/secrets/.env")
    else:
        load_dotenv()
except ImportError:
    # python-dotenv nie je nainštalovaný — spoliehame sa čisto na
    # premenné prostredia nastavené platformou (Render Environment Group a pod.)
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-orchestrator")

app = FastAPI(title="AI Video Editor Orchestrator", version="2.0.0")

# CORS: povolené pre prípad, že by si mobilnú appku hostovala inde ako na tomto
# serveri (napr. GitHub Pages). Ak appku servíruješ priamo odtiaľto (route "/" nižšie),
# CORS sa vôbec nerieši, keďže požiadavky sú na rovnakej doméne (same-origin).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_mobile_app():
    """
    Servíruje mobilnú web appku (ovládací panel) priamo z tohto servera.
    Súbor mobile_app.html musí byť v koreni repozitára vedľa main.py.
    Po nasadení otvor na mobile: https://tvoj-server.onrender.com/
    """
    return FileResponse("mobile_app.html")


# -------------------------------------------------------------------------
# 1. KONFIGURÁCIA API KĽÚČOV (z premenných prostredia)
# -------------------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SHOTSTACK_API_KEY = os.environ.get("SHOTSTACK_API_KEY")
SHOTSTACK_ENV = os.environ.get("SHOTSTACK_ENV", "stage")  # "stage" alebo "v1" (produkcia)
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
MUBERT_API_KEY = os.environ.get("MUBERT_API_KEY")
REMOVE_BG_API_KEY = os.environ.get("REMOVE_BG_API_KEY")
PUSH_NOTIFICATION_WEBHOOK = os.environ.get("PUSH_NOTIFICATION_WEBHOOK")  # napr. OneSignal / vlastný endpoint

# --- OpenRouter (voliteľná alternatíva k priamemu OpenAI API pre krok analýzy textu) ---
# POZOR: OpenRouter NEPODPORUJE prepis zvuku (Whisper) — to je funkcia špecifická
# pre priame OpenAI API. Preto sa OpenRouter používa iba pre krok "analyze_transcript"
# (GPT analýza prepisu), zatiaľ čo samotný prepis (transcribe_with_whisper) stále
# potrebuje reálny OPENAI_API_KEY z platform.openai.com.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3-coder:free")
USE_OPENROUTER = bool(OPENROUTER_API_KEY)  # ak je nastavený, použije sa namiesto OpenAI pre analýzu

SHOTSTACK_BASE_URL = f"https://api.shotstack.io/{SHOTSTACK_ENV}"

# Jednoduché úložisko stavu úloh (pre produkciu nahraď Redis/DB)
JOBS: Dict[str, Dict[str, Any]] = {}

# Kľúče, bez ktorých pipeline nemôže fungovať vôbec (základné funkcie).
# POZOR: OPENAI_API_KEY je stále povinný aj keď používaš OpenRouter (OPENROUTER_API_KEY)
# pre krok analýzy — Whisper prepis reči funguje iba cez priame OpenAI API.
# Ostatné (Mubert, Remove.bg) sú potrebné len ak sú dané feature toggles zapnuté,
# preto sa kontrolujú samostatne až pri použití.
REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "SHOTSTACK_API_KEY", "PEXELS_API_KEY"]


@app.on_event("startup")
async def validate_environment():
    """
    Pri štarte servera skontroluje, či sú nastavené povinné premenné prostredia,
    a ak niečo chýba, jasne to napíše do logov (namiesto pádu neskôr uprostred
    spracovania videa, keď je to oveľa ťažšie odladiť).
    """
    missing = [key for key in REQUIRED_ENV_VARS if not os.environ.get(key)]
    if missing:
        log.warning(
            "⚠️  CHÝBAJÚCE povinné premenné prostredia: %s. "
            "Skontroluj Render → Environment (alebo Secret File /etc/secrets/.env) "
            "a over presné názvy kľúčov.",
            ", ".join(missing),
        )
    else:
        log.info("✅ Všetky povinné premenné prostredia sú nastavené.")

    if USE_OPENROUTER:
        log.info("ℹ️  Analýza prepisu (GPT krok) beží cez OpenRouter, model: %s", OPENROUTER_MODEL)
    else:
        log.info("ℹ️  Analýza prepisu (GPT krok) beží cez priame OpenAI API (gpt-4o-mini).")

    optional_checks = {
        "MUBERT_API_KEY": "add_music",
        "REMOVE_BG_API_KEY": "remove_background",
    }
    for env_key, feature in optional_checks.items():
        if not os.environ.get(env_key):
            log.info(
                "ℹ️  %s nie je nastavený — funkcia '%s' zlyhá, ak ju používateľ zapne.",
                env_key, feature,
            )


# -------------------------------------------------------------------------
# 2. MODELY FORMÁTOV A ROZLÍŠENÍ
# -------------------------------------------------------------------------
class VideoFormat(str, Enum):
    VERTICAL = "vertical"     # 9:16 – Reels, TikTok, Shorts
    HORIZONTAL = "horizontal"  # 16:9 – YouTube dlhé video, web, TV
    SQUARE = "square"          # 1:1 – Facebook/LinkedIn feed


FORMAT_SPECS = {
    VideoFormat.VERTICAL: {
        "width": 1080, "height": 1920, "aspectRatio": "9:16",
        "crop_strategy": "smart_vertical",
    },
    VideoFormat.HORIZONTAL: {
        "width": 1920, "height": 1080, "aspectRatio": "16:9",
        "crop_strategy": "letterbox_or_fill",
    },
    VideoFormat.SQUARE: {
        "width": 1080, "height": 1080, "aspectRatio": "1:1",
        "crop_strategy": "center_crop",
    },
}


# -------------------------------------------------------------------------
# 3. SCHÉMA POŽIADAVKY Z MOBILU (JSON payload cez HTTP POST)
# -------------------------------------------------------------------------
class FeatureToggles(BaseModel):
    remove_background: bool = False
    add_captions: bool = True
    caption_language: str = Field(default="sk", pattern="^(sk|en)$")
    add_b_roll: bool = False
    add_music: bool = False
    add_sfx: bool = False
    smart_crop: bool = True


class VideoRequest(BaseModel):
    source_video_url: str          # URL na surové video (S3 / Supabase Storage)
    format: VideoFormat = VideoFormat.VERTICAL
    features: FeatureToggles = FeatureToggles()
    brand_color: Optional[str] = "#FF4D4D"
    font_family: Optional[str] = "Montserrat ExtraBold"
    callback_push_token: Optional[str] = None  # token zariadenia pre push notifikáciu


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    render_url: Optional[str] = None
    error: Optional[str] = None


# -------------------------------------------------------------------------
# 4. VSTUPNÝ WEBHOOK ENDPOINT
# -------------------------------------------------------------------------
@app.post("/webhook/process-video", response_model=JobStatusResponse)
async def process_video(payload: VideoRequest, background_tasks: BackgroundTasks):
    """
    Prijme konfiguráciu z mobilu, založí job a spustí spracovanie
    na pozadí (aby mobil hneď dostal odpoveď a nečakal na celý render).
    """
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "render_url": None, "error": None}

    background_tasks.add_task(run_pipeline, job_id, payload)

    return JobStatusResponse(job_id=job_id, status="queued")


@app.get("/webhook/status/{job_id}", response_model=JobStatusResponse)
async def get_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job neexistuje")
    return JobStatusResponse(job_id=job_id, **job)


# -------------------------------------------------------------------------
# 5. HLAVNÁ PIPELINE (beží na pozadí)
# -------------------------------------------------------------------------
async def run_pipeline(job_id: str, req: VideoRequest):
    try:
        JOBS[job_id]["status"] = "processing_audio"

        # --- 5.1 Prepis reči (Whisper) ---
        transcript = await transcribe_with_whisper(
            req.source_video_url, req.features.caption_language
        )

        # --- 5.2 Analýza prepisu cez GPT-4o-mini (hooky, nálada hudby, B-roll kľúčové slová) ---
        JOBS[job_id]["status"] = "analyzing_content"
        analysis = await analyze_transcript(transcript, req.features)

        # --- 5.3 Paralelné zbieranie assetov ---
        JOBS[job_id]["status"] = "gathering_assets"
        broll_clips = []
        if req.features.add_b_roll:
            broll_clips = await fetch_broll_from_pexels(analysis["broll_keywords"])

        music_track_url = None
        if req.features.add_music:
            music_track_url = await fetch_music_track(analysis["music_mood"])

        bg_removed_video_url = req.source_video_url
        if req.features.remove_background:
            JOBS[job_id]["status"] = "removing_background"
            bg_removed_video_url = await remove_background(req.source_video_url)

        # --- 5.4 Zostavenie Shotstack JSON blueprintu ---
        JOBS[job_id]["status"] = "building_edit"
        edit_json = build_shotstack_edit(
            video_url=bg_removed_video_url,
            req=req,
            transcript=transcript,
            broll_clips=broll_clips,
            music_track_url=music_track_url,
        )

        # --- 5.5 Odoslanie na render ---
        JOBS[job_id]["status"] = "rendering"
        render_id = await submit_to_shotstack(edit_json)

        # --- 5.6 Čakanie na dokončenie renderu ---
        render_url = await poll_render_status(render_id)

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["render_url"] = render_url

        if req.callback_push_token:
            await send_push_notification(req.callback_push_token, render_url)

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline zlyhala pre job %s", job_id)
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(exc)


# -------------------------------------------------------------------------
# 6. INTEGRÁCIA: OPENAI WHISPER (prepis + časové značky slov)
# -------------------------------------------------------------------------
async def transcribe_with_whisper(video_url: str, language: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=180) as client:
        video_bytes = (await client.get(video_url)).content

        files = {"file": ("audio.mp4", video_bytes)}
        data = {
            "model": "whisper-1",
            "language": language,  # "sk" alebo "en"
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers, data=data, files=files,
        )
        resp.raise_for_status()
        return resp.json()


# -------------------------------------------------------------------------
# 7. INTEGRÁCIA: GPT-4o-mini (analýza obsahu)
# -------------------------------------------------------------------------
async def analyze_transcript(transcript: Dict[str, Any], features: FeatureToggles) -> Dict[str, Any]:
    prompt = f"""
Analyzuj tento prepis videa a vráť IBA JSON (žiadny iný text) s kľúčmi:
- "hooks": zoznam 1-3 najlepších "hook" viet z úvodu videa
- "music_mood": jedno slovo popisujúce náladu hudby (napr. "energetic", "calm", "dramatic")
- "broll_keywords": zoznam 3-6 anglických kľúčových slov pre vyhľadanie B-roll záberov
- "subtitle_segments": zoznam objektov {{"text", "start", "end"}} pre titulky

Prepis: {json.dumps(transcript, ensure_ascii=False)}
"""
    if USE_OPENROUTER:
        # OpenRouter používa rovnaký formát požiadavky ako OpenAI (chat completions),
        # len iné URL, hlavičky a názov modelu (napr. "qwen/qwen3-coder:free").
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # OpenRouter odporúča tieto dve hlavičky pre lepšie priradenie na dashboarde:
            "HTTP-Referer": "https://tvoj-server.onrender.com",
            "X-Title": "AI Video Editor",
        }
        body = {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
    else:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        body = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Niektoré bezplatné modely na OpenRouteri občas obalia JSON do ```json bloku
            # aj napriek response_format nastaveniu — tu to očistíme ako fallback.
            cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(cleaned)


# -------------------------------------------------------------------------
# 8. INTEGRÁCIA: PEXELS (B-roll)
# -------------------------------------------------------------------------
async def fetch_broll_from_pexels(keywords: List[str]) -> List[Dict[str, str]]:
    clips = []
    async with httpx.AsyncClient(timeout=30) as client:
        for kw in keywords[:4]:
            resp = await client.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": kw, "per_page": 1, "orientation": "portrait"},
            )
            resp.raise_for_status()
            results = resp.json().get("videos", [])
            if results:
                video_files = results[0]["video_files"]
                best = max(video_files, key=lambda v: v.get("width", 0))
                clips.append({"keyword": kw, "url": best["link"]})
    return clips


# -------------------------------------------------------------------------
# 9. INTEGRÁCIA: HUDBA (Mubert – generatívna hudba podľa nálady)
# -------------------------------------------------------------------------
async def fetch_music_track(mood: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api-b2b.mubert.com/v2/TTMGenerate",
            json={"api_key": MUBERT_API_KEY, "mood": mood, "duration": 30},
        )
        if resp.status_code != 200:
            log.warning("Mubert API zlyhalo, hudba sa preskočí")
            return None
        return resp.json().get("data", {}).get("tracks", [{}])[0].get("download_link")


# -------------------------------------------------------------------------
# 10. INTEGRÁCIA: ODSTRÁNENIE POZADIA (Unscreen/Remove.bg Video API)
# -------------------------------------------------------------------------
async def remove_background(video_url: str) -> str:
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "https://api.unscreen.com/v1.0/videos",
            headers={"X-Api-Key": REMOVE_BG_API_KEY},
            data={"video_url": video_url},
        )
        resp.raise_for_status()
        return resp.json()["data"]["attributes"]["result"]["video_url"]


# -------------------------------------------------------------------------
# 11. ZOSTAVENIE SHOTSTACK EDIT JSON (podmienené vrstvy podľa prepínačov)
# -------------------------------------------------------------------------
def build_shotstack_edit(
    video_url: str,
    req: VideoRequest,
    transcript: Dict[str, Any],
    broll_clips: List[Dict[str, str]],
    music_track_url: Optional[str],
) -> Dict[str, Any]:
    spec = FORMAT_SPECS[req.format]
    tracks: List[Dict[str, Any]] = []

    # --- Vrstva titulkov (najvyššia = posledná v poli "tracks") ---
    if req.features.add_captions:
        caption_clips = [
            {
                "asset": {
                    "type": "caption",  # Shotstack "caption" asset s vlastným štýlom
                    "text": seg.get("text", ""),
                    "font": {
                        "family": req.font_family,
                        "color": req.brand_color,
                        "size": 42,
                    },
                    "background": {"color": "#000000", "opacity": 0.35, "padding": 12},
                },
                "start": seg.get("start", 0),
                "length": max(seg.get("end", 1) - seg.get("start", 0), 0.3),
            }
            for seg in transcript.get("subtitle_segments", transcript.get("segments", []))
        ]
        if caption_clips:
            tracks.append({"clips": caption_clips})

    # --- Vrstva B-roll (nad hlavným videom, pod titulkami) ---
    if req.features.add_b_roll and broll_clips:
        step = max(4.0, 12.0 / max(len(broll_clips), 1))
        broll_track_clips = []
        for i, clip in enumerate(broll_clips):
            broll_track_clips.append({
                "asset": {"type": "video", "src": clip["url"], "volume": 0},
                "start": i * (step + 2),
                "length": step,
                "fit": spec["crop_strategy"] if req.format != VideoFormat.HORIZONTAL else "cover",
                "transition": {"in": "fade", "out": "fade"},
            })
        tracks.append({"clips": broll_track_clips})

    # --- Hlavná video vrstva ---
    tracks.append({
        "clips": [{
            "asset": {"type": "video", "src": video_url},
            "start": 0,
            "length": "auto",
            "fit": spec["crop_strategy"] if req.features.smart_crop else "crop",
        }]
    })

    # --- Zvukové vrstvy: SFX ---
    if req.features.add_sfx:
        tracks.append({
            "clips": [
                {
                    "asset": {"type": "audio", "src": "https://assets.shotstack.io/sfx/whoosh.mp3"},
                    "start": 0.0, "length": 1.0,
                },
                {
                    "asset": {"type": "audio", "src": "https://assets.shotstack.io/sfx/pop.mp3"},
                    "start": 5.0, "length": 0.6,
                },
            ]
        })

    # --- Zvuková vrstva: hudba na pozadí s auto-duckingom ---
    if req.features.add_music and music_track_url:
        tracks.append({
            "clips": [{
                "asset": {
                    "type": "audio",
                    "src": music_track_url,
                    "volume": 0.25,
                    "effect": "fadeInFadeOut",
                },
                "start": 0,
                "length": "auto",
            }]
        })

    edit = {
        "timeline": {
            "background": "#000000",
            "tracks": tracks,
        },
        "output": {
            "format": "mp4",
            "size": {"width": spec["width"], "height": spec["height"]},
        },
    }
    return edit


# -------------------------------------------------------------------------
# 12. INTEGRÁCIA: SHOTSTACK (render + polling)
# -------------------------------------------------------------------------
async def submit_to_shotstack(edit_json: Dict[str, Any]) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SHOTSTACK_BASE_URL}/render",
            headers={"x-api-key": SHOTSTACK_API_KEY, "Content-Type": "application/json"},
            json=edit_json,
        )
        resp.raise_for_status()
        return resp.json()["response"]["id"]


async def poll_render_status(render_id: str, max_wait_seconds: int = 600) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        elapsed = 0
        while elapsed < max_wait_seconds:
            resp = await client.get(
                f"{SHOTSTACK_BASE_URL}/render/{render_id}",
                headers={"x-api-key": SHOTSTACK_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json()["response"]
            status = data["status"]
            if status == "done":
                return data["url"]
            if status == "failed":
                raise RuntimeError(f"Shotstack render zlyhal: {data.get('error')}")
            time.sleep(5)
            elapsed += 5
        raise TimeoutError("Render trval príliš dlho")


# -------------------------------------------------------------------------
# 13. PUSH NOTIFIKÁCIA PO DOKONČENÍ
# -------------------------------------------------------------------------
async def send_push_notification(device_token: str, render_url: str):
    if not PUSH_NOTIFICATION_WEBHOOK:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(PUSH_NOTIFICATION_WEBHOOK, json={
            "token": device_token,
            "title": "Video je hotové! 🎬",
            "body": "Tvoje video bolo úspešne vyrenderované.",
            "url": render_url,
        })


# -------------------------------------------------------------------------
# 14. ZDRAVOTNÝ ENDPOINT
# -------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/env")
async def debug_env():
    """
    DOČASNÝ diagnostický endpoint — ukazuje LEN či je premenná nastavená a akú má
    dĺžku (nikdy jej skutočnú hodnotu), aby si vedela odhaliť prázdne/chýbajúce kľúče
    bez toho, aby unikli do logov alebo prehliadača.
    Po vyriešení problému tento endpoint z main.py zmaž (bezpečnostné odporúčanie).
    """
    keys = [
        "OPENAI_API_KEY", "OPENROUTER_API_KEY", "SHOTSTACK_API_KEY",
        "SHOTSTACK_ENV", "PEXELS_API_KEY", "MUBERT_API_KEY", "REMOVE_BG_API_KEY",
    ]
    report = {}
    for k in keys:
        val = os.environ.get(k)
        if val is None:
            report[k] = "❌ nie je nastavený vôbec"
        elif val.strip() == "":
            report[k] = "⚠️ nastavený, ale je PRÁZDNY (alebo len medzery)"
        else:
            report[k] = f"✅ nastavený, dĺžka {len(val)} znakov"
    return report
