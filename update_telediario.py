import urllib.request
import urllib.error
import json
import re
import html
import os
import sys
import time

# Clé API Gemini (récupérée de manière sécurisée depuis les variables d'environnement / GitHub Secrets)
API_KEY = os.environ.get("GEMINI_API_KEY", "")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "telediarios.json")
OUTPUT_JS_FILE = os.path.join(DATA_DIR, "telediarios.js")

# Modèles Gemini officiels et ultra-rapides testés avec succès
GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.8-flash"
]

# Timeouts et tentatives de retry
RTVE_TIMEOUT = 20
GEMINI_TIMEOUT = 45
MAX_RETRIES_PER_MODEL = 2

INVALID_SUMMARY_MARKERS = (
    "Résumé en cours de traitement",
    "Journal très récent",
    "transcription en attente",
    "Résumé non disponible"
)

# Éditions clés connues : "major" (rouge, très rare) ou "important" (bleu, modéré)
# Chaque intitulé est strictement unique.
KNOWN_IMPORTANCE = {
    "17287313": ("major", "Collision ferroviaire à Urda"),
    "17288730": ("important", "Bilan de 69 morts à Urda"),
    "17297902": ("important", "Plan de sécurité ferroviaire"),
    "17299811": ("major", "Démission d'Óscar Puente"),
    "17300630": ("important", "Carlos Cuerpo aux Transports"),
    "17301584": ("important", "Perquisitions UCO (affaire Koldo)"),
}


def http_get_text(url, timeout=RTVE_TIMEOUT, retries=3):
    """Requête HTTP GET robuste avec tentatives de relance (retry) automatiques."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/json,text/vtt,*/*"
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise last_err


def clean_asr_typos(text):
    """Corrige les coquilles récurrentes du sous-titrage automatique RTVE."""
    if not text:
        return ""
    replacements = {
        "crédito moneda": "criptomoneda",
        "ráipida": "rápida",
        "abatido ya récord": "ha batido ya récord",
        "bajo ceo": "bajo cero",
        "La Guarda Civil": "La Guardia Civil",
        "le confianza": "la confianza",
        "con su bajo que pueden": "con chubascos que pueden",
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_rtve_catalog(limit=40):
    """Récupère les éditions du Telediario en 4' depuis la page thématique RTVE."""
    url = "https://www.rtve.es/temas/telediario-en-4/10650/"
    try:
        page_html = http_get_text(url, timeout=RTVE_TIMEOUT, retries=3)
    except Exception as e:
        print(f"[!] Erreur de connexion à RTVE : {e}")
        return []

    configs = re.findall(r"data-config='([^']+)'", page_html)
    episodes = []
    seen_ids = set()

    for c in configs:
        try:
            raw = c.replace('&quot;', '"').replace('&#39;', "'")
            data = json.loads(raw)
            vid_id = str(data.get("id"))
            if vid_id and vid_id not in seen_ids:
                seen_ids.add(vid_id)
                title = html.unescape(data.get("title", "")).strip()
                date_str = data.get("date", {}).get("key", "")
                date_label = data.get("date", {}).get("label", "")
                duration = data.get("duration", "4 min")
                page_url = data.get("url", "")
                img_url = data.get("img", {}).get("normal", {}).get("url", "")

                hls_url = f"https://ztnr.rtve.es/ztnr/{vid_id}.m3u8"
                embed_url = f"https://www.rtve.es/drmn/embed/video/{vid_id}"

                episodes.append({
                    "id": vid_id,
                    "title": title,
                    "date": date_str,
                    "date_label": date_label,
                    "duration": duration,
                    "url": page_url,
                    "image": img_url,
                    "hls_url": hls_url,
                    "embed_url": embed_url
                })
        except Exception:
            continue

    return episodes[:limit] if limit else episodes


def extract_vtt_text(vtt_content):
    """
    Extrait fidèlement la transcription mot-à-mot depuis le contenu WebVTT de RTVE,
    en nettoyant les métadonnées de sous-titrage et en éliminant les répétitions
    de blocs consécutifs sans jamais tronquer ni paraphraser le texte parlé.
    """
    if not vtt_content:
        return ""
    normalized = vtt_content.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    blocks = re.split(r"\n\s*\n", normalized)
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        text_lines = []
        for line in lines:
            if "-->" in line or line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE") or line.startswith("REGION") or line.isdigit():
                continue
            cleaned = re.sub(r"<[^>]+>", "", line).strip()
            if cleaned:
                text_lines.append(cleaned)
        if text_lines:
            cue_text = " ".join(text_lines)
            if not cues or cues[-1] != cue_text:
                cues.append(cue_text)
    transcript = " ".join(cues)
    return clean_asr_typos(transcript)


def fetch_transcription_and_editorial(video_id):
    """
    Extrait la transcription officielle mot-à-mot depuis les sous-titres VTT espagnols de RTVE,
    avec le synopsis éditorial officiel en repli si le VTT n'est pas encore disponible.
    """
    vtt_text = ""
    editorial_text = ""

    sub_url = f"https://www.rtve.es/api/videos/{video_id}/subtitulos.json"
    try:
        sub_raw = http_get_text(sub_url, timeout=RTVE_TIMEOUT, retries=3)
        data = json.loads(sub_raw)
        items = data.get("page", {}).get("items", [])
        vtt_url = None
        for it in items:
            if it.get("lang") == "es":
                vtt_url = it.get("src")
                break
        if not vtt_url and items:
            vtt_url = items[0].get("src")

        if vtt_url:
            vtt_content = http_get_text(vtt_url, timeout=RTVE_TIMEOUT, retries=3)
            vtt_text = extract_vtt_text(vtt_content)
    except Exception as e:
        print(f"    [!] Avertissement VTT ({video_id}) : {e}")

    try:
        info_url = f"https://api.rtve.es/api/videos/{video_id}.json"
        info_raw = http_get_text(info_url, timeout=RTVE_TIMEOUT, retries=3)
        info_data = json.loads(info_raw)
        items = info_data.get("page", {}).get("items", [])
        if items:
            desc = items[0].get("description") or items[0].get("shortDescription") or ""
            desc = re.sub(r'</p>\s*<p[^>]*>', '\n\n', desc)
            desc = desc.replace(" -- ", ". ")
            editorial_text = re.sub(r'<[^>]+>', ' ', html.unescape(desc))
            editorial_text = re.sub(r'[ \t]+', ' ', editorial_text).strip()
    except Exception as e:
        print(f"    [!] Avertissement Synopsis RTVE ({video_id}) : {e}")

    # Priorité absolue à la transcription mot-à-mot des sous-titres VTT
    transcript = vtt_text if len(vtt_text) > 40 else editorial_text
    return transcript, vtt_text, editorial_text


def parse_gemini_json(raw_text):
    """Extrait proprement le JSON même si le modèle ajoute des balises markdown."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def generate_ai_analysis(transcript, editorial_text, title, date, existing_labels):
    """Génère le résumé et le lexique CPGE ainsi que le niveau d'importance via Gemini avec retries."""
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY non définie dans l'environnement.")

    forbidden_labels = ", ".join(f'"{lbl}"' for lbl in sorted(existing_labels) if lbl)

    context_editorial = f"\nSynopsis éditorial RTVE (contexte complémentaire) :\n\"\"\"{editorial_text}\"\"\"\n" if editorial_text else ""

    prompt = f"""Tu es un professeur agrégé d'espagnol en CPGE (classes préparatoires aux grandes écoles).
Tu analyses la transcription intégrale mot-à-mot de cette édition du journal d'actualité RTVE « Telediario en 4 minutos » :
- Titre : {title}
- Date : {date}
{context_editorial}
Transcription intégrale du journal :
\"\"\"{transcript}\"\"\"

Ta mission est de produire une ressource pédagogique d'actualité de haute qualité pour des étudiants de CPGE préparant les concours des grandes écoles.
Génère STRICTEMENT un objet JSON valide (aucun commentaire, pas de balises markdown) ayant exactement cette structure :
{{
  "importance": "normal",
  "importance_label": "",
  "summary": [
    "Premier point clé en espagnol d'actualité soutenu : fait marquant, acteurs et enjeux politiques ou économiques (2 à 3 phrases).",
    "Deuxième point clé sur un autre sujet abordé...",
    "Troisième point clé..."
  ],
  "vocabulary": [
    {{
      "term": "mot ou expression idiomatique en espagnol présent dans le journal",
      "translation": "traduction précise en français adaptée au contexte concours CPGE",
      "context": "phrase courte d'illustration extraite du journal"
    }}
  ]
}}

Consignes impératives :
1. 'importance' :
   - Mets "normal" pour la majorité des éditions quotidiennes (environ 75% des journaux).
   - Mets "important" (affiché en bleu) uniquement si l'édition annonce pour la première fois un fait politique, économique ou géopolitique notable.
   - Mets "major" (affiché en rouge, très rare) uniquement pour un choc historique exceptionnel (catastrophe nationale, démission d'un ministre de premier plan, bascule électorale historique).
2. 'importance_label' :
   - Si 'importance' vaut "normal", laisse "".
   - Si 'importance' vaut "important" ou "major", donne un intitulé très court et sobre en français (3 à 5 mots, SANS AUCUN EMOJI, ex: "Démission d'Óscar Puente", "Collision ferroviaire à Urda").
   - Cet intitulé doit être STRICTEMENT UNIQUE et ne JAMAIS reprendre un sujet déjà étiqueté dans cette liste : [{forbidden_labels}]. Si l'événement principal a déjà été étiqueté dans une édition précédente, mets 'importance': "normal" et 'importance_label': "".
3. 'summary' : 3 à 4 tirets synthétiques en espagnol journalistique soutenu.
4. 'vocabulary' : 5 à 7 termes ou tournures de niveau concours.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    last_error = None
    for model_name in GEMINI_MODELS:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={API_KEY}"
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as res:
                    data = json.loads(res.read().decode("utf-8"))
                text_response = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = parse_gemini_json(text_response)
                if (
                    isinstance(parsed.get("summary"), list)
                    and len(parsed["summary"]) > 0
                    and isinstance(parsed.get("vocabulary"), list)
                    and len(parsed["vocabulary"]) > 0
                ):
                    return parsed
            except Exception as e:
                last_error = f"[{model_name} tentative {attempt}/{MAX_RETRIES_PER_MODEL}] {e}"
                print(f"    [!] Avertissement Gemini {last_error}")
                time.sleep(2 * attempt)
                continue

    raise RuntimeError(f"Échec de l'appel Gemini sur tous les modèles : {last_error}")


def is_valid_cached_episode(ep):
    """Vérifie qu'une édition en cache possède un vrai résumé complet et du vocabulaire."""
    if not ep:
        return False
    summary = ep.get("summary")
    vocab = ep.get("vocabulary")
    transcript = ep.get("transcript", "")
    if not isinstance(summary, list) or len(summary) == 0:
        return False
    first_line = str(summary[0])
    for marker in INVALID_SUMMARY_MARKERS:
        if marker.lower() in first_line.lower():
            return False
    if not isinstance(vocab, list) or len(vocab) == 0:
        return False
    if not transcript or len(transcript) < 60:
        return False
    return True


def load_existing_data():
    """Charge toutes les éditions existantes pour conserver tout l'historique depuis le début de l'année."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {str(item["id"]): item for item in data if "id" in item}
        except Exception as e:
            print(f"[!] Erreur lecture cache existant : {e}")
            return {}
    return {}


def sort_episodes_chronologically(episodes_dict):
    """Trie toutes les éditions de la plus récente à la plus ancienne sans jamais en supprimer."""
    def sort_key(ep):
        date_str = ep.get("date") or ""
        try:
            vid_int = int(ep.get("id", 0))
        except Exception:
            vid_int = 0
        return (date_str, vid_int)

    return sorted(episodes_dict.values(), key=sort_key, reverse=True)


def enforce_unique_importance_labels(episodes_sorted):
    """
    Garantit qu'il n'y a jamais deux fois le même intitulé d'importance (du plus ancien au plus récent,
    seule la première édition qui couvre un événement garde son badge) et supprime tout emoji.
    """
    seen_labels = set()
    # Parcourir du plus ancien au plus récent pour attribuer la primeur à la 1re édition
    for ep in reversed(episodes_sorted):
        vid = str(ep.get("id", ""))
        if vid in KNOWN_IMPORTANCE:
            lvl, lbl = KNOWN_IMPORTANCE[vid]
            ep["importance"] = lvl
            ep["importance_label"] = lbl
        else:
            lvl = ep.get("importance", "normal")
            lbl = (ep.get("importance_label") or "").replace("⚡", "").strip()
            if lvl not in ("important", "major") or not lbl:
                ep["importance"] = "normal"
                ep["importance_label"] = ""
            else:
                ep["importance"] = lvl
                ep["importance_label"] = lbl

        # Nettoyer les anciens champs éventuels
        ep.pop("is_major", None)
        ep.pop("major_label", None)

        norm_lbl = ep["importance_label"].lower().strip()
        if norm_lbl:
            if norm_lbl in seen_labels:
                ep["importance"] = "normal"
                ep["importance_label"] = ""
            else:
                seen_labels.add(norm_lbl)


def main():
    print("=" * 68)
    print("   MISE À JOUR AUTOMATIQUE DU TELEDIARIO EXPRESS CPGE (HISTORIQUE COMPLET)")
    print("=" * 68)

    os.makedirs(DATA_DIR, exist_ok=True)
    existing_db = load_existing_data()
    print(f"[*] {len(existing_db)} éditions déjà présentes dans l'archive historique.")

    catalog_episodes = fetch_rtve_catalog(limit=50)
    if not catalog_episodes:
        print("[!] Aucun épisode récupéré depuis RTVE lors de cet appel.")
        if not existing_db:
            return
    else:
        print(f"[*] {len(catalog_episodes)} éditions trouvées sur le catalogue RTVE.")

    merged_db = dict(existing_db)
    existing_labels = {
        (ep.get("importance_label") or "").strip()
        for ep in merged_db.values()
        if ep.get("importance_label")
    }
    new_count = 0

    for ep in catalog_episodes:
        ep_id = str(ep["id"])
        cached = merged_db.get(ep_id)

        if is_valid_cached_episode(cached):
            merged_db[ep_id] = cached
            print(f"  [CACHE OK] {cached.get('title', ep['title'])} ({cached.get('date', ep['date'])}) - déjà complet.")
            continue

        print(f"\n[+] ANALYSE DE L'ÉDITION : {ep['title']} ({ep['date']}) [ID {ep_id}]")
        transcript, vtt_text, editorial_text = fetch_transcription_and_editorial(ep_id)

        if "Telediario 1" in ep["title"] and (
            "/telediario-2/" in ep.get("url", "") or "buenas noches" in (vtt_text[:120] or "").lower()
        ):
            ep["title"] = ep["title"].replace("Telediario 1", "Telediario 2")

        if transcript:
            ep["transcript"] = transcript
            print(f"    - Transcription mot-à-mot (VTT: {len(vtt_text)} car. | Synopsis RTVE: {len(editorial_text)} car.).")
            try:
                ai_data = generate_ai_analysis(transcript, editorial_text, ep["title"], ep["date"], existing_labels)
                ep["summary"] = ai_data.get("summary", [])
                ep["vocabulary"] = ai_data.get("vocabulary", [])

                imp = ai_data.get("importance", "normal")
                lbl = (ai_data.get("importance_label") or "").replace("⚡", "").strip()
                if imp in ("important", "major") and lbl and lbl not in existing_labels:
                    ep["importance"] = imp
                    ep["importance_label"] = lbl
                    existing_labels.add(lbl)
                else:
                    ep["importance"] = "normal"
                    ep["importance_label"] = ""

                print(f"    - Succès IA : {len(ep['summary'])} points clés, {len(ep['vocabulary'])} termes CPGE.")
                new_count += 1
                merged_db[ep_id] = ep
            except Exception as err:
                print(f"    - Erreur Gemini : {err}")
                if cached and is_valid_cached_episode(cached):
                    merged_db[ep_id] = cached
                    print(f"    [i] Conservation de la version complète précédente en cache.")
                else:
                    print(f"    [!] Édition non publiée en l'état (pas de résumé/lexique). Elle sera retraitée au prochain passage.")
                continue
        else:
            print("    - Aucune transcription disponible pour l'instant (journal trop récent).")
            if cached and is_valid_cached_episode(cached):
                merged_db[ep_id] = cached
            else:
                print(f"    [!] Édition non publiée en l'état : en attente des sous-titres RTVE au prochain passage.")
            continue

        time.sleep(1.5)

    all_episodes_sorted = sort_episodes_chronologically(merged_db)
    enforce_unique_importance_labels(all_episodes_sorted)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_episodes_sorted, f, indent=2, ensure_ascii=False)

    with open(OUTPUT_JS_FILE, "w", encoding="utf-8") as f:
        f.write("window.TELEDIARIO_DATA = " + json.dumps(all_episodes_sorted, indent=2, ensure_ascii=False) + ";\n")

    print("\n" + "=" * 68)
    print(f"[OK] Total d'éditions conservées (sans suppression) : {len(all_episodes_sorted)}")
    print(f"[OK] Fichier JSON actualisé : {OUTPUT_FILE}")
    print(f"[OK] Fichier JS local actualisé : {OUTPUT_JS_FILE}")
    print(f"[OK] {new_count} nouvelle(s) édition(s) enrichie(s).")
    print("=" * 68)


if __name__ == "__main__":
    main()
