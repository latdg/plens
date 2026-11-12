# =============================================================================
# PREGUNTES SOBRE UNA TRANSCRIPCIÓ — bot de plens de La TdG · 12.11.2026
#
# El llança el Worker latdg-plens (workflow preguntar.yml) quan respons al Word
# d'una transcripció (o a un dels missatges de la transcripció) amb una pregunta.
# Llegeix el text del Word, en pregunta a Gemini (la mateixa clau i els mateixos
# models que el resum dels plens, plens.gemini) i escriu la resposta dins del
# missatge «💬 ...» que ha deixat el Worker. Amb ANTERIORS, té en compte la conversa
# (les preguntes i, si hi respons, la resposta anterior), com a Plaud.
# =============================================================================
import html, os, re, sys, time, traceback, urllib.request, zipfile, io

import plens   # tg(), gemini() i la configuració de Gemini

CHAT = os.environ.get("CHAT_ID", "").strip()
MISSATGE = os.environ.get("MISSATGE_ID", "").strip()   # la pregunta
ESTAT = os.environ.get("ESTAT_ID", "").strip()         # missatge «💬 ...» del Worker
FILE_ID = os.environ.get("FILE_ID", "").strip()        # el Word (si es respon al Word)
CONTEXT = os.environ.get("CONTEXT", "").strip()        # el text (si es respon a un missatge)
PREGUNTA = os.environ.get("PREGUNTA", "").strip()
ANTERIORS = os.environ.get("ANTERIORS", "").strip()     # context de la conversa (preguntes i resposta anteriors)

INSTRUCCIONS = (
    "Ets l'assistent de la redacció de La TdG, un mitjà local de la Bisbal d'Empordà. Et donen la transcripció "
    "automàtica d'un àudio (un ple municipal o un altre àudio llarg; pot tenir errades de reconeixement) i una "
    "pregunta sobre aquest àudio. Respon NOMÉS a partir del document; si la resposta no hi és, digues-ho clarament "
    "i no t'inventis res. Respon en català (o en l'idioma de la pregunta, si és un altre), de manera clara i directa. "
    "Sempre que situïs o citis alguna cosa, indica entre claudàtors la marca de temps d'on surt ([hh:mm:ss], o de "
    "quin minut a quin minut, [hh:mm:ss–hh:mm:ss], si és un fragment) i qui ho diu. Les cites literals, entre "
    "cometes baixes («»). No facis servir format Markdown: ni asteriscs, ni coixinets, ni taules. Per a llistes, "
    "fes servir guions."
)


def edita(text):
    if ESTAT:
        plens.tg("editMessageText", {"chat_id": CHAT, "message_id": ESTAT, "text": text[:4096],
                                     "disable_web_page_preview": "true"})
    else:
        envia(text)


def envia(text):
    d = {"chat_id": CHAT, "text": text[:4096], "disable_web_page_preview": "true"}
    if MISSATGE:
        d["reply_to_message_id"] = MISSATGE
        d["allow_sending_without_reply"] = "true"
    plens.tg("sendMessage", d)


def text_del_word(dades):
    """Text d'un .docx, paràgraf a paràgraf (sense biblioteques)."""
    with zipfile.ZipFile(io.BytesIO(dades)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    xml = re.sub(r"<w:br\s*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def document():
    if not FILE_ID:
        return CONTEXT
    r = plens.tg("getFile", {"file_id": FILE_ID})
    cami = (r.get("result") or {}).get("file_path")
    if not cami:
        raise RuntimeError("no he pogut obrir el document (Telegram no el deixa descarregar)")
    with urllib.request.urlopen("https://api.telegram.org/file/bot" + plens.TOKEN + "/" + cami, timeout=120) as f:
        dades = f.read()
    if cami.lower().endswith(".docx") or dades[:2] == b"PK":
        return text_del_word(dades)
    return dades.decode("utf-8", "replace")


def trossos(text, maxim=4000):
    parts = []
    while len(text) > maxim:
        tall = text.rfind("\n", 0, maxim)
        if tall < maxim // 2:
            tall = text.rfind(" ", 0, maxim)
        if tall < maxim // 2:
            tall = maxim
        parts.append(text[:tall].strip())
        text = text[tall:].strip()
    if text:
        parts.append(text)
    return parts


def principal():
    if not plens.GEMINI_KEY:
        raise RuntimeError("falta el secret GEMINI_API_KEY al repositori plens")
    if not PREGUNTA:
        raise RuntimeError("no hi ha cap pregunta")

    text = document()
    if len(text) < 20:
        raise RuntimeError("el document és buit o no s'hi ha trobat text")
    print("Document: %d caràcters · Pregunta: %s" % (len(text), PREGUNTA))

    edita("💬 Pensant la resposta...")
    prompt = (INSTRUCCIONS + "\n\nDOCUMENT:\n\n" + text + "\n\n\n" +
              ((ANTERIORS + "\n\n") if ANTERIORS else "") + "PREGUNTA: " + PREGUNTA)
    resposta = plens.gemini(prompt)
    resposta = re.sub(r"\*\*", "", resposta)
    resposta = re.sub(r"^#+\s*", "", resposta, flags=re.M).strip()

    parts = trossos("💬 " + resposta)
    edita(parts[0])
    for p in parts[1:]:
        time.sleep(1)
        envia(p)


try:
    principal()
except Exception as e:
    traceback.print_exc()
    edita("💬 ❌ No he pogut respondre: " + str(e))
    sys.exit(1)
