# =============================================================================
# BOT DE PLENS I ÀUDIOS LLARGS — La TdG · 04.10.2026
#
# S'executa a GitHub Actions (repositori latdg/plens, workflow plens.yml) en
# quatre passos:
#   python3 plens.py preparar       descarrega l'àudio i el talla en trossos
#   python3 plens.py transcriure N  transcriu el tros N (els trossos van en paral·lel)
#   python3 plens.py parlants       identifica qui parla (pyannote, com WhisperX)
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


def gemini(prompt):
    import requests
    errors = []
    for model in GEMINI_MODELS:
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent"
        cos = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768}}
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

    estat("⏳ Passant el corrector ortogràfic...")
    corrector = engegar_corrector()
    correccions = 0
    for par in paragrafs:
        par["text"] = re.sub(r"\s+", " ", par["text"]).strip()
        if par["text"]:
            par["text"] = par["text"][0].upper() + par["text"][1:]
        if corrector:
            par["text"], c = corregir(par["text"])
            correccions += c
    if corrector:
        corrector.kill()

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

    blocs = ["\n".join(capcalera)]
    for par in paragrafs:
        if par["text"]:
            blocs.append("[" + hms(par["s"]) + "]" + (" " + noms[par["p"]] + ":" if par["p"] in noms else "") +
                         "\n" + par["text"])

    resum = ("✅ Fet: " + durada_text(durada) + " · " + NOMS_MODEL.get(MODEL, MODEL) +
             (" · " + str(len(noms)) + " parlants" if noms else " · sense identificar parlants") +
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
