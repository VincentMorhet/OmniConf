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
Connexion : identifiant et mot de passe personnels (voir « Comptes » ci-dessous). Le premier administrateur est
créé au premier démarrage avec `ADMIN_PASSWORD` (dans `/etc/webconf/control.env` sur webconf-ctl), identifiant
`ADMIN_LOGIN` (`admin` par défaut) et nom `ADMIN_NAME`.

**Comptes** (onglet Enregistrements → « 👥 Utilisateurs », réservé aux administrateurs) :
- **Création** : chaque compte reçoit un mot de passe provisoire, affiché une seule fois, à changer à la
  première connexion. Les administrateurs peuvent aussi :
  - réinitialiser un mot de passe (les sessions de la personne sont alors fermées) ;
  - changer le rôle ;
  - désactiver ou supprimer un compte (ses réunions restent, visibles des administrateurs).
- **Visibilité** : chacun voit les réunions qu'il a lancées, et celles qu'on a partagées avec lui (menu ⋯ → 🔗
  Partager, en lecture seule). Un administrateur voit tout et peut changer le propriétaire d'une réunion. Les
  réunions d'avant les comptes n'ont pas de propriétaire : seuls les administrateurs les voient.
- **Assistants** : ils sont communs. Un assistant en réunion n'est accessible qu'à celui qui l'a lancé (et aux
  administrateurs) ; les autres le voient « occupé », sans titre ni écran ni transcription.
- **👤 Mon compte** : nom dans les comptes rendus (« Pour … »), nom de l'assistant par plateforme (par
  défaut ce nom ; sinon `DEFAULT_NAMES`), mot de passe.
- **Jetons MCP** : chaque jeton appartient à un compte et ne donne accès qu'à ses réunions. Les jetons
  d'avant les comptes sont rattachés au premier administrateur.
- **Lexique** : il est commun. Tout le monde le lit, seuls les administrateurs le modifient.
- Fichier : `/var/lib/webconf/users.json` (mode 600, mots de passe hachés avec scrypt).

1. Coller le lien de la réunion. Le nom affiché se remplit selon la plateforme : celui de « Mon compte », sinon
   `DEFAULT_NAMES` (JSON, clés `zoom`, `teams`, `web`, dans `/etc/webconf/control.env`), par ex. « Prénom NOM -
   Société ». La durée max vaut 180 min par défaut (`DEFAULT_DURATION_MIN`). Les deux restent modifiables.
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
8. **Entrée ratée, terminée à la main** : si l'assistant n'arrive pas à rejoindre seul (la plateforme a
   changé sa page), il reste sur la page, en erreur, avec une capture de la page et la liste de ses boutons
   dans `recordings/debug/` (10 derniers échecs). Termine l'entrée via « Écran » : dès que l'assistant
   se voit dans la réunion, il lance l'enregistrement et la transcription demandés au départ.
9. **Transcription après coup** (enregistrement sans transcription) : `worker/offline.py` reprend la
   chaîne du direct sur la vidéo. Il fait le découpage de la parole, Whisper avec ses hallucinations
   retranscrites, les empreintes vocales, la mise en forme et les diapositives.
   - L'orateur actif est repéré dans l'image, au cadre coloré de sa vignette Teams, et son nom est lu
     par le modèle d'image. Les voix sont donc nommées comme en direct.
   - Les participants sont relevés sur les vignettes toutes les minutes.
   - Les noms tronqués ou mal lus sont regroupés.
   - Ensuite, ↻ Régénérer produit le compte rendu.

   ```bash
   docker exec -u bot webconf-w1 /opt/webconf/venv/bin/python /opt/webconf/worker/offline.py <nom>.mp4
   ```

   La transcription d'une réunion de 83 min a pris environ 30 min, surtout pour la mise en forme et les
   pages partagées.
10. **📖 Lexique du projet** (onglet Enregistrements) : un texte Markdown, saisi une fois et envoyé à tous
   les assistants (ils le retrouvent dans les 2 minutes après un redémarrage).
   - Sections reconnues :
     - `## 1. PERSONNES` : « - Prénom Nom : rôle [variantes entendues] », ou « - Équipe : Nom (rôle) ; Nom… » ;
     - `## 2. CORRECTIONS` : « entendu, autre forme → correct ; … » ;
     - `## 3. SIGLES ET TERMES` et `## 4. REGISTRE DES SUJETS` : texte libre.
   - Usages :
     - **Whisper** : les participants vus à l'écran et les formes correctes des mots souvent mal entendus
       (prompt d'environ 200 jetons) ;
     - **mise en forme** : la table des corrections, appliquée selon le contexte (le texte brut de
       Whisper reste tel quel) ;
     - **noms des intervenants** : casse (« CAMILLE DURAND » → « Camille Durand »), variantes et noms tronqués
       par la plateforme (« Camille Dur… ») complétés ;
     - **compte rendu** : personnes, sigles et registre donnés aux modèles. Un nom absent du lexique et des
       intervenants est signalé dans « Points à vérifier ».
   - Claude le lit par MCP (`get_lexicon`). La version précédente est gardée dans
     `/var/lib/webconf/lexicon.prev.md`.
   - Le compte rendu signale aussi :
     - plusieurs voix sous un même nom (« Nom (2) ») : salle partagée possible, attributions incertaines ;
     - une **couverture partielle** : entrée terminée à la main, créneau d'agenda dépassé, ou début ou fin
       manquants selon les modèles.

**Ré-identification des intervenants en fin de réunion** (`worker/reid.py`). Pendant la réunion, les voix
sont regroupées au fil de l'eau et nommées d'après la vignette que la plateforme encadre. Or la plateforme
désigne un appareil, pas une personne : quelqu'un qui parle depuis le poste d'un autre prend le nom de ce
poste. Avant chaque compte rendu, toute la réunion est donc reprise :
1. une empreinte vocale par phrase (modèle NVIDIA TitaNet large), signature du canal retirée ;
2. un regroupement global (lien moyen, seuil 0,20) ;
3. chaque groupe est nommé d'après les vignettes encadrées pendant ses phrases ; un renommage fait à la main
   l'emporte. Un groupe qui a reçu plusieurs noms sans majorité devient « Intervenant N (sans vignette) » :
   le compte rendu le signale, et il suffit de le renommer dans l'IHM.

La transcription d'avant est gardée (`*.transcript.avant-reid.json`). Réglages : `REID=0` pour désactiver,
`REID_MODEL`, `REID_THRESHOLD`.

Choix du modèle (banc d'essai `tests/speaker_models_bench.py`, 6 réunions, 29 personnes, 777 phrases dont
l'auteur est connu) :

| Modèle | AUC | F1 du regroupement |
|---|---|---|
| TitaNet large (retenu) | 0,952 | 0,875 |
| ERes2Net VoxCeleb | 0,948 | 0,866 |
| CAM++ 3D-Speaker (pendant la réunion) | 0,941 | 0,844 |
| ERes2NetV2 | 0,954 | 0,837 |
| WeSpeaker ResNet34 / ResNet293 / CAM++ | 0,52 à 0,86 | 0,36 à 0,74 |

Les combinaisons de modèles ne font pas mieux. Sur deux réunions de référence :
- un poste partagé : la personne sans vignette est retrouvée seule, 26 voix ramenées aux 8 participants ;
- trois personnes sur un même micro : elles sont séparées.

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

## Accès de Claude : serveur MCP en lecture seule

L'IHM expose un serveur MCP (Model Context Protocol, transport HTTP) sur `https://<IHM>/mcp` : Claude Code
peut lister les réunions et en lire le contenu de façon autonome, sans passer par l'interface web ni
manipuler de mot de passe. Code : `control/mcp_server.py`.

| Outil | Paramètres | Retour |
|---|---|---|
| `list_meetings` | `since`, `until`, `status`, `query`, `page`, `per_page` | id (`w1:20261110-160042_teams`), titre et titre du compte rendu, début/fin (heure de Paris), durée, plateforme, statut (`recording_in_progress`, `recorded`, `transcribed`, `draft_cr`, `final_cr`) |
| `get_meeting` | `id` | métadonnées : identifiants de la réunion (lien sans mot de passe, ids Teams/Zoom), agenda s'il est connu, participants vus (nom, organisation, arrivée/départ approximatifs), intervenants (temps de parole, source du nom), micro de l'utilisateur capté ou non, réunion effective, statistiques, avertissements |
| `get_transcript` | `id`, `format` (`segments`, `paragraphs`, `text`), `start`, `end`, `cursor` | segments `{start, end, speaker, speaker_source, text, confidence, suspect, off_meeting}` ; pages de ~40 000 caractères |
| `get_draft_cr` | `id` | premier compte rendu en Markdown, modèles utilisés (rédaction, arbitrage, contrôle) et date de génération |
| `get_slides` | `id`, `images` | diapositives : horodatage, titre, texte lu, description ; liens temporaires vers les captures (ou les images elles-mêmes) |
| `get_audio_url` | `id`, `start`, `end`, `format` | lien temporaire (signé, sans jeton) vers l'audio en MP3/WAV mono 16 kHz, en entier ou un passage |
| `get_lexicon` | — | lexique du projet (Markdown) : personnes, corrections de transcription, sigles, registre des sujets ; version et date |
| `attach_cr` | `id`, `markdown`, `pdf_base64` | dépôt du compte rendu final à côté de la réunion (jeton d'**écriture** distinct) |

* **Jetons** : propres à un client, révocables, de portée *lecture* (les 7 premiers outils) ou *écriture*
  (`attach_cr` seulement). Chaque jeton appartient à un compte : il ne voit que les réunions de ce compte
  (toutes pour un administrateur), et `attach_cr` n'est permis que sur ses propres réunions. Seule leur
  empreinte est conservée (`/var/lib/webconf/mcp_tokens.json`). En ligne de commande : `--user <identifiant>`.
  Création et révocation : IHM, onglet Enregistrements, « Accès de Claude (MCP) » (le jeton n'est affiché
  qu'une fois), ou `sudo bash -c 'set -a; . /etc/webconf/control.env; /opt/webconf/venv/bin/python
  /opt/webconf/control/mcp_server.py create --name claude --scope read --out /root/claude.tok'` (`list`, `revoke <id>`).
* **Données** : `speaker_source` vaut `teams`/`zoom` quand le nom vient de la plateforme pendant la phrase,
  `diarisation` quand il vient de la voix (nom reconnu sur d'autres phrases de la même voix, ou « Intervenant N »),
  `manuel` (renommé dans l'IHM) ou `micro_ihm` (l'utilisateur, via le micro de l'IHM). `confidence` = probabilité
  moyenne de Whisper (réunions transcrites après cette version). `suspect` = hallucination probable (boucle
  de répétition, « Sous-titrage… », « Merci » en série) : la phrase a déjà été retranscrite en tronçons plus courts,
  à vérifier à l'écoute. `off_meeting` = avant le premier ou après le dernier bloc de parole à plusieurs voix
  (séparé par plus de 3 min de silence).
* **Journal** : chaque appel est tracé (`journalctl -u webconf-control | grep " mcp "`).

Sur le poste Windows (PowerShell), une fois :

```powershell
# 1. Certificat de l'IHM (autorité locale de Caddy) : lien « télécharger le certificat racine » du panneau
#    « Accès de Claude », vérifier l'empreinte affichée, puis (sans droits administrateur) :
certutil -user -addstore Root "$env:USERPROFILE\Downloads\omniconf-ca.crt"
#    (à défaut : setx NODE_EXTRA_CA_CERTS "C:\chemin\omniconf-ca.crt")
# 2. Jeton créé dans l'IHM, gardé dans une variable d'environnement (puis rouvrir le terminal)
setx WEBCONF_TOKEN "wcr_..."
# 3. Déclaration dans Claude Code : guillemets SIMPLES pour que ${WEBCONF_TOKEN} reste tel quel
#    (Claude Code le remplace au lancement ; entre guillemets doubles, PowerShell le viderait).
#    Pas de « add-json » sous Windows PowerShell 5.1 : il retire les guillemets du JSON (« Invalid input »).
claude mcp add --transport http webconf https://192.168.0.230/mcp --header 'Authorization: Bearer ${WEBCONF_TOKEN}' --scope user
claude mcp list        # webconf: ... ✔ Connected
```

Pour ne pas confirmer chaque appel : `"permissions": {"allow": ["mcp__webconf__*"]}` dans
`%USERPROFILE%\.claude\settings.json`. Jeton d'écriture : même chose avec un second serveur
(`webconf-ecriture`, variable `WEBCONF_WRITE_TOKEN`). Les comptes rendus finaux déposés apparaissent dans
le menu ⋯ de l'enregistrement (✅).

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
| `deploy/host/webconf-update` | met à jour les assistants libres ; ceux en réunion à la fin (minuteur, chaque minute) |
| `deploy/proxmox-create-vms.sh` | `VMS_SPEC="233 192.168.0.201 192.168.0.233 webconf-ct 8 12288 60G"` crée la VM hôte |

`NAMES` doit lister **tous** les conteneurs de l'hôte : le script réécrit `compose.yml` avec ces seuls noms
et supprimerait les autres (il s'arrête désormais si un conteneur existant manque, sauf `ALLOW_REMOVE=1`).
Pour essayer une version sur un seul conteneur sans toucher aux autres : image `webconf-worker:test` et
`docker compose -f compose.yml -f compose.test.yml up -d --no-deps w3` (fichier de surcharge qui ne change
que l'image de `w3`).

**Mise à jour sans couper de réunion.** `deploy-containers.sh` construit la nouvelle image, mais ne recrée
que les assistants libres. Un assistant occupé garde son ancienne version et passe à la nouvelle dès qu'il est
libre. Sont occupés les assistants :
- en réunion ou en navigation manuelle ;
- qui enregistrent ou transcrivent ;
- qui rédigent un compte rendu ou lisent des diapositives ;
- qui font une transcription après coup.

C'est `webconf-update` qui s'en charge : script `/usr/local/sbin/webconf-update` sur l'hôte, lancé toutes les
minutes par `webconf-update.timer`. `sudo webconf-update --status` montre ce qui attend. Le contrôle (IHM)
se met à jour à part (`install-control.sh`) et ne touche jamais aux assistants : pendant son redémarrage
(quelques secondes), les réunions continuent, seule la page est momentanément indisponible.

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
