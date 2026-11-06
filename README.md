# WebConf : bot Teams / Zoom avec enregistrement et intervention audio à distance

```
 PC / iPad / iPhone (navigateur)
        │  HTTPS (IHM + API)            ┌──────────── webconf-w1 (pve-02, 192.168.0.231) ─────────────┐
        ▼                               │ Xvfb + Chrome (Playwright) ─ client web Teams/Zoom       │
 webconf-ctl (pve-01, 192.168.0.230) ──API─┤ PulseAudio virtuel :                                     │
  Caddy (TLS) → FastAPI                 │   meeting_out (son réunion) ──► WebRTC ──► ton casque    │
  - lance / quitte / planifie           │   ton micro ──► WebRTC ──► user_in ──► virtmic ──► Chrome│
  - relaie signalisation WebRTC         │ ffmpeg : écran + son réunion + ta voix → MP4             │
  - relaie l'écran distant (noVNC)      │ x11vnc/noVNC : reprise en main manuelle                  │
  - liste / lit / supprime les MP4      └──────────── webconf-w2 (pve-03, 192.168.0.232) idem ────────┘
        ▲
        └── audio WebRTC direct navigateur ⇄ worker (UDP), en LAN ou via Tailscale (route 192.168.0.0/24 d'OPNsense)
```

Schémas détaillés (Mermaid, affichés par GitLab) : [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ;
version modifiable draw.io : [docs/architecture.drawio](docs/architecture.drawio).

- **3 VM Debian 12** (cloud-init) : ctl 2 vCPU/2 Go, workers 4 vCPU/6 Go (≈ 850 Mo utilisés au repos).
- **2 workers** : deux réunions en parallèle (Teams, Zoom ou toute autre webconf). N'importe quel worker
  accepte n'importe quelle plateforme ; « Worker libre » prend le premier disponible.

## Utilisation

IHM : **https://192.168.0.230** (LAN, certificat interne : accepter l'avertissement).
Mot de passe : `ADMIN_PASSWORD` dans `/etc/webconf/control.env` sur webconf-ctl.

1. Coller le lien de la réunion. Le nom affiché se remplit selon la plateforme (« Assistant » par défaut,
   par ex. « Prénom NOM - Société ») et la durée max vaut 180 min par défaut ; les deux restent modifiables.
   Pour changer ces valeurs : `DEFAULT_NAMES` (JSON, clés `zoom`, `teams`, `web`) et `DEFAULT_DURATION_MIN`
   dans `/etc/webconf/control.env`.
   **Autres webconfs** (Google Meet, Jitsi, Webex, BigBlueButton, Whereby, Livestorm…) : la plateforme
   « Auto » choisit « Autre webconf », qui essaie tout seul : accepter les cookies, saisir le nom et le
   code (champ « Code / mot de passe »), cliquer « Rejoindre / Join / Ask to join », activer l'audio par
   l'ordinateur (BBB : « Microphone » puis test d'écho), puis reconnaître la salle d'attente et la barre
   de réunion. Testé sur Jitsi. Si l'interface n'est pas reconnue, l'enregistrement démarre quand même
   et la carte affiche « ℹ entrée automatique non confirmée » : vérifier ou finir via 🖥 Écran.
   La fin de réunion n'est détectée que si le bouton Quitter/Raccrocher a été reconnu ; sinon c'est la
   durée max qui arrête. « Ouvrir seulement (manuel) » ouvre juste la page et enregistre.
2. **Lancer maintenant**, ou **Planifier** (le bot entre à l'heure prévue ; il réessaie pendant 30 min si
   aucun worker n'est libre).
3. Sur la carte du worker : aperçu en direct, ● REC avec la durée.
   - **🎧 Rejoindre l'audio** : tu entends la réunion. Ton micro est **coupé par défaut**.
   - **🎚 Audio** (en-tête ou carte) : choix du **micro** et de la **sortie** (casque, haut-parleurs), test
     du micro avec vumètre et bip de test. Choix mémorisés sur l'appareil ; le micro change à chaud pendant
     une réunion. Sur Safari/iOS la sortie se choisit dans le système (Centre de contrôle).
   - **✋ Parler** (appui maintenu) ou **🎙️ Micro** (bascule) pour intervenir.
   - **Muet réunion** : bascule le micro du bot dans la réunion (Teams Ctrl+Maj+M, Zoom Alt+A,
     autres : bouton micro de la page).
   - **🖥 Écran** : prise en main du navigateur du worker (noVNC), utile pour la salle d'attente, un
     captcha, un écran qui a changé, partager l'écran…
   - **🔑 Ouvrir…** : ouvre une URL, par exemple pour **se connecter une fois** à un compte Teams/Zoom.
     Le profil Chrome est persistant, la session est conservée (sinon le bot rejoint en invité).
   - **Quitter** : quitte la réunion et arrête l'enregistrement. Le bot quitte aussi tout seul quand la
     réunion se termine, ou à la fin de la durée max.
4. **📝 Transcription en direct** (case « Transcrire », cochée par défaut) : le panneau s'ouvre sur la
   carte du worker dès que la réunion commence.
   - **Intervenants distingués**, chacun avec sa couleur, sous forme de pastilles au-dessus du texte :
     - **Moi** : ta voix envoyée depuis l'IHM (flux séparé, toujours juste) ;
     - les participants sont séparés par **empreinte vocale** (WeSpeaker ResNet34, ONNX sur le CPU du
       worker, ~60 ms par phrase), ce qui distingue aussi plusieurs personnes derrière un même micro ;
     - leurs **vrais noms** viennent de l'orateur actif affiché par la plateforme, relevé 4 fois par
       seconde (🖥 sur la pastille). Une voix ne peut pas être associée à deux appareils différents.
       Deux voix sur un même appareil donnent « Salle Nord » et « Salle Nord (2) ».
     - Sans nom affiché, les participants deviennent « Intervenant 1, 2… » (🔊).
     - **Clic sur un nom** (pastille ou paragraphe) pour renommer (✏️), pendant la réunion ou après dans
       le lecteur ; un nom vide rend le nom automatique. Le fichier `.md` est mis à jour.
     - Testé sur Jitsi avec 3 participants et 4 voix, dont 2 sur le même appareil : 8 phrases sur 8 bien
       attribuées et nommées. Sans noms affichés, 32 morceaux de phrases sur 32 bien regroupés.
       Teams et Zoom : sélecteurs écrits d'après leurs interfaces web, à confirmer sur une vraie réunion
       (diagnostic : `GET /debug/speakers` sur le worker).
   - Les phrases sont découpées sur les silences et transcrites par **Whisper large v3**, en 1 à 2 s après
     la fin de chaque phrase. Elles apparaissent d'abord en italique (texte brut).
   - Toutes les ~20 s, **Qwen3.8-27B** les remet en forme : ponctuation, hésitations supprimées, termes
     corrigés d'après le glossaire (`TRANSCRIPT_GLOSSARY`), paragraphes. Il ne résume rien.
   - Les deux modèles passent par **OVH AI Endpoints** ; la clé est dans `worker.env`.
   - Boutons : ⏹/▶ pour arrêter ou relancer en cours de réunion, 📋 pour copier le texte.
   - Fichiers à côté du MP4 : `<nom>.transcript.json` (brut et mis en forme) et `<nom>.md`. Les horodatages
     sont ceux de la vidéo.
   - Sans enregistrement, la transcription apparaît seule dans 📁 Enregistrements (vignette 📝).
   - **🧠 Points d'étape** (onglet du panneau, pastille « nouveau ») : toutes les **5 minutes**, Qwen relit
     toute la transcription depuis le début et met à jour :
     🎯 le sujet en cours, 🙋 **ce qui te concerne** (questions qui te sont posées, actions et engagements
     qui te reviennent), 🧭 où en est la réunion, ⭐ points à retenir, ✅ décisions, 📌 actions (qui, quand),
     ❓ questions ouvertes et répondues, ⚠️ points de vigilance (désaccords, risques, chiffres qui changent),
     🗂 sujets abordés, et 🕒 le **fil de la réunion** (un résumé par tranche de 5 min).
     « ↻ Analyser » fait un point tout de suite. Un clic sur un horodatage ramène au passage de la transcription.
6. **📋 Compte rendu final**, rédigé automatiquement à la fin de la réunion (environ 6 à 15 min selon la
   durée de la réunion et la charge d'OVH) :
   - **plusieurs modèles se contrôlent** :
     1. Qwen3.8-27B, Qwen3.5-397B et GPT-OSS-120B rédigent chacun un compte rendu complet, en parallèle et
        sans se voir ;
     2. un arbitre (Qwen3.8-27B, en mode réflexion) confronte les trois versions point par point. Il tranche
        chaque divergence en revenant à la transcription et garde l'union de ce qui est étayé ;
     3. un contrôleur (Qwen3.5-397B, le plus gros modèle) revérifie chaque élément contre la transcription,
        cherche les oublis ligne par ligne, et renvoie ses corrections ; le programme les applique ;
     4. un contrôle par programme des dates : jours de semaine incohérents, « vendredi » converti en date.
   - **contenu** : objet, synthèse, à retenir, tableau des décisions (porteur, statut, condition), tableau
     des actions (responsable, échéance), « Pour toi », déroulé par sujet avec la position de chacun,
     questions ouvertes, risques, chiffres et dates cités, prochaine réunion, **points à vérifier**
     (inaudible, ambiguïtés) et temps de parole mesuré ;
   - **traçabilité** : chaque élément porte son horodatage, et un clic fait sauter la vidéo au passage.
     L'annexe « Méthode et contrôle » liste les modèles utilisés, les arbitrages entre versions et les
     corrections du contrôleur ;
   - **où le trouver** : dans 📁 Enregistrements, bouton **📋 CR** ou onglet « Compte rendu » du lecteur ;
     téléchargement **Word** (.docx) ou Markdown, et ↻ Régénérer ;
   - **noms** : le compte rendu désigne les personnes par leur identifiant, donc renommer un intervenant
     met aussi à jour le compte rendu (Word compris), sans le régénérer ;
   - fichier `<nom>.report.json` à côté du MP4.
7. Menu **📁 Enregistrements** (en haut sur PC, barre du bas sur mobile ; lien direct `/#rec`) :
   - les vidéos classées par jour, avec une miniature, la durée, la plateforme, la taille et le worker ;
   - une recherche (titre, date) et des filtres par plateforme et par worker ;
   - ▶ lecture dans la page (y compris sur iPhone, Range supporté), avec la transcription sous la vidéo :
     un clic sur un horodatage fait sauter la vidéo au bon endroit. Badge 📝 sur les vignettes transcrites ;
   - ⬇ téléchargement du MP4, 📝⬇ de la transcription (Markdown),
     ✏️ renommage (titre affiché), 🗑 suppression (impossible tant que l'enregistrement est en cours) ;
   - l'espace disque libre de chaque worker.
   Fichiers : `/srv/webconf/recordings/*.mp4` sur chaque worker. Vidéo 1920x1200 (réglable : `SCREEN_SIZE`) à **2 images/s**, très
   compressée (H.264, environ 25 Mo/h) ; audio **AAC 192 kb/s, 48 kHz stéréo** (environ 85 Mo/h), soit environ 110 Mo/h au total.
   Réglables dans `worker.env` : `RECORD_FPS`, `RECORD_CRF`, `RECORD_AUDIO_BITRATE`, `SCREEN_SIZE`.
   À côté de chaque MP4 : un `.json` (métadonnées) et un `.jpg` (miniature). Le MP4 est fragmenté : il
   reste lisible même après un crash.

### Réglages de la transcription (`/etc/webconf/worker.env`, puis `systemctl restart webconf-agent`)

| Variable | Défaut | Rôle |
|---|---|---|
| `OVH_AI_ENDPOINTS_ACCESS_TOKEN` | (vide = désactivé) | clé OVH AI Endpoints. `deploy-all.sh` la reprend de `.ovh_token` (mode 600) |
| `TRANSCRIPT_GLOSSARY` | (vide) | termes à bien orthographier (Whisper et Qwen), séparés par des virgules |
| `TRANSCRIPT_LANG` | `fr` | langue de la réunion |
| `TRANSCRIPT_ME_LABEL` / `TRANSCRIPT_OTHERS_LABEL` | `Moi` / `Participants` | noms des deux locuteurs |
| `TRANSCRIPT_LLM_MODEL` | `Qwen3.8-27B` | modèle de mise en forme (`Qwen3.6-27B` aussi disponible) |
| `TRANSCRIPT_ASR_MODEL` | `whisper-large-v3` | `whisper-large-v3-turbo` est plus rapide mais moins fidèle |
| `TRANSCRIPT_FORMAT_EVERY` | `20` | secondes entre deux passes de mise en forme |
| `TRANSCRIPT_VAD_DB` | `-45` | seuil de détection de parole (dBFS) |
| `TRANSCRIPT_SPK_NEW` | `0.23` | similarité de voix (empreintes centrées sur la moyenne de la séance) sous laquelle une nouvelle voix est créée (monter = plus de voix distinctes) |
| `TRANSCRIPT_SPK_MERGE` | `0.48` | similarité au-dessus de laquelle deux voix sont fusionnées |
| `TRANSCRIPT_SPK_MODEL` | `/opt/webconf/models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx` | modèle d'empreinte vocale (dans l'image ; téléchargé par `install-worker.sh` sur VM). Réglage évalué avec `tests/diar_real_eval.py` |
| `SLIDES` | `1` | lecture des diapositives et documents partagés à l'écran (`0` : désactivée) |
| `SLIDES_MODEL` | `Mistral-Small-3.2-24B-Instruct-2506` | modèle d'image d'OVH qui retranscrit les diapositives (`Qwen3.8-27B` : descriptions plus fines, ~3 fois plus lent) |
| `SLIDES_EVERY` / `SLIDES_MIN_GAP` / `SLIDES_MAX` | `3` / `6` / `400` | secondes entre deux coups d'œil à l'écran / entre deux lectures ; lectures maximum par réunion |
| `ANALYSIS_EVERY` | `300` | secondes entre deux points d'étape |
| `ANALYSIS_MODEL` | `Qwen3.8-27B:low` | modèle des points d'étape (`:none`/`:low`/`:medium`/`:high` = niveau de réflexion) |
| `REPORT_MODELS` | `Qwen3.8-27B:low,Qwen3.5-397B-A17B:none,gpt-oss-120b:low` | rédacteurs indépendants du compte rendu |
| `REPORT_JUDGE` / `REPORT_AUDITOR` | `Qwen3.8-27B:medium` / `Qwen3.5-397B-A17B:low` | arbitre des versions / contrôleur qualité |
| `REPORT_JUDGE_SECS` / `REPORT_JUDGE_RETRY_SECS` | `480` / `300` | délai de l'arbitre ; s'il est dépassé (OVH lent), nouvel essai avec une réflexion courte, puis à défaut le projet le plus complet est retenu (signalé dans « Méthode et contrôle ») |
| `REPORT_AUTO` | `1` | compte rendu automatique en fin de réunion (`0` : seulement sur demande) |
| `TRANSCRIPT_ME_NAME` | nom affiché sans suffixe | ton nom dans les comptes rendus (ex. `Prénom NOM`) |

Autre fournisseur compatible OpenAI pour un des modèles : `modele:effort@https://api.exemple/v1#VARIABLE_CLE`
(la clé est lue dans `VARIABLE_CLE` de `worker.env`), par ex. pour ajouter Mistral ou Claude comme rédacteur.

## iPhone / iPad : HTTPS valide obligatoire pour le micro

Safari n'autorise le micro qu'avec un certificat valide. Le plus simple, puisque tu as déjà Tailscale
(OPNsense annonce 192.168.0.0/24, donc l'UDP WebRTC vers les workers passe aussi) :

```bash
# sur webconf-ctl (activer d'abord "HTTPS Certificates" dans la console Tailscale > DNS)
scp deploy/enable-tailscale.sh debian@192.168.0.230:/tmp/ && ssh -t debian@192.168.0.230 sudo bash /tmp/enable-tailscale.sh
```
→ IHM sur `https://webconf.tail8a7335.ts.net` avec un vrai certificat Let's Encrypt. Sur l'iPhone :
Safari → Partager → « Sur l'écran d'accueil » pour l'utiliser comme une app.

Hors Tailscale (4G sans VPN), il faudrait un serveur TURN : mettre `ICE_SERVERS` (JSON
`[{"urls":"turn:...","username":"..","credential":".."}]`) dans `control.env` **et** `worker.env`.

## Déploiement / maintenance (depuis pve-00)

| Script | Rôle |
|---|---|
| `deploy/proxmox-create-vms.sh` | crée les 3 VM (VMID 230-232, image debian-12-genericcloud) |
| `deploy/deploy-all.sh` | (re)déploie le code sur les 3 VM, conserve la config |
| `worker/install-worker.sh` | installe un worker (Chrome, Xvfb, PulseAudio, ffmpeg, noVNC, agent, pare-feu) |
| `control/install-control.sh` | installe la VM de contrôle (FastAPI, Caddy, noVNC) |
| `deploy/enable-tailscale.sh` | HTTPS valide via Tailscale |
| `tests/` | tests : pont audio, boucle micro → Chrome → retour, IHM complète dans un vrai Chrome, autre webconf, transcription, intervenants (`diar_*` : réunion Jitsi simulée à 3 participants), points d'étape et compte rendu (`report_*`, `fixtures/make_meeting.py` : réunion fictive avec pièges) |

Jeton partagé ctl ↔ workers : `.agent_token` (ici) = `AGENT_TOKEN`. Les workers n'acceptent l'API (8001)
et le VNC (6080) que depuis 192.168.0.230 (nftables).

Services : `webconf-{xvfb,wm,pulse,vnc,agent}` sur les workers, `webconf-control` + `caddy` sur ctl.
Logs : `journalctl -u webconf-agent -f`.

### Workers en conteneurs (Docker)

Un worker peut aussi tourner en conteneur : même code, mêmes réglages, même IHM. **Un conteneur =
un worker = une réunion à la fois** ; plusieurs conteneurs sur un même hôte remplacent plusieurs VM.

| Fichier | Rôle |
|---|---|
| `worker/Dockerfile` | image `webconf-worker` (Debian 12, Chrome, Xvfb, PulseAudio, ffmpeg, x11vnc/noVNC, agent, modèle d'empreinte vocale) |
| `worker/container/supervisord.conf` | les 5 services du worker dans le conteneur (équivalents des unités systemd) |
| `deploy/deploy-containers.sh` | installe Docker sur l'hôte, construit l'image, crée un conteneur par worker, pare-feu |
| `deploy/proxmox-create-vms.sh` | `VMS_SPEC="233 192.168.0.201 192.168.0.233 webconf-ct 8 12288 60G"` crée la VM hôte |

```bash
HOST=192.168.0.233 NAMES="w3 w4" CONTROL_IPS="192.168.0.230" ./deploy/deploy-containers.sh
# puis ajouter la ligne affichée (w3=http://192.168.0.233:8001,w4=http://192.168.0.233:8002)
# à WORKERS dans /etc/webconf/control.env sur ctl, et : systemctl restart webconf-control
```

- **Réseau de l'hôte** (`network_mode: host`) : l'audio WebRTC va directement du navigateur au
  conteneur, comme avec une VM. Chaque conteneur a donc ses propres ports et son propre écran :
  worker n°i → API `8000+i`, VNC `6079+i`, écran `:98+i` (le premier garde 8001, 6080, `:99`).
  Le worker annonce son port VNC à l'IHM (`vnc_port` dans `/status`).
- **Données** : `/srv/webconf/<nom>/` sur l'hôte (enregistrements, profil Chrome persistant), même
  utilisateur (uid 1500) que sur les VM : un dossier d'enregistrements se déplace tel quel.
- **Réglages** : `/etc/webconf/containers/<nom>.env` sur l'hôte (mêmes variables que `worker.env`).
  Après modification : `cd /opt/webconf && docker compose up -d`.
- **Ressources** par conteneur : 4 vCPU et 6 Go maximum (`CPUS`, `MEMORY`) ; environ 1 Go et
  20-30 % d'un cœur pendant une réunion transcrite.
- **Pare-feu** de l'hôte : API et VNC des workers réservés à `CONTROL_IPS` (table nftables
  `inet webconf`, qui laisse intactes les règles de Docker).
- Exploitation : `docker ps`, `docker logs -f webconf-w3`, `docker restart webconf-w3`.
  Mise à jour du code : relancer `deploy-containers.sh` (réglages et données conservés).
- Test : `tests/container_ui_test.py` (IHM, VNC de chaque conteneur, audio, lecture) et
  `tests/container_meeting_e2e.sh` (réunion Jitsi avec participants, transcription, compte rendu).

### Chez OVHcloud (Terraform)

Une instance Public Cloud porte l'IHM et les workers en conteneurs, avec une IP publique (WebRTC
sans Tailscale ni TURN), un certificat Let's Encrypt et un disque de données séparé :
`cd deploy/ovh && terraform apply && ./deploy-ovh.sh`. Détails : [deploy/ovh/README.md](deploy/ovh/README.md).

## Limites à connaître

- **Interfaces web mouvantes** : l'entrée automatique s'appuie sur les écrans web actuels, avec
  des sélecteurs en anglais et en français (`worker/meetings.py`, classe `Generic` pour les autres webconfs).
  Google Meet refuse souvent les invités non connectés : se connecter une fois à un compte Google via 🔑. Si une étape casse, l'état passe en
  « erreur » et la page reste ouverte : on termine à la main via 🖥 Écran, puis on lance ⏺ Enregistrer.
- Zoom peut imposer un captcha ou une connexion pour le client web, selon les réglages de l'organisateur.
  Teams peut refuser les invités anonymes selon la politique du tenant : se connecter une fois via 🔑.
- **Transcription** : l'audio de la réunion est envoyé à OVH AI Endpoints (hébergé en France), à inclure
  dans l'information des participants. Qwen met en forme sans résumer, mais peut ponctuellement
  « corriger » un mot à tort : en cas de doute, la vidéo fait foi (clic sur l'horodatage).
- **Compte rendu** : les modèles se recoupent, mais restent dépendants de la transcription (mots mal
  reconnus, attribution d'un intervenant). Relire la rubrique « Points à vérifier » avant diffusion.
  Les comptes rendus passent aussi par OVH AI Endpoints (aucun autre fournisseur sans configuration).
- **Légal / RGPD** : prévenir les participants avant d'enregistrer. Les bots qui passent par le client web
  sont tolérés mais pas officiellement supportés par Zoom/Microsoft (alternatives officielles : Zoom
  Meeting SDK, Microsoft Graph Communications / bots Teams).
