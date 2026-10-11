# =============================================================================
# BOT DE PLENS I ÀUDIOS LLARGS — La TdG · 04.10.2026
#
# S'executa a GitHub Actions (repositori latdg/plens, workflow plens.yml) en
# quatre passos:
#   python3 plens.py preparar       descarrega l'àudio i el talla en trossos
#   python3 plens.py transcriure N  transcriu el tros N (els trossos van en paral·lel)
#   python3 plens.py parlants       identifica qui parla (pyannote, com WhisperX), d'una tirada
#   python3 plens.py parlants_tros N  identifica qui parla al tros N (canvi 11.10.2026: els
#                                   trossos van en paral·lel i «unir» ajunta les veus iguals)
#   python3 plens.py unir           ajunta-ho tot, passa el corrector i ho envia
#
# RESUM PER PUNTS (canvi 10.10.2026): si al missatge del bot hi ha l'ordre del
# dia (PDF o text) o la paraula «resum», després de la transcripció s'envia un
# resum per punts fet amb Gemini: qui és cada «Parlant N» (a partir de la
# composició del consistori, carpeta consistoris/), resum de cada punt,
# intervencions, votació i titulars possibles. Secret: GEMINI_API_KEY.
#
# Models (MODEL):
#   turbo -> Whisper large-v3-turbo: amb signes de puntuació i majúscules.
#   aina  -> projecte-aina/faster-whisper-large-v3-ca-3catparla (Projecte Aina,
#            entrenat amb català): segons la seva fitxa, transcriu SENSE signes
#            de puntuació. Més lent.
# =============================================================================
import glob, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT = os.environ.get("CHAT_ID", "").strip()
MISSATGE = os.environ.get("MISSATGE_ID", "").strip()
ESTAT = os.environ.get("ESTAT_ID", "").strip()
FILE_ID = os.environ.get("FILE_ID", "").strip()
URL = os.environ.get("URL_AUDIO", "").strip()
NOM = os.environ.get("NOM", "").strip() or "ple"
TITOL = os.environ.get("TITOL", "").strip()
MODEL = (os.environ.get("MODEL", "") or "turbo").strip().lower()
PARLANTS = os.environ.get("PARLANTS", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
API = "https://api.telegram.org/bot" + TOKEN

# Resum per punts (canvi 10.10.2026). ORDRE pot ser:
#   ""              -> sense resum
#   "resum"         -> resum sense ordre del dia (Gemini dedueix els punts)
#   "pdf:<file_id>" -> PDF de l'ordre del dia enviat al bot
#   qualsevol text  -> l'ordre del dia enganxat al missatge
ORDRE = os.environ.get("ORDRE", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-flash-latest", "gemini-2.5-flash"] if m]

# Composició de cada consistori: fitxer de text a la carpeta consistoris/.
# Es tria pel títol o l'ordre del dia; si no hi surt cap altre municipi, la Bisbal.
CONSISTORIS = [("corçà", "corca"), ("forallac", "forallac"), ("cruïlles", "cruilles"), ("monells", "cruilles")]
CONSISTORI_PER_DEFECTE = "la-bisbal"

TREBALL = "treball"
TROS_MIN = 15            # minuts de cada tros (es talla en un silenci proper)
MAX_TROSSOS = 20
MODELS = {"turbo": "large-v3-turbo", "aina": "projecte-aina/faster-whisper-large-v3-ca-3catparla"}
NOMS_MODEL = {"turbo": "Whisper large-v3-turbo", "aina": "Projecte Aina (3CatParla)"}


# -----------------------------------------------------------------------------
# TELEGRAM
# -----------------------------------------------------------------------------
def tg(metode, dades):
    cos = urllib.parse.urlencode(dades).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(API + "/" + metode, data=cos), timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("Telegram %s: HTTP %s %s" % (metode, e.code, e.read()[:200]))
        if e.code in (401, 404):
            print("::error::El secret TELEGRAM_TOKEN_PLENS no és un testimoni de bot vàlid.")
    except Exception as e:
        print("Telegram %s: %s" % (metode, e))
    return {}


def estat(text):
    print(text)
    if ESTAT:
        tg("editMessageText", {"chat_id": CHAT, "message_id": ESTAT, "text": ("📥 Rebut! " + text)[:4096]})


def enviar_document(cami, llegenda):
    import requests
    with open(cami, "rb") as f:
        d = {"chat_id": CHAT, "caption": llegenda[:1024]}
        if MISSATGE:
            d["reply_to_message_id"] = MISSATGE
            d["allow_sending_without_reply"] = "true"
        r = requests.post(API + "/sendDocument", data=d, files={"document": (os.path.basename(cami), f)}, timeout=180)
        if r.status_code != 200:
            print("sendDocument:", r.status_code, r.text[:200])


def enviar_text(text):
    d = {"chat_id": CHAT, "text": text[:4096], "disable_web_page_preview": "true"}
    if MISSATGE:
        d["reply_to_message_id"] = MISSATGE
        d["allow_sending_without_reply"] = "true"
    tg("sendMessage", d)


def enviar_en_missatges(blocs, maxim=3900):
    """Envia blocs de text en missatges de com a molt 'maxim' caràcters, sense partir cap bloc."""
    actual = ""
    for b in blocs:
        while len(b) > maxim:          # un sol paràgraf massa llarg: es parteix per frases
            tall = b.rfind(". ", 0, maxim)
            tall = tall + 1 if tall > maxim // 2 else maxim
            if actual:
                enviar_text(actual)
                time.sleep(1)
                actual = ""
            enviar_text(b[:tall].strip())
            time.sleep(1)
            b = b[tall:].strip()
        if actual and len(actual) + len(b) + 2 > maxim:
            enviar_text(actual)
            time.sleep(1)
            actual = ""
        actual = (actual + "\n\n" + b) if actual else b
    if actual:
        enviar_text(actual)


def hms(s):
    s = int(s)
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def durada_text(s):
    s = int(s)
    return ("%d h %02d min" % (s // 3600, s % 3600 // 60)) if s >= 3600 else ("%d min" % max(1, s // 60))


def net(nom):
    nom = re.sub(r"[^\w\-. ]+", "", nom, flags=re.UNICODE).strip().replace(" ", "_")
    nom = re.sub(r"\.(ogg|oga|opus|mp3|m4a|wav|mp4|webm|aac|flac|mkv|mov)$", "", nom, flags=re.I)
    return (nom or "ple")[:60]


def llegir_json(cami, defecte):
    try:
        with open(cami, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defecte


# -----------------------------------------------------------------------------
# 1. PREPARAR: descarregar i tallar en trossos
# -----------------------------------------------------------------------------
def descarregar():
    global NOM
    os.makedirs("entrada", exist_ok=True)

    if FILE_ID:
        r = tg("getFile", {"file_id": FILE_ID})
        cami = (r.get("result") or {}).get("file_path")
        if not cami:
            raise RuntimeError("Telegram no deixa descarregar aquest fitxer (els bots només poden baixar fitxers de fins a 20 MB). "
                               "Puja'l a Google Drive i envia'm l'enllaç.")
        desti = os.path.join("entrada", os.path.basename(cami))
        urllib.request.urlretrieve("https://api.telegram.org/file/bot" + TOKEN + "/" + cami, desti)
        return desti

    if URL and re.search(r"(drive|docs)\.google\.com", URL):
        m = re.search(r"/d/([A-Za-z0-9_-]{10,})", URL) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", URL)
        if not m:
            raise RuntimeError("No reconec aquest enllaç de Google Drive. Fes servir l'enllaç de compartir del fitxer.")
        import gdown
        try:
            desti = gdown.download(id=m.group(1), output="entrada/", quiet=True)
        except Exception as e:
            print("gdown:", e)
            desti = None
        if not desti or not os.path.exists(desti) or os.path.getsize(desti) < 1000:
            raise RuntimeError("No he pogut descarregar el fitxer de Google Drive. Comprova que està compartit amb "
                               "\"Qualsevol persona que tingui l'enllaç\" i torna-me'l a enviar.")
        NOM = os.path.basename(desti)
        return desti

    if URL:
        ordre = ["yt-dlp", "-f", "bestaudio/best", "--no-playlist", "-o", "entrada/audio.%(ext)s", URL]
        p = subprocess.run(ordre, capture_output=True, text=True)
        print(p.stderr[-1500:])
        fitxers = glob.glob("entrada/audio.*")
        if p.returncode != 0 or not fitxers:
            if re.search(r"youtu\.?be", URL, re.I):
                raise RuntimeError("YouTube no deixa descarregar vídeos des dels servidors de GitHub. "
                                   "Baixa'n l'àudio amb OmniGet (MP3), puja'l a Google Drive i envia'm l'enllaç.")
            raise RuntimeError("No s'ha pogut descarregar l'enllaç (" + (p.stderr or "error desconegut").strip()[-300:] + ")")
        return fitxers[0]

    raise RuntimeError("No hi ha cap fitxer ni enllaç per transcriure.")


def durada_segons(cami):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", cami],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def silencis(cami):
    """Punts mitjans dels silencis de l'àudio (per tallar-lo sense partir paraules)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", cami, "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
                       capture_output=True, text=True)
    inicis = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    finals = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    return [(a + b) / 2 for a, b in zip(inicis, finals)]


def preparar():
    os.makedirs(TREBALL, exist_ok=True)
    estat("⏳ Descarregant l'àudio...")
    origen = descarregar()

    sencer = os.path.join(TREBALL, "sencer.wav")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", origen,
                        "-vn", "-ac", "1", "-ar", "16000", sencer])
    if p.returncode != 0 or not os.path.exists(sencer):
        raise RuntimeError("No s'ha pogut convertir l'àudio (el fitxer pot estar malmès o no tenir so).")

    durada = durada_segons(sencer)
    if durada < 5:
        raise RuntimeError("L'àudio és massa curt o no té so.")

    # Talls cada TROS_MIN minuts, desplaçats al silenci més proper (±45 s).
    mida = max(TROS_MIN * 60, durada / MAX_TROSSOS)
    punts = silencis(sencer)
    talls = [0.0]
    k = 1
    while k * mida < durada - 60:
        objectiu = k * mida
        propers = [s for s in punts if abs(s - objectiu) <= 45]
        talls.append(min(propers, key=lambda s: abs(s - objectiu)) if propers else objectiu)
        k += 1
    talls.append(durada)

    trams = []
    for i in range(len(talls) - 1):
        fitxer = os.path.join(TREBALL, "tros_%02d.wav" % i)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", sencer,
                        "-ss", "%.2f" % talls[i], "-to", "%.2f" % talls[i + 1], "-c", "copy", fitxer], check=True)
        trams.append({"i": i, "inici": talls[i], "fi": talls[i + 1], "fitxer": fitxer})

    info = {"durada": durada, "nom": NOM, "trams": trams}
    with open(os.path.join(TREBALL, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f)

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
        f.write("trams=" + json.dumps([t["i"] for t in trams]) + "\n")
        f.write("durada=" + str(int(durada)) + "\n")   # canvi 10.10.2026: per al seguiment

    estat("⏳ Àudio de " + durada_text(durada) + ". Transcrivint en " + str(len(trams)) +
          (" tros" if len(trams) == 1 else " trossos") + " alhora amb " + NOMS_MODEL.get(MODEL, MODEL) +
          " i identificant qui parla... (pot trigar una estona)")


# -----------------------------------------------------------------------------
# 2. TRANSCRIURE un tros
# -----------------------------------------------------------------------------
def transcriure(n):
    from faster_whisper import WhisperModel

    info = llegir_json(os.path.join(TREBALL, "info.json"), {})
    tros = [t for t in info.get("trams", []) if t["i"] == n][0]

    model = WhisperModel(MODELS.get(MODEL, MODELS["turbo"]), device="cpu", compute_type="int8",
                         cpu_threads=os.cpu_count() or 4)
    segments, _ = model.transcribe(tros["fitxer"], language="ca", beam_size=5, vad_filter=True,
                                   word_timestamps=True, condition_on_previous_text=False)

    paraules = []
    for seg in segments:
        for w in (seg.words or []):
            paraules.append({"s": round(tros["inici"] + w.start, 2), "e": round(tros["inici"] + w.end, 2), "w": w.word})
    print("Tros %d: %d paraules" % (n, len(paraules)))

    with open(os.path.join(TREBALL, "paraules_%02d.json" % n), "w", encoding="utf-8") as f:
        json.dump(paraules, f, ensure_ascii=False)


# -----------------------------------------------------------------------------
# SEGUIMENT (canvi 10.10.2026): mentre es transcriuen els trossos, mira a GitHub
# quants n'han acabat i escriu el percentatge al missatge «📥 Rebut!» cada minut.
# -----------------------------------------------------------------------------
def seguiment():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    gh = os.environ.get("GH_TOKEN", "")
    durada = int(os.environ.get("DURADA", "0") or 0)
    if not (repo and run and gh and ESTAT):
        print("Seguiment: falten dades; no es fa.")
        return

    inici = time.time()
    darrer_text = ""
    while time.time() - inici < 290 * 60:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/actions/runs/%s/jobs?per_page=100" % (repo, run),
                headers={"Authorization": "Bearer " + gh, "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                feines = json.loads(r.read()).get("jobs", [])
        except Exception as e:
            print("Seguiment:", e)
            time.sleep(60)
            continue

        trossos = [j for j in feines if j.get("name", "").startswith("transcriure")]
        parl = [j for j in feines if j.get("name", "") == "parlants" and j.get("conclusion") != "skipped"]
        parl_t = [j for j in feines if j.get("name", "").startswith("parlants_trossos") and j.get("conclusion") != "skipped"]
        fets = [j for j in trossos if j.get("status") == "completed"]
        fallats = [j for j in fets if j.get("conclusion") not in ("success", "skipped")]
        en_cua = [j for j in trossos if j.get("status") == "queued"]
        total = len(trossos)

        if parl_t:
            parl_fets = [j for j in parl_t if j.get("status") == "completed"]
            text_parl = "%d %% (%d de %d trossos)" % (int(len(parl_fets) * 100 / len(parl_t)), len(parl_fets), len(parl_t))
            parl_acabat = len(parl_fets) == len(parl_t)
        elif parl and parl[0].get("status") == "completed":
            text_parl = "✅ fet" if parl[0].get("conclusion") == "success" else "⚠️ no s'ha pogut"
            parl_acabat = True
        else:
            text_parl = "⏳ en curs"
            parl_acabat = False

        minuts = int((time.time() - inici) / 60)
        linies = []
        if durada:
            linies.append("Àudio de " + durada_text(durada) + " · " + NOMS_MODEL.get(MODEL, MODEL))
        if total:
            pct = int(len(fets) * 100 / total)
            linies.append("📝 Transcripció: %d %% (%d de %d trossos)" % (pct, len(fets), total) +
                          (" · %d en cua" % len(en_cua) if en_cua else "") +
                          (" · ⚠️ %d amb error" % len(fallats) if fallats else ""))
        else:
            linies.append("📝 Transcripció: començant...")
        linies.append("🗣️ Qui parla: " + text_parl)
        linies.append("⏱️ Treballant des de fa %d min" % minuts)

        acabat = total and len(fets) == total and (parl or parl_t) and parl_acabat
        if acabat:
            linies.append("⏳ Ajuntant-ho tot. Després passarà el corrector de Softcatalà" +
                          (" i farà el resum per punts." if ORDRE else "."))

        text = "⏳ " + "\n".join(linies)
        if text != darrer_text:
            estat(text)
            darrer_text = text
        if acabat:
            return
        time.sleep(60)


# -----------------------------------------------------------------------------
# 3. PARLANTS (qui parla): pyannote, la mateixa eina que fa servir WhisperX
# -----------------------------------------------------------------------------
def parlants():
    sortida = os.path.join(TREBALL, "parlants.json")
    resultat = {"torns": [], "error": ""}
    try:
        if not HF_TOKEN:
            raise RuntimeError("falta el secret HF_TOKEN")
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
        opcions = {}
        if PARLANTS.isdigit() and int(PARLANTS) > 0:
            opcions["num_speakers"] = int(PARLANTS)
        diar = pipeline(os.path.join(TREBALL, "sencer.wav"), **opcions)
        resultat["torns"] = [{"s": round(t.start, 2), "e": round(t.end, 2), "p": p}
                             for t, _, p in diar.itertracks(yield_label=True)]
        print("Parlants: %d torns" % len(resultat["torns"]))
    except Exception as e:
        print("::warning::No s'ha pogut identificar qui parla: %s" % e)
        resultat["error"] = str(e)[:300]
    with open(sortida, "w", encoding="utf-8") as f:
        json.dump(resultat, f)


# Canvi 11.10.2026: qui parla, tros a tros (en paral·lel). A més dels torns, es
# desa una «empremta» (embedding) de cada veu, per poder saber després quines
# veus de trossos diferents són la mateixa persona.
def parlants_tros(n):
    sortida = os.path.join(TREBALL, "parlants_%02d.json" % n)
    resultat = {"i": n, "torns": [], "empremtes": {}, "error": ""}
    try:
        if not HF_TOKEN:
            raise RuntimeError("falta el secret HF_TOKEN")
        info = llegir_json(os.path.join(TREBALL, "info.json"), {})
        tros = [t for t in info.get("trams", []) if t["i"] == n][0]
        import numpy as np
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
        opcions = {}
        if PARLANTS.isdigit() and int(PARLANTS) > 0:
            opcions["max_speakers"] = int(PARLANTS)   # en un tros hi pot parlar menys gent
        diar, empremtes = pipeline(tros["fitxer"], return_embeddings=True, **opcions)
        for t, _, p in diar.itertracks(yield_label=True):
            resultat["torns"].append({"s": round(tros["inici"] + t.start, 2), "e": round(tros["inici"] + t.end, 2), "p": p})
        for k, etiqueta in enumerate(diar.labels()):
            if empremtes is not None and k < len(empremtes) and np.all(np.isfinite(empremtes[k])):
                resultat["empremtes"][etiqueta] = [round(float(x), 5) for x in empremtes[k]]
        print("Tros %d: %d torns, %d veus" % (n, len(resultat["torns"]), len(resultat["empremtes"])))
    except Exception as e:
        print("::warning::Tros %d: no s'ha pogut identificar qui parla: %s" % (n, e))
        resultat["error"] = str(e)[:300]
    with open(sortida, "w", encoding="utf-8") as f:
        json.dump(resultat, f)


# Ajunta els resultats dels trossos: agrupa les empremtes de veu de tots els
# trossos (mateix mètode i llindar que pyannote 3.1) i torna els torns amb una
# etiqueta comuna per a cada persona.
LLINDAR_VEUS = 0.7045


def ajuntar_parlants(fitxers):
    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster

    claus, vectors, torns_bruts, errors = [], [], [], []
    for cami in sorted(fitxers):
        d = llegir_json(cami, {})
        if d.get("error"):
            errors.append("tros %s: %s" % (d.get("i"), d["error"][:80]))
        for etiqueta, v in (d.get("empremtes") or {}).items():
            claus.append((d.get("i"), etiqueta))
            vectors.append(v)
        for t in d.get("torns") or []:
            torns_bruts.append((d.get("i"), t))

    if not vectors:
        return {"torns": [], "error": "cap tros amb veus identificades" + ("; " + "; ".join(errors) if errors else "")}

    X = np.array(vectors, dtype=float)
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    if len(X) == 1:
        grups = [1]
    else:
        Z = linkage(X, method="centroid", metric="euclidean")
        if PARLANTS.isdigit() and int(PARLANTS) > 0:
            grups = fcluster(Z, t=int(PARLANTS), criterion="maxclust")
        else:
            grups = fcluster(Z, t=LLINDAR_VEUS, criterion="distance")
    global_de = {claus[k]: "V%02d" % int(grups[k]) for k in range(len(claus))}

    torns = []
    for i, t in torns_bruts:
        g = global_de.get((i, t["p"]))
        if g:
            torns.append({"s": t["s"], "e": t["e"], "p": g})
    torns.sort(key=lambda t: t["s"])
    print("Parlants per trossos: %d veus locals -> %d persones" % (len(claus), len(set(global_de.values()))))
    return {"torns": torns, "error": ("; ".join(errors) if errors and not torns else "")}


# -----------------------------------------------------------------------------
# CORRECTOR (LanguageTool, el motor del corrector de Softcatalà)
# -----------------------------------------------------------------------------
LT_PORT = 8081
LT_PROTEGITS = ["Bisbal", "Corçà", "Cruïlles", "Monells", "Forallac", "Vulpellac", "Peratallada",
                "Canapost", "Casavells", "Gavarres", "Daró", "Empordà", "Sadurní", "Heura"]


def engegar_corrector():
    jars = glob.glob(os.path.expanduser("~/languagetool/*/languagetool-server.jar"))
    if not jars:
        print("Corrector: no hi és.")
        return None
    proc = subprocess.Popen(["java", "-cp", jars[0], "org.languagetool.server.HTTPServer", "--port", str(LT_PORT),
                             "--allow-origin", "*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        try:
            urllib.request.urlopen("http://localhost:%d/v2/languages" % LT_PORT, timeout=2)
            return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    return None


def corregir(text):
    """Corregeix un paràgraf: només canvis segurs (una sola proposta, no noms propis)."""
    if not text.strip():
        return text, 0
    try:
        dades = urllib.parse.urlencode({"text": text, "language": "ca-ES",
                                        "disabledRules": "UPPERCASE_SENTENCE_START,WHITESPACE_RULE"}).encode()
        with urllib.request.urlopen("http://localhost:%d/v2/check" % LT_PORT, data=dades, timeout=60) as r:
            coincidencies = json.loads(r.read()).get("matches", [])
    except Exception as e:
        print("Corrector:", e)
        return text, 0
    canvis = 0
    for m in sorted(coincidencies, key=lambda x: x["offset"], reverse=True):
        propostes = m.get("replacements") or []
        original = text[m["offset"]:m["offset"] + m["length"]]
        if len(propostes) != 1 or not original.strip():
            continue
        if original[:1].isupper() and m.get("rule", {}).get("issueType") == "misspelling":
            continue
        if any(p.lower() in original.lower() for p in LT_PROTEGITS):
            continue
        text = text[:m["offset"]] + propostes[0]["value"] + text[m["offset"] + m["length"]:]
        canvis += 1
    return text, canvis


# -----------------------------------------------------------------------------
# RESUM PER PUNTS AMB GEMINI (canvi 10.10.2026)
# -----------------------------------------------------------------------------
def text_ordre_del_dia():
    """Text de l'ordre del dia ("" si no n'hi ha)."""
    if not ORDRE or ORDRE.lower() == "resum":
        return ""
    if not ORDRE.startswith("pdf:"):
        return ORDRE
    r = tg("getFile", {"file_id": ORDRE[4:]})
    cami = (r.get("result") or {}).get("file_path")
    if not cami:
        raise RuntimeError("Telegram no deixa descarregar el PDF de l'ordre del dia.")
    pdf = os.path.join(TREBALL, "ordre.pdf")
    urllib.request.urlretrieve("https://api.telegram.org/file/bot" + TOKEN + "/" + cami, pdf)
    p = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True)
    text = re.sub(r"[ \t]+", " ", p.stdout or "").strip()
    if len(text) < 30:
        raise RuntimeError("No he pogut llegir el text del PDF de l'ordre del dia (potser és una imatge escanejada). "
                           "Enganxa'l com a text al missatge, després de «Ordre del dia:».")
    return text


def text_consistori(ordre_text):
    """Composició del consistori que toca, o "" si no hi ha el fitxer."""
    on = (TITOL + " " + ordre_text[:2000]).lower()
    nom = CONSISTORI_PER_DEFECTE
    for paraula, fitxer in CONSISTORIS:
        if paraula in on:
            nom = fitxer
            break
    cami = os.path.join("consistoris", nom + ".txt")
    try:
        with open(cami, encoding="utf-8") as f:
            linies = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except OSError:
        print("Consistori: no hi ha el fitxer " + cami)
        return ""
    return "\n".join(linies)


# -----------------------------------------------------------------------------
# NOMS DEL CONSISTORI (canvi 11.10.2026)
# Quan a la transcripció surt «senyor Vidal», «senyora Somalla» o «senyor Gaetan
# Xapel», es corregeix el cognom i s'hi afegeix el nom sencer entre claudàtors:
# «senyor Vidal [Francesc Vidal Rodríguez]», «senyora Sumalla [Neus Sumalla
# Saballs]», «senyor Gaëtan Chapel». Només es toca darrere de senyor, senyora,
# regidor, regidora, alcalde, alcaldessa o tinent/a d'alcalde, i només si
# s'assembla clarament a algú del consistori.
# -----------------------------------------------------------------------------
import difflib, unicodedata

TRACTAMENTS = r"(?:[Ss]enyor(?:a)?|[Ss]r\.?|[Ss]ra\.?|[Rr]egidor(?:a)?|[Aa]lcalde(?:ssa)?|[Tt]inent(?:a)? d'alcalde|[Cc]ompany(?:a)?)"
ROLS_FORA_CONSISTORI = ("secretari", "secretària", "interventor", "interventora", "tresorer", "tresorera", "tècnic", "tècnica")


def fonetic(paraula):
    t = unicodedata.normalize("NFD", paraula.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-zç]", "", t)
    for a, b in (("tx", "x"), ("ch", "x"), ("ç", "s"), ("ll", "l"), ("ss", "s"), ("v", "b"), ("y", "i"), ("h", "")):
        t = t.replace(a, b)
    return t


def persones_consistori(text):
    """[{nom, paraules, dona, alies}] a partir de les línies «nom | càrrec | grup | àlies»."""
    persones = []
    for linia in text.splitlines():
        if linia.strip().startswith("#"):
            continue
        camps = [c.strip() for c in linia.split("|")]
        if len(camps) < 2 or not camps[0]:
            continue
        carrec = camps[1].lower()
        dona = bool(re.search(r"\b(regidora|tinenta|alcaldessa|presidenta)\b", carrec))
        alies = [a.strip() for a in camps[3].split(",")] if len(camps) > 3 and camps[3] else []
        persones.append({"nom": camps[0], "paraules": camps[0].split(), "dona": dona, "alies": alies})
    return persones


def millor_persona(paraules_dites, persones, dona=None):
    """Busca a qui correspon «Vidal», «Gaetan Xapel», «Ben Saïd»... Retorna (persona, n_paraules) o (None, 0)."""
    candidats = []
    for n in (2, 1):                       # primer, dues paraules seguides (Gaetan Xapel, Ben Saïd)
        if len(paraules_dites) < n:
            continue
        dit = fonetic("".join(paraules_dites[:n]))
        if len(dit) < 4:
            continue
        for p in persones:
            if dona is not None and p["dona"] != dona:
                continue
            opcions = []
            for k, w in enumerate(p["paraules"]):
                opcions.append((fonetic(w), k))                                       # una paraula del nom
                if k + 1 < len(p["paraules"]):
                    opcions.append((fonetic(w + p["paraules"][k + 1]), 1))            # dues paraules seguides
            opcions += [(fonetic(a), 1) for a in p["alies"]]
            for forma, posicio in opcions:
                r = difflib.SequenceMatcher(None, dit, forma).ratio()
                if r >= 0.8:
                    # «Senyor Martí» és més sovint el primer cognom que no pas el nom de pila o el segon cognom.
                    bonus = 0 if posicio == 0 else (0.05 if posicio == 1 else 0.03)
                    candidats.append((r + bonus + (0.05 if n == 2 else 0), p, n))
        if candidats:
            break
    if not candidats:
        return None, 0
    candidats.sort(key=lambda c: -c[0])
    millor = candidats[0]
    # Ambigu: dues persones diferents amb la mateixa puntuació.
    if any(c[1] is not millor[1] and abs(c[0] - millor[0]) < 0.015 for c in candidats[1:]):
        return None, 0
    return millor[1], millor[2]


def persona_per_nom_complet(nom, persones):
    """Per als noms que dona Gemini: totes les paraules han de ser del nom d'una persona de la llista
    («Òscar Aparicio», «Gaetan Xapel»), i almenys una ha de ser un cognom. Si no, None."""
    tot = fonetic(nom)
    for p in persones:
        if tot == fonetic(p["nom"]) or any(len(fonetic(a)) >= 5 and fonetic(a) in tot for a in p["alies"]):
            return p
    dites = [fonetic(w) for w in nom.split() if len(fonetic(w)) >= 3]
    if not dites:
        return None
    millor, punts_millor = None, 0
    for p in persones:
        propies = [fonetic(w) for w in p["paraules"]]
        trobades, cognom = 0, False
        for d in dites:
            ratios = [difflib.SequenceMatcher(None, d, w).ratio() for w in propies]
            k = max(range(len(ratios)), key=lambda i: ratios[i])
            if ratios[k] >= 0.8:
                trobades += 1
                cognom = cognom or k > 0
        if trobades == len(dites) and cognom and trobades > punts_millor:
            millor, punts_millor = p, trobades
    return millor


def corregir_noms(text, persones):
    if not persones:
        return text, 0
    canvis = [0]
    patro = re.compile(r"\b(" + TRACTAMENTS + r")(\s+)((?:d['’]|de\s+)?)((?:[A-ZÀ-Ý][\w'’·À-ÿ-]*)(?:\s+[A-ZÀ-Ý][\w'’·À-ÿ-]*){0,2})")

    def substitueix(m):
        tractament, espai, prep, nom_dit = m.group(1), m.group(2), m.group(3), m.group(4)
        if "[" in text[m.end():m.end() + 2]:
            return m.group(0)                                   # ja corregit
        t = tractament.lower()
        dona = True if re.search(r"(senyora|sra|regidora|alcaldessa|tinenta|companya)", t) else \
            (False if re.search(r"(senyor|sr|regidor|alcalde|tinent|company)", t) else None)
        paraules = nom_dit.split()
        persona, n = millor_persona(paraules, persones, dona)
        if not persona:
            return m.group(0)
        dites = paraules[:n]
        resta = " ".join(paraules[n:])
        # Forma correcta de les paraules dites (Xapel -> Chapel, Somalla -> Sumalla).
        correctes = []
        for w in dites:
            fw = fonetic(w)
            bona = max(persona["paraules"], key=lambda x: difflib.SequenceMatcher(None, fw, fonetic(x)).ratio())
            correctes.append(bona if difflib.SequenceMatcher(None, fw, fonetic(bona)).ratio() >= 0.8 else w)
        if n == 2 and len(correctes) == 2 and fonetic("".join(dites)) in [fonetic(a) for a in persona["alies"]] + \
                [fonetic(persona["paraules"][k] + persona["paraules"][k + 1]) for k in range(len(persona["paraules"]) - 1)]:
            # «Ben Saïd» -> «Bensahid»: dues paraules dites que són una del nom
            unides = [w for w in persona["paraules"] if fonetic(w) == fonetic("".join(dites))]
            if unides:
                correctes = unides
        nou = " ".join(correctes)
        if nou != persona["nom"]:
            nou += " [" + persona["nom"] + "]"
        canvis[0] += 1
        return tractament + espai + prep + nou + ((" " + resta) if resta else "")

    nou_text = patro.sub(substitueix, text)
    return nou_text, canvis[0]


def gemini(prompt, json_mode=False):
    import requests
    errors = []
    for model in GEMINI_MODELS:
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent"
        cos = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768}}
        if json_mode:
            cos["generationConfig"]["responseMimeType"] = "application/json"
        for intent in range(3):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                              json=cos, timeout=600)
            if r.status_code in (429, 500, 503) and intent < 2:
                print("Gemini %s: HTTP %s, es torna a provar d'aquí a 60 s" % (model, r.status_code))
                time.sleep(60)
                continue
            break
        if r.status_code != 200:
            errors.append("%s: HTTP %s %s" % (model, r.status_code, r.text[:200]))
            continue
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if text:
            print("Gemini: resposta de %s (%d caràcters)" % (model, len(text)))
            return text
        errors.append(model + ": resposta buida")
    raise RuntimeError("; ".join(errors)[:400])


def identificar_parlants(blocs_transcripcio, etiquetes):
    """Canvi 10.10.2026: diu qui és cada «Parlant N» (a partir de la composició del
    consistori i del que es diu al ple). Retorna {"Parlant 1": {"nom": ..., "detall": ...}}
    només amb els que Gemini dona per segurs."""
    if not GEMINI_KEY:
        raise RuntimeError("falta el secret GEMINI_API_KEY al repositori")

    consistori = text_consistori(text_ordre_del_dia() if ORDRE.startswith("pdf:") else ORDRE)
    prompt = (
        "Tens la transcripció automàtica d'un ple municipal. Una eina automàtica ha separat les veus i les ha "
        "numerat (" + ", ".join(etiquetes) + "). Digues quina persona real és cada etiqueta.\n\n"
        + ("COMPOSICIÓ DEL CONSISTORI (nom | càrrec | grup):\n" + consistori + "\n\n" if consistori else "")
        + "Fes-ho NOMÉS amb proves clares de la transcripció: l'alcalde o l'alcaldessa dona la paraula a algú pel "
        "nom i la intervenció següent és d'aquella etiqueta; algú es presenta o diu de quin grup és; algú respon una "
        "pregunta adreçada a una persona concreta; qui presideix i dona la paraula és l'alcalde. El nom pot estar mal "
        "transcrit («Xapel» per Chapel): fes servir la forma correcta de la composició del consistori. Una persona que "
        "no sigui del consistori (secretari, interventor, públic) també es pot identificar pel càrrec.\n"
        "Fes servir NOMÉS els noms de la composició del consistori, escrits exactament com hi surten. No hi afegeixis "
        "ningú que no hi sigui: si una veu no és de cap regidor, només la pots identificar pel càrrec (Secretari, "
        "Interventor...) si es diu a la transcripció; si no, segur: false.\n"
        "Si una etiqueta sembla que barreja persones diferents, o no n'estàs segur, posa segur: false.\n\n"
        "Respon NOMÉS amb un objecte JSON amb aquesta forma, amb totes les etiquetes:\n"
        '{"Parlant 1": {"nom": "Nom i cognoms", "detall": "càrrec o grup", "segur": true, "prova": "frase curta"}}\n\n'
        "TRANSCRIPCIÓ:\n" + "\n\n".join(blocs_transcripcio)
    )
    persones = persones_consistori(consistori)
    resposta = gemini(prompt, json_mode=True)
    resposta = re.sub(r"^```(?:json)?|```$", "", resposta.strip()).strip()
    dades = json.loads(resposta)

    resultat = {}
    for etiqueta, d in (dades or {}).items():
        if isinstance(d, dict) and d.get("segur") is True and str(d.get("nom") or "").strip():
            nom = str(d["nom"]).strip()
            # Canvi 11.10.2026: només noms de la llista (escrits com a la llista) o un càrrec de fora del consistori.
            persona = persona_per_nom_complet(nom, persones)
            if persona:
                nom = persona["nom"]
            elif persones and not any(r in nom.lower() for r in ROLS_FORA_CONSISTORI):
                print("%s: «%s» no és a la llista del consistori; no es fa servir" % (etiqueta, nom))
                continue
            resultat[etiqueta] = {"nom": nom[:80], "detall": str(d.get("detall") or "").strip()[:120]}
        print("%s -> %s" % (etiqueta, json.dumps(d, ensure_ascii=False)[:300]))
    return resultat


def resumir(blocs_transcripcio):
    if not GEMINI_KEY:
        raise RuntimeError("falta el secret GEMINI_API_KEY al repositori")

    ordre_text = text_ordre_del_dia()
    consistori = text_consistori(ordre_text)
    transcripcio = "\n\n".join(blocs_transcripcio)

    prompt = (
        "Ets redactor de La TdG, un mitjà local de la Bisbal d'Empordà. Tens la transcripció automàtica d'una "
        "sessió plenària municipal. La transcripció pot tenir errors, i els parlants surten numerats (Parlant 1, "
        "Parlant 2...) per una eina automàtica que de vegades s'equivoca o ajunta persones diferents.\n\n"
        + ("COMPOSICIÓ DEL CONSISTORI (nom | càrrec | grup):\n" + consistori + "\n\n" if consistori else "")
        + ("ORDRE DEL DIA:\n" + ordre_text + "\n\n" if ordre_text else
           "No tens l'ordre del dia: dedueix els punts a partir de la transcripció.\n\n")
        + "FES AIXÒ:\n"
        "1. QUI PARLA. Digues quina persona és cada «Parlant N». Fes-ho només amb proves clares de la transcripció: "
        "quan l'alcalde dona la paraula a algú pel nom, quan algú es presenta o diu de quin grup és, o quan algú "
        "respon una pregunta adreçada a una persona concreta. Si no n'estàs segur, escriu «no identificat». Mai no "
        "ho inventis.\n"
        "2. PUNTS. Per a cada punt de l'ordre del dia, en ordre:\n"
        "- número i títol del punt;\n"
        "- hora d'inici a la transcripció [hh:mm:ss];\n"
        "- resum de 2 a 4 frases: què s'hi aprova o s'hi debat i per què;\n"
        "- intervencions: una línia per persona (nom i grup) amb la seva posició en una frase;\n"
        "- votació: el resultat i què ha votat cada grup, només si es diu a la gravació; si no es diu, escriu "
        "«Votació: no es diu a la gravació».\n"
        "Els punts de tràmit (aprovació de l'acta, donar compte de decrets) es poden resumir en una línia.\n"
        "3. Al final, «TITULARS POSSIBLES»: de 3 a 5 titulars informatius i curts amb el que és més notícia del ple.\n\n"
        "NOMS: cada persona del consistori, amb el nom sencer tal com surt a la composició (si a la transcripció diu "
        "«el senyor Vidal», escriu Francesc Vidal Rodríguez). Si el nom surt mal transcrit, fes servir el de la llista. "
        "No hi posis ningú que no sigui a la llista, llevat del secretari o l'interventor pel càrrec.\n"
        "NORMES: català normatiu i to periodístic neutre. No hi afegeixis res que no surti a la transcripció. "
        "Escriu els imports, les xifres i els noms tal com surten. Text pla, sense markdown (ni asteriscs ni "
        "coixinets). Separa cada bloc (qui parla, cada punt i els titulars) amb una línia que només contingui ———.\n\n"
        "TRANSCRIPCIÓ:\n" + transcripcio
    )

    resposta = gemini(prompt)
    resposta = re.sub(r"\*\*|__|^#+\s*", "", resposta, flags=re.M)
    trossos = [t.strip() for t in re.split(r"^\s*[—–\-_=]{3,}\s*$", resposta, flags=re.M) if t.strip()]

    capcalera = ("🧾 RESUM PER PUNTS — " + (TITOL or "Ple") + "\n"
                 "Fet amb Gemini a partir de la transcripció automàtica" +
                 (" i l'ordre del dia" if ordre_text else " (sense ordre del dia)") +
                 ". Revisa'l abans de publicar-lo.")
    enviar_en_missatges([capcalera] + trossos)


# -----------------------------------------------------------------------------
# 4. UNIR, CORREGIR I ENVIAR
# -----------------------------------------------------------------------------
def parlant_de(mig, torns, anterior):
    for t in torns:
        if t["s"] <= mig <= t["e"]:
            return t["p"]
    propers = [t for t in torns if abs(t["s"] - mig) <= 1.5 or abs(t["e"] - mig) <= 1.5]
    if propers:
        return min(propers, key=lambda t: min(abs(t["s"] - mig), abs(t["e"] - mig)))["p"]
    return anterior


def unir():
    info = llegir_json(os.path.join(TREBALL, "info.json"), {})
    durada = info.get("durada", 0)
    paraules = []
    falten = []
    for t in info.get("trams", []):
        llista = llegir_json(os.path.join(TREBALL, "paraules_%02d.json" % t["i"]), None)
        if llista is None:
            falten.append(t["i"])
        else:
            paraules.extend(llista)
    paraules.sort(key=lambda w: w["s"])
    if not paraules:
        raise RuntimeError("No s'ha pogut transcriure cap tros (o l'àudio no té veu).")

    diar = llegir_json(os.path.join(TREBALL, "parlants.json"), {"torns": [], "error": "no hi ha resultat"})
    # Canvi 11.10.2026: si s'ha fet per trossos, s'ajunten aquí.
    fitxers_trossos = glob.glob(os.path.join(TREBALL, "parlants_[0-9][0-9].json"))
    if fitxers_trossos and not diar.get("torns"):
        estat("✅ Transcripció feta. ⏳ Ajuntant les veus dels trossos...")
        try:
            diar = ajuntar_parlants(fitxers_trossos)
        except Exception as e:
            print("::warning::No s'han pogut ajuntar les veus: %s" % e)
            diar = {"torns": [], "error": "no s'han pogut ajuntar les veus dels trossos (" + str(e)[:120] + ")"}
    torns = diar.get("torns", [])

    # Paràgrafs: canvi de parlant o pausa llarga.
    noms = {}
    paragrafs = []
    anterior = None
    for w in paraules:
        p = parlant_de((w["s"] + w["e"]) / 2, torns, anterior) if torns else None
        if p is not None and p not in noms:
            noms[p] = "Parlant " + str(len(noms) + 1)
        darrer = paragrafs[-1] if paragrafs else None
        if darrer and darrer["p"] == p and w["s"] - darrer["e"] < (4 if torns else 2.5):
            darrer["text"] += w["w"]
            darrer["e"] = w["e"]
        else:
            paragrafs.append({"p": p, "s": w["s"], "e": w["e"], "text": w["w"]})
        anterior = p

    estat("✅ Transcripció feta. ⏳ Engegant el corrector de Softcatalà...")
    corrector = engegar_corrector()
    if not corrector:
        estat("✅ Transcripció feta. ⚠️ El corrector no s'ha pogut engegar: la transcripció sortirà sense corregir.")
    correccions = 0
    darrer_avis = time.time()
    for n, par in enumerate(paragrafs):
        par["text"] = re.sub(r"\s+", " ", par["text"]).strip()
        if par["text"]:
            par["text"] = par["text"][0].upper() + par["text"][1:]
        if corrector:
            par["text"], c = corregir(par["text"])
            correccions += c
            # Canvi 10.10.2026: percentatge del corrector, cada minut.
            if time.time() - darrer_avis > 60:
                estat("✅ Transcripció feta. ✏️ Passant el corrector: %d %% (%d correccions fins ara)" %
                      (int((n + 1) * 100 / len(paragrafs)), correccions))
                darrer_avis = time.time()
    if corrector:
        corrector.kill()

    # Canvi 11.10.2026: noms del consistori ben escrits i sencers.
    persones = persones_consistori(text_consistori(ORDRE if not ORDRE.startswith("pdf:") else ""))
    noms_corregits = 0
    for par in paragrafs:
        if par["text"]:
            par["text"], c = corregir_noms(par["text"], persones)
            noms_corregits += c
    print("Noms del consistori: %d mencions completades" % noms_corregits)

    # Canvi 04.10.2026: la transcripció va dins dels missatges (sense fitxers).
    capcalera = ["📝 " + (TITOL or info.get("nom") or "Transcripció"),
                 "Durada: " + durada_text(durada) + " · Model: " + NOMS_MODEL.get(MODEL, MODEL) +
                 (" · " + str(len(noms)) + " parlants" if noms else ""),
                 "Transcripció automàtica: cal revisar-la abans de citar-la."]
    if MODEL == "aina":
        capcalera.append("El model d'Aina no posa signes de puntuació.")
    if not torns:
        capcalera.append("No s'ha pogut identificar qui parla (" + (diar.get("error") or "sense dades") + ").")
    if falten:
        capcalera.append("⚠️ Falten els trossos " + ", ".join(str(i + 1) for i in falten) + " (no s'han pogut transcriure).")

    def muntar_blocs(etiquetes_parlants):
        llista = []
        for par in paragrafs:
            if par["text"]:
                llista.append("[" + hms(par["s"]) + "]" +
                              (" " + etiquetes_parlants[par["p"]] + ":" if par["p"] in etiquetes_parlants else "") +
                              "\n" + par["text"])
        return llista

    cos = muntar_blocs(noms)

    # Canvi 10.10.2026: noms reals dels parlants (Gemini), abans d'enviar la transcripció.
    identificats = {}
    if noms and GEMINI_KEY:
        estat("✅ Transcripció i correcció fetes. ⏳ Identificant qui és cada parlant amb Gemini...")
        try:
            identificats = identificar_parlants(cos, sorted(noms.values(), key=lambda x: int(x.split()[-1])))
        except Exception as e:
            print("::warning::Identificació de parlants: %s" % e)
            capcalera.append("⚠️ No s'han pogut posar els noms dels parlants (" + str(e)[:150] + ").")

    if identificats:
        noms_finals = {}
        for etiqueta_pyannote, parlant in noms.items():
            noms_finals[etiqueta_pyannote] = identificats[parlant]["nom"] if parlant in identificats else parlant
        cos = muntar_blocs(noms_finals)
        llegenda = ["🗣️ Qui és qui (identificat amb Gemini pel que es diu al ple; revisa-ho):"]
        for parlant in sorted(noms.values(), key=lambda x: int(x.split()[-1])):
            if parlant in identificats:
                d = identificats[parlant]
                llegenda.append("• " + d["nom"] + (" — " + d["detall"] if d["detall"] else "") + " (" + parlant + ")")
            else:
                llegenda.append("• " + parlant + ": no identificat")
        capcalera.append("")
        capcalera.extend(llegenda)

    blocs = ["\n".join(capcalera)] + cos

    resum = ("✅ Fet: " + durada_text(durada) + " · " + NOMS_MODEL.get(MODEL, MODEL) +
             (" · " + str(len(noms)) + " parlants" if noms else " · sense identificar parlants") +
             (" (" + str(len(identificats)) + " amb nom)" if identificats else "") +
             " · ✏️ " + (str(correccions) + " correccions" if corrector else "corrector no disponible") +
             ("\n⚠️ Falten trossos: " + ", ".join(str(i + 1) for i in falten) if falten else ""))
    estat(resum)
    enviar_en_missatges(blocs)

    # Resum per punts (canvi 10.10.2026). Si falla, la transcripció ja s'ha enviat.
    if ORDRE:
        estat(resum + "\n⏳ Preparant el resum per punts amb Gemini...")
        try:
            resumir(blocs[1:])
            estat(resum + "\n🧾 Resum per punts enviat.")
        except Exception as e:
            print("::warning::Resum per punts: %s" % e)
            enviar_text("⚠️ No he pogut fer el resum per punts: " + str(e)[:500])
            estat(resum + "\n⚠️ Sense resum per punts.")


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    pas = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if pas == "preparar":
            preparar()
        elif pas == "transcriure":
            transcriure(int(sys.argv[2]))
        elif pas == "parlants":
            parlants()
        elif pas == "parlants_tros":
            parlants_tros(int(sys.argv[2]))
        elif pas == "seguiment":
            seguiment()
        elif pas == "unir":
            unir()
        elif pas == "error":
            estat("❌ No s'ha pogut acabar la transcripció. Mira el registre a GitHub (Actions > Plens).")
        else:
            raise SystemExit("Pas desconegut: " + pas)
    except Exception as e:
        if pas in ("preparar", "unir"):
            estat("❌ " + str(e))
        raise
