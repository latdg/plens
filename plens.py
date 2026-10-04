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

    # Fitxers de sortida
    base = net(TITOL or info.get("nom") or NOM)
    os.makedirs("sortida", exist_ok=True)
    txt = os.path.join("sortida", base + ".txt")
    srt = os.path.join("sortida", base + ".srt")

    capcalera = [TITOL or info.get("nom") or "Transcripció",
                 "Durada: " + durada_text(durada) + " · Model: " + NOMS_MODEL.get(MODEL, MODEL) +
                 (" · " + str(len(noms)) + " parlants" if noms else ""),
                 "Transcripció automàtica de La TdG: cal revisar-la abans de citar-la."]
    if MODEL == "aina":
        capcalera.append("El model d'Aina no posa signes de puntuació.")
    if not torns:
        capcalera.append("No s'ha pogut identificar qui parla (" + (diar.get("error") or "sense dades") + ").")
    if falten:
        capcalera.append("⚠️ Falten els trossos " + ", ".join(str(i + 1) for i in falten) + " (no s'han pogut transcriure).")

    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(capcalera) + "\n\n")
        for par in paragrafs:
            if not par["text"]:
                continue
            f.write("[" + hms(par["s"]) + "]" + (" " + noms[par["p"]] + ":" if par["p"] in noms else "") +
                    "\n" + par["text"] + "\n\n")

    # Subtítols: blocs de com a molt 7 s.
    def tc(s):
        return "%02d:%02d:%02d,%03d" % (int(s) // 3600, int(s) % 3600 // 60, int(s) % 60, int((s - int(s)) * 1000))
    with open(srt, "w", encoding="utf-8") as f:
        n = 0
        bloc = None
        for w in paraules + [None]:
            if bloc and (w is None or w["e"] - bloc["s"] > 7):
                n += 1
                f.write("%d\n%s --> %s\n%s\n\n" % (n, tc(bloc["s"]), tc(bloc["e"]), re.sub(r"\s+", " ", bloc["t"]).strip()))
                bloc = None
            if w is None:
                break
            if not bloc:
                bloc = {"s": w["s"], "e": w["e"], "t": ""}
            bloc["t"] += w["w"]
            bloc["e"] = w["e"]

    resum = ("✅ Fet: " + durada_text(durada) + " · " + NOMS_MODEL.get(MODEL, MODEL) +
             (" · " + str(len(noms)) + " parlants" if noms else " · sense identificar parlants") +
             " · ✏️ " + (str(correccions) + " correccions" if corrector else "corrector no disponible") +
             ("\n⚠️ Falten trossos: " + ", ".join(str(i + 1) for i in falten) if falten else ""))
    enviar_document(txt, "📝 " + (TITOL or "Transcripció") + "\nRevisa-la abans de citar-la. Els parlants surten numerats (Parlant 1, 2...).")
    enviar_document(srt, "🎬 Subtítols (.srt)")
    estat(resum)


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
