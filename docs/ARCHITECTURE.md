# Omniconf : schémas d'architecture

Les schémas sont en Mermaid (GitLab les affiche directement). Une version modifiable dans draw.io
est dans [`architecture.drawio`](architecture.drawio) : ouvrir avec https://app.diagrams.net ou
l'extension draw.io de VS Code.

1. [Vue d'ensemble](#1-vue-densemble)
2. [Intérieur d'un worker : le circuit du son](#2-intérieur-dun-worker--le-circuit-du-son)
3. [Déroulé d'une réunion](#3-déroulé-dune-réunion)
4. [De la voix au texte : transcription et intervenants](#4-de-la-voix-au-texte--transcription-et-intervenants)
5. [Compte rendu multi-modèles](#5-compte-rendu-multi-modèles)
6. [Fichiers produits](#6-fichiers-produits)
7. [Variante : workers en conteneurs](#7-variante--workers-en-conteneurs)

---

## 1. Vue d'ensemble

```mermaid
flowchart LR
    subgraph U["👤 Toi"]
        direction TB
        PC["💻 PC"]
        IPAD["📱 iPad / iPhone"]
    end

    subgraph PVE1["pve-01"]
        CTL["<b>webconf-ctl</b> · 192.168.0.230<br/>Caddy (HTTPS 443)<br/>→ FastAPI (IHM + API)<br/>planification · noVNC relais<br/>liste et lecture des MP4"]
    end

    subgraph PVE2["pve-02"]
        W1["<b>webconf-w1</b> · 192.168.0.231<br/>agent FastAPI :8001<br/>Chrome + Xvfb + PulseAudio<br/>ffmpeg · noVNC :6080<br/>transcription · analyses"]
    end

    subgraph PVE3["pve-03"]
        W2["<b>webconf-w2</b> · 192.168.0.232<br/>(identique à w1)"]
    end

    subgraph EXT["☁️ Internet"]
        direction TB
        WEBCONF["Teams · Zoom · Meet<br/>Jitsi · Webex · BBB…"]
        OVH["OVH AI Endpoints<br/>Whisper large v3<br/>Qwen 27B · Qwen 397B<br/>GPT-OSS 120B"]
    end

    PC -- "HTTPS : IHM, API" --> CTL
    IPAD -- "HTTPS : IHM, API" --> CTL
    CTL -- "API + jeton AGENT_TOKEN<br/>(seul ctl est autorisé)" --> W1
    CTL -- "API + jeton" --> W2
    PC <-. "🎧 audio WebRTC direct (UDP)<br/>LAN ou Tailscale" .-> W1
    W1 <-- "client web de la réunion" --> WEBCONF
    W2 <-- "client web" --> WEBCONF
    W1 -- "son → texte, analyses" --> OVH
    W2 --> OVH

    classDef ctl fill:#DCE6F0,stroke:#2F5597,color:#000
    classDef wk fill:#E2F0D9,stroke:#548235,color:#000
    classDef ext fill:#FFF2CC,stroke:#BF9000,color:#000
    class CTL ctl
    class W1,W2 wk
    class WEBCONF,OVH ext
```

- **ctl** ne fait que piloter : il ne rejoint aucune réunion. Il choisit un worker libre et relaie
  la signalisation WebRTC et l'écran distant.
- **Chaque worker** rejoint **une** réunion à la fois : deux workers, deux réunions en parallèle.
- **Le son** circule directement entre ton navigateur et le worker (WebRTC). Il ne passe pas par ctl.

---

## 2. Intérieur d'un worker : le circuit du son

```mermaid
flowchart LR
    subgraph CHROME["Chrome (Playwright) · Xvfb 1920x1200"]
        PAGE["Page de la webconf"]
    end

    subgraph PULSE["PulseAudio virtuel"]
        direction TB
        MO[("meeting_out<br/>son de la réunion")]
        UI[("user_in<br/>ta voix")]
        VM["virtmic<br/>(micro de Chrome)"]
    end

    BR["audio_bridge<br/>(WebRTC)"]
    TOI["🎧🎙️ ton navigateur"]

    subgraph USE["Lisent les 2 flux"]
        direction TB
        REC["ffmpeg<br/>écran 2 i/s + son 48 kHz → MP4"]
        TR["transcriber<br/>meeting_out → participants<br/>user_in → « Moi »<br/>silences → Whisper → Qwen"]
    end

    SPK["speakers<br/>empreintes vocales<br/>+ noms affichés"]

    PAGE -- "haut-parleur" --> MO
    MO --> BR
    BR -- "tu entends" --> TOI
    TOI -- "tu parles" --> BR
    BR --> UI
    UI --> VM
    VM -- "micro" --> PAGE
    PULSE -.-> USE
    TR <--> SPK
    PAGE -. "orateur actif affiché (4 fois/s)" .-> SPK

    classDef sink fill:#FCE4D6,stroke:#C55A11,color:#000
    classDef wk fill:#E2F0D9,stroke:#548235,color:#000
    class MO,UI,VM sink
    class BR,REC,TR,SPK wk
```

- Ta voix arrive par un flux séparé (`user_in`) : elle est toujours attribuée à « Moi » sans erreur.
- Le son de la réunion (`meeting_out`) mélange tous les autres : c'est là que les empreintes vocales
  séparent les personnes, et que les noms affichés par la plateforme leur donnent un nom.

---

## 3. Déroulé d'une réunion

```mermaid
sequenceDiagram
    autonumber
    actor V as Toi (IHM)
    participant C as ctl
    participant W as worker
    participant R as Webconf
    participant O as OVH AI

    V->>C: lien + nom + durée max (« Lancer » ou « Planifier »)
    C->>W: POST /join (worker libre)
    W->>R: ouvre Chrome, cookies, nom, code, « Rejoindre »
    R-->>W: salle d'attente puis réunion
    W->>W: ⏺ ffmpeg enregistre · transcription démarre
    V->>W: 🎧 Rejoindre l'audio (WebRTC, signalisation via ctl)

    loop toute la réunion
        W->>O: chaque phrase → Whisper
        W->>O: toutes les ~20 s → Qwen 27B (mise en forme)
        W->>O: toutes les 5 min → Qwen 27B (🧠 point d'étape)
        C-->>V: carte : aperçu, REC, transcription, points d'étape
    end

    alt fin détectée, bouton Quitter ou durée max
        W->>R: quitte la réunion
    end
    W->>W: MP4 + transcript.json + .md
    W->>O: 📋 compte rendu (3 rédacteurs, arbitre, contrôleur)
    W->>W: report.json (Word / Markdown à la demande)
    V->>C: 📁 Enregistrements → ▶ vidéo, 📝, 🧠, 📋 CR
```

---

## 4. De la voix au texte : transcription et intervenants

```mermaid
flowchart TB
    A["Flux audio<br/>(meeting_out et user_in)"] --> B["Découpage sur les silences<br/>(VAD par énergie)"]
    B --> C["Whisper large v3 (OVH)<br/>texte brut en 1 à 2 s<br/><i>affiché en italique</i>"]
    B --> D["Empreinte vocale<br/>WeSpeaker ResNet34 (ONNX, CPU)"]
    N["Orateur actif affiché<br/>par Teams / Zoom / Jitsi"] --> E
    D --> E{"Qui parle ?"}
    E -- "flux user_in" --> M["Moi"]
    E -- "voix + nom affiché" --> P["Alice, Bruno…"]
    E -- "voix sans nom" --> I["Intervenant 1, 2…"]
    E -- "2 voix, 1 appareil" --> S["Salle Nord, Salle Nord (2)"]
    C --> F["Toutes les ~20 s : Qwen 27B<br/>ponctuation, hésitations, glossaire<br/>(ne résume rien)"]
    M & P & I & S --> F
    F --> G["Transcription mise en forme<br/>paragraphes par intervenant"]
    G --> H["✏️ renommage possible<br/>pendant ou après"]
```

---

## 5. Compte rendu multi-modèles

```mermaid
flowchart TB
    T["Transcription complète<br/>(intervenants notés {S1}, {S2}, {ME})"]

    subgraph ETAPE1["1 · Trois rédactions indépendantes, en parallèle"]
        direction LR
        D1["Qwen3.8-27B"]
        D2["Qwen3.5-397B"]
        D3["GPT-OSS-120B"]
    end

    J["2 · Arbitre : Qwen3.8-27B (réflexion)<br/>confronte les 3 versions point par point<br/>revient à la transcription en cas de divergence<br/>garde l'<b>union</b> de ce qui est étayé"]
    Q["3 · Contrôleur : Qwen3.5-397B<br/>revérifie chaque élément, cherche les oublis<br/>→ liste de corrections appliquées par le programme"]
    DT["4 · Contrôle des dates par programme<br/>jour de semaine ↔ date, « vendredi » → date réelle<br/>incohérences → « Points à vérifier »"]
    ST["Temps de parole<br/>(calculé, pas par un LLM)"]
    CR[("report.json")]
    OUT["📋 IHM · Word · Markdown<br/>noms réels substitués à l'affichage"]

    T --> D1 & D2 & D3
    D1 & D2 & D3 --> J
    T -.-> J
    J --> Q
    T -.-> Q
    Q --> DT --> CR
    T --> ST --> CR
    CR --> OUT
```

**Contenu du compte rendu** : objet, synthèse, à retenir, tableau des décisions, tableau des actions,
« Pour moi » (ce qui te concerne), déroulé par sujet (positions de chacun, conclusion), questions ouvertes, risques,
chiffres, dates, prochaine réunion, points à vérifier, temps de parole, annexe « méthode et contrôle ».

---

## 6. Fichiers produits

Dans `/srv/webconf/recordings/` sur le worker :

```mermaid
flowchart LR
    S(["AAAAMMJJ-HHMMSS_titre"]) --> MP4[".mp4<br/>vidéo 1920x1200 · 2 i/s<br/>son AAC 48 kHz"]
    S --> TJ[".transcript.json<br/>texte brut + mis en forme<br/>intervenants · points d'étape"]
    S --> MD[".md<br/>transcription lisible"]
    S --> RJ[".report.json<br/>compte rendu + traces<br/>(3 brouillons, arbitrages, corrections)"]
    RJ -. "à la demande" .-> DOCX["compte rendu.docx"]
    RJ -. "à la demande" .-> CRMD["compte rendu.md"]
    S --> SL[".slide-NNN.jpg<br/>captures de l'écran partagé"]
    S --> FIN[".cr-final.md / .pdf<br/>compte rendu final<br/>(déposé par Claude, MCP)"]
```

## 6 bis. Accès de Claude (serveur MCP)

```mermaid
flowchart LR
    CC["Claude Code<br/>(poste Windows)"] -- "POST /mcp<br/>Bearer jeton (lecture)" --> CTL["IHM (contrôle)<br/>mcp_server.py"]
    CTL -- "X-Agent-Token" --> W["workers<br/>/recordings, /transcript…"]
    CC -- "lien signé temporaire<br/>(audio, captures)" --> CTL
```

Le serveur est sans état (réponses JSON, versions du protocole 2024-11-05 à 2026-07-28). Il transforme les
fichiers de la réunion (transcription, compte rendu, diapositives) en données prêtes à rédiger : locuteur résolu
par phrase (nom affiché par la plateforme > voix reconnue > « Intervenant N »), participants et organisations,
segments suspects, séquences hors réunion, indicateur de captation du micro de l'utilisateur.

---

## 7. Variante : workers en conteneurs

Les workers peuvent tourner en conteneurs Docker sur une VM hôte (`deploy/deploy-containers.sh`).
Le contrôle et l'IHM ne changent pas : il suffit de déclarer les conteneurs dans `WORKERS`.

```mermaid
flowchart LR
    CTL["<b>webconf-ctl</b><br/>IHM + API"]
    TOI["🎧 ton navigateur"]

    subgraph HOST["VM hôte Docker · ex. webconf-ct 192.168.0.233 · réseau de l'hôte"]
        direction TB
        C3["<b>conteneur w3</b><br/>API :8001 · VNC :6080 · écran :99<br/>Chrome · Xvfb · PulseAudio · ffmpeg · agent"]
        C4["<b>conteneur w4</b><br/>API :8002 · VNC :6081 · écran :100<br/>(identique)"]
        V3[("/srv/webconf/w3<br/>enregistrements, profil Chrome")]
        V4[("/srv/webconf/w4")]
        C3 --- V3
        C4 --- V4
    end

    CTL -- "API + jeton, VNC (port annoncé par le worker)" --> C3
    CTL --> C4
    TOI <-. "audio WebRTC direct (UDP)" .-> C3
    C3 <--> WEB["Webconf"]
    C4 <--> WEB

    classDef ctl fill:#DCE6F0,stroke:#2F5597,color:#000
    classDef wk fill:#E2F0D9,stroke:#548235,color:#000
    class CTL ctl
    class C3,C4 wk
```

- Dans chaque conteneur, les 5 services d'une VM worker tournent sous `supervisord`.
- Les conteneurs utilisent le réseau de l'hôte : chacun a donc ses propres ports et son propre écran.
- Un conteneur = une réunion à la fois, comme une VM.
