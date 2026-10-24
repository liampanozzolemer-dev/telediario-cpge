import urllib.request
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

# Modèles Gemini avec repli automatique en cas de charge temporaire
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-flash-latest"
]

def fetch_rtve_catalog(limit=15):
    """Récupère les dernières éditions du Telediario en 4' depuis la page thématique RTVE."""
    url = "https://www.rtve.es/temas/telediario-en-4/10650/"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        page_html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore')
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
                title = html.unescape(data.get("title", ""))
                date_str = data.get("date", {}).get("key", "")
                date_label = data.get("date", {}).get("label", "")
                duration = data.get("duration", "4 min")
                page_url = data.get("url", "")
                img_url = data.get("img", {}).get("normal", {}).get("url", "")
                
                # Flux vidéo direct (HLS m3u8) et iframe d'intégration officielle
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

    return episodes[:limit]

def fetch_transcription(video_id):
    """Extrait la transcription officielle depuis les sous-titres VTT espagnols de RTVE."""
    # 1. Tentative par le fichier de sous-titres VTT
    sub_url = f"https://www.rtve.es/api/videos/{video_id}/subtitulos.json"
    try:
        req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=6).read().decode('utf-8', errors='ignore'))
        items = data.get("page", {}).get("items", [])
        vtt_url = None
        for it in items:
            if it.get("lang") == "es":
                vtt_url = it.get("src")
                break
        
        if vtt_url:
            req_vtt = urllib.request.Request(vtt_url, headers={'User-Agent': 'Mozilla/5.0'})
            vtt_content = urllib.request.urlopen(req_vtt, timeout=6).read().decode('utf-8', errors='ignore')
            lines = []
            for line in vtt_content.splitlines():
                line = line.strip()
                if not line or "-->" in line or line.isdigit() or line.startswith("WEBVTT"):
                    continue
                cleaned = re.sub(r'<[^>]+>', '', line)
                if cleaned and (not lines or lines[-1] != cleaned):
                    lines.append(cleaned)
            clean_text = " ".join(lines)
            if len(clean_text) > 80:
                return clean_text
    except Exception:
        pass

    # 2. Repli : Récupération du résumé éditorial officiel RTVE
    try:
        info_url = f"https://api.rtve.es/api/videos/{video_id}.json"
        req_info = urllib.request.Request(info_url, headers={'User-Agent': 'Mozilla/5.0'})
        info_data = json.loads(urllib.request.urlopen(req_info, timeout=6).read().decode('utf-8', errors='ignore'))
        items = info_data.get("page", {}).get("items", [])
        if items:
            desc = items[0].get("description", "")
            return re.sub(r'<[^>]+>', ' ', html.unescape(desc)).strip()
    except Exception:
        pass

    return ""

def generate_ai_analysis(transcript, title, date):
    """Génère le résumé et le lexique CPGE via l'API Gemini avec modèle de repli."""
    prompt = f"""Tu es un professeur agrégé d'espagnol en CPGE (classes préparatoires aux grandes écoles).
Tu analyses la transcription de cette édition du journal d'actualité RTVE « Telediario en 4 minutos » :
- Titre : {title}
- Date : {date}

Transcription intégrale :
\"\"\"{transcript}\"\"\"

Ta mission est de produire une ressource d'actualité pour des élèves préparant les concours des grandes écoles.
Génère STRICTEMENT un objet JSON valide (aucun commentaire, pas de balises de code markdown comme ```json) :
{{
  "summary": [
    "Un tiret en espagnol d'actualité soutenu : résumé précis et nuancé d'un fait marquant, de ses acteurs et des enjeux politiques ou économiques (2 à 3 phrases).",
    "Deuxième tiret sur un autre sujet clé abordé...",
    "Troisième tiret..."
  ],
  "vocabulary": [
    {{
      "term": "mot ou expression idiomatique en espagnol (ex: una brecha, arrojar luz, un escollo)",
      "translation": "traduction précise en français adaptée au contexte",
      "context": "phrase courte d'illustration ou extrait du journal"
    }}
  ]
}}

Consignes pédagogiques impératives :
1. 'summary' : 3 à 5 tirets synthétiques, rédigés dans un style journalistique soigné.
2. 'vocabulary' : 5 à 7 termes de niveau concours (politique, économie, institutions, mouvements sociaux, transition écologique). Éviter absolument les termes trop simples (como 'país', 'presidente', 'hospital').
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
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            res = urllib.request.urlopen(req, timeout=20)
            data = json.loads(res.read().decode('utf-8'))
            text_response = data['candidates'][0]['content']['parts'][0]['text']
            parsed = json.loads(text_response)
            if "summary" in parsed and "vocabulary" in parsed:
                return parsed
        except Exception as e:
            last_error = e
            time.sleep(1)
            continue

    raise Exception(f"Échec de l'appel Gemini sur tous les modèles : {last_error}")

def load_existing_data():
    """Charge les données déjà existantes pour éviter de re-générer inutilement."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {item["id"]: item for item in data}
        except Exception:
            return {}
    return {}

def main():
    print("=" * 60)
    print("   MISE À JOUR AUTOMATIQUE DU TELEDIARIO EXPRESS CPGE")
    print("=" * 60)

    os.makedirs(DATA_DIR, exist_ok=True)
    existing_db = load_existing_data()
    print(f"[*] {len(existing_db)} éditions déjà présentes dans la base.")

    episodes = fetch_rtve_catalog(limit=10)
    if not episodes:
        print("[!] Aucun épisode trouvé sur RTVE. Fin.")
        return

    print(f"[*] {len(episodes)} éditions récentes trouvées sur RTVE.")

    updated_list = []
    new_count = 0

    for ep in episodes:
        ep_id = ep["id"]
        # Si l'épisode a déjà un résumé complet, on le réutilise
        if ep_id in existing_db and existing_db[ep_id].get("summary") and existing_db[ep_id].get("vocabulary"):
            updated_list.append(existing_db[ep_id])
            print(f"  [CACHE] {ep['title']} ({ep['date']}) - déjà analysé.")
            continue

        print(f"\n[+] NOUVELLE ÉDITION : {ep['title']} ({ep['date']})")
        transcript = fetch_transcription(ep_id)
        
        if transcript:
            ep["transcript"] = transcript
            print(f"    - Transcription extraite ({len(transcript)} car.). Envoi à Gemini...")
            try:
                ai_data = generate_ai_analysis(transcript, ep["title"], ep["date"])
                ep["summary"] = ai_data.get("summary", [])
                ep["vocabulary"] = ai_data.get("vocabulary", [])
                print(f"    - Succès IA : {len(ep['summary'])} points d'actualité, {len(ep['vocabulary'])} mots de lexique.")
                new_count += 1
            except Exception as err:
                print(f"    - Erreur Gemini : {err}")
                ep["summary"] = ["Résumé en cours de traitement."]
                ep["vocabulary"] = []
        else:
            ep["transcript"] = ""
            print("    - Aucune transcription disponible pour l'instant.")
            ep["summary"] = ["Journal très récent : transcription en attente."]
            ep["vocabulary"] = []

        updated_list.append(ep)
        # Petite pause pour respecter les quotas
        time.sleep(1)

    # Conservation d'une fenêtre glissante de 10 à 12 éditions (la semaine active)
    # (Les classements et favoris des étudiants sont conservés indépendamment dans le localStorage de leur téléphone)
    updated_list = updated_list[:12]

    # Sauvegarde dans le fichier JSON et dans le fichier JS (pour double-clic local sans serveur)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_list, f, indent=2, ensure_ascii=False)

    with open(OUTPUT_JS_FILE, "w", encoding="utf-8") as f:
        f.write("window.TELEDIARIO_DATA = " + json.dumps(updated_list, indent=2, ensure_ascii=False) + ";\n")

    print("\n" + "=" * 60)
    print(f"[OK] Fichier JSON actualisé : {OUTPUT_FILE}")
    print(f"[OK] Fichier JS local actualisé : {OUTPUT_JS_FILE}")
    print(f"[OK] {new_count} nouvelle(s) édition(s) analysée(s) par l'IA.")
    print("=" * 60)

if __name__ == "__main__":
    main()
