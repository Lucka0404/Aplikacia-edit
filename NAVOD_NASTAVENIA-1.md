# Návod na nastavenie – AI Video Editor Pipeline

## 1. Architektúra v skratke

```
📱 Mobil (Apple Shortcuts / web formulár)
      │  POST JSON payload
      ▼
🖥️  Orchestrátor (FastAPI na Render/Railway)
      │
      ├── OpenAI Whisper API        → prepis + časové značky
      ├── GPT-4o-mini                → hooky, nálada hudby, B-roll kľúčové slová
      ├── Pexels API                 → B-roll klipy
      ├── Mubert API                 → generatívna hudba
      ├── Unscreen / Remove.bg API   → odstránenie pozadia
      └── Shotstack API              → finálny render (JSON blueprint)
      │
      ▼
🔔 Push notifikácia na mobil s odkazom na hotové video
```

## 2. Získanie API kľúčov

| Služba | Kde získať kľúč | Free tier |
|---|---|---|
| OpenAI (Whisper + GPT-4o-mini) | platform.openai.com/api-keys | Platí sa podľa spotreby, Whisper je lacný (~0,006 $/min) |
| Shotstack | shotstack.io → Dashboard → API Keys | Áno, "stage" prostredie je zdarma s vodoznakom |
| Pexels | pexels.com/api | 100 % zdarma, len treba registráciu |
| Mubert | mubert.com/render (B2B API) | Má bezplatnú úroveň pre testovanie |
| Unscreen / Remove.bg Video | unscreen.com alebo remove.bg/api | Obmedzené bezplatné kredity |

## 3. Premenné prostredia (nastav na Render/Railway)

```bash
OPENAI_API_KEY=          # POVINNÝ vždy — potrebný pre Whisper prepis (OpenRouter ho nepodporuje)
SHOTSTACK_API_KEY=       # POVINNÝ
SHOTSTACK_ENV=stage      # POVINNÝ — po otestovaní prepni na "v1" (produkcia)
PEXELS_API_KEY=          # POVINNÝ

# Voliteľné — ak nastavíš OPENROUTER_API_KEY, krok analýzy prepisu (GPT)
# pôjde cez OpenRouter namiesto priameho OpenAI (napr. kvôli free modelom).
OPENROUTER_API_KEY=
OPENROUTER_MODEL=qwen/qwen3-coder:free

MUBERT_API_KEY=          # voliteľný — len ak chceš add_music
REMOVE_BG_API_KEY=       # voliteľný — len ak chceš remove_background
PUSH_NOTIFICATION_WEBHOOK=  # voliteľný
```

**Dôležité:** `OPENAI_API_KEY` treba mať nastavený vždy — aj keď používaš OpenRouter pre analýzu textu, samotný prepis zvuku (Whisper) beží iba cez priame OpenAI API, OpenRouter túto funkciu neponúka.

## 4. Nasadenie

### Render.com
1. Nový "Web Service" → pripoj GitHub repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Pridaj vyššie uvedené premenné prostredia v sekcii Environment.

### requirements.txt
```
fastapi
uvicorn
httpx
pydantic
python-dotenv
```

## 4b. Dve možnosti uloženia kľúčov na Render

**Možnosť A – Environment Group (odporúčané, jednoduchšie):**
Premenné sa nastavia priamo v "Environment" sekcii alebo v zdieľanej "Environment Group" a Render ich automaticky vloží do `os.environ` pri štarte kontajnera. `main.py` ich rovno prečíta cez `os.environ.get(...)`.

**Možnosť B – Secret Files:**
Ak radšej uložíš všetky kľúče ako jeden `.env` súbor cez Render "Secret Files" (File name: `.env`), Render ho sprístupní na ceste `/etc/secrets/.env`. `main.py` teraz pri štarte automaticky skúsi tento súbor načítať pomocou `python-dotenv`, takže funguje aj tento spôsob bez ďalších úprav kódu.

Oba spôsoby fungujú súčasne — netreba sa rozhodovať, kód si poradí s hocktorým.

## 4c. Kontrola pri štarte servera

Po nasadení skontroluj logy v Render. Server pri štarte vypíše buď:
- `✅ Všetky povinné premenné prostredia sú nastavené.` — všetko v poriadku,
- alebo `⚠️ CHÝBAJÚCE povinné premenné prostredia: ...` — presný zoznam toho, čo chýba alebo má preklep v názve.

Toto sa týka povinných kľúčov (OpenAI, Shotstack, Pexels). Voliteľné kľúče (Mubert, Remove.bg) sa kontrolujú zvlášť a vypíšu len informačnú hlášku — pipeline beží ďalej, zlyhá až vtedy, keď používateľ danú funkciu (`add_music`/`remove_background`) skutočne zapne.

## 5. Volanie z mobilu (Apple Shortcuts)

V Skratkách (Shortcuts) postav "Get Contents of URL" akciu:

- **URL:** `https://tvoj-server.onrender.com/webhook/process-video`
- **Metóda:** POST
- **Hlavičky:** `Content-Type: application/json`
- **Telo (JSON):** vyplň pomocou menu volieb v Skratkách (formát, prepínače) a vlož ako telo požiadavky – presne podľa `mobile_request_schema.json`.

Príklad cez `curl` (na testovanie):

```bash
curl -X POST https://tvoj-server.onrender.com/webhook/process-video \
  -H "Content-Type: application/json" \
  -d '{
    "source_video_url": "https://tvoj-bucket.s3.amazonaws.com/raw_video_123.mp4",
    "format": "vertical",
    "features": {
      "remove_background": true,
      "add_captions": true,
      "caption_language": "sk",
      "add_b_roll": true,
      "add_music": true,
      "add_sfx": false,
      "smart_crop": true
    },
    "brand_color": "#FF4D4D",
    "font_family": "Montserrat ExtraBold"
  }'
```

Odpoveď obsahuje `job_id`. Stav si vieš skontrolovať cez:

```bash
GET /webhook/status/{job_id}
```

## 6. Ako fungujú podmienené vrstvy

Funkcia `build_shotstack_edit()` v `main.py` pridáva do Shotstack JSON-u **len tie vrstvy (tracks)**, ktoré sú v `features` zapnuté:

- `add_captions=False` → vrstva titulkov sa vôbec nepridá do `tracks`.
- `add_b_roll=False` → žiadne volanie na Pexels, žiadna B-roll vrstva.
- `add_music=False` → žiadne volanie na Mubert, žiadna hudobná vrstva → žiadne zbytočné API náklady.
- `add_sfx=False` → bez zvukových efektov.

Toto zároveň šetrí náklady na externé API, keďže sa nevolajú služby pre funkcie, ktoré si používateľ nezvolil.

## 8. Mobilná web appka (ovládací panel)

Súbor `mobile_app.html` je jednoduchá mobilná appka (funguje priamo v prehliadači, žiadna inštalácia) s prepínačom SK/EN, výberom formátu, všetkými feature-toggle prepínačmi a sekciou "Pro štýl" s promptom pre editoriálny koláž-štýl cez Google Flow.

**Nasadenie (najjednoduchšie):**
1. Nahraj `mobile_app.html` do koreňa svojho GitHub repozitára, presne vedľa `main.py`.
2. Redeploy na Render (push do repa spustí auto-deploy).
3. Na mobile otvor: `https://tvoj-server.onrender.com/` — appka sa načíta priamo z tvojho backendu, takže žiadne CORS problémy (rovnaká doména).
4. V appke v sekcii "01 Server" zadaj tú istú URL (`https://tvoj-server.onrender.com`) — appka si ju zapamätá v telefóne (localStorage).
5. Pridaj si stránku na plochu telefónu (v Safari/Chrome "Pridať na plochu") — správa sa potom takmer ako natívna appka.

**Poznámka k sekcii "Pro štýl":** Editoriálny koláž-štýl cez Google Flow (Google Omni model) je samostatný manuálny krok — nedá sa automatizovať cez Shotstack, pretože ide o iný nástroj (Google-e generatívne video). Appka ti len uľahčí skopírovanie hotového promptu; samotné video musíš vygenerovať priamo v Google Flow.

## 9. Odporúčané ďalšie kroky

1. Nahraď in-memory `JOBS` slovník za Redis alebo databázu (pre viac súbežných používateľov).
2. Pridaj autentifikáciu webhooku (API kľúč v hlavičke, aby ho nemohol volať hocikto).
3. Pre skutočné face-tracking smart-crop zváž `OpenCV` (Haar cascade / MediaPipe) priamo na serveri namiesto externého Auto-Reframe API – ušetríš náklady.
4. Otestuj najprv v `SHOTSTACK_ENV=stage`, až potom prepni na produkciu.
