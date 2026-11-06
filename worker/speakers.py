"""Distinction des interlocuteurs (diarisation en ligne) pour le flux « réunion ».

Deux indices complémentaires :
1. **Empreinte vocale** : un embedding (3D-Speaker CAM++ zh/en, ONNX, CPU, ~30 ms par phrase) est
   calculé pour chaque phrase transcrite, puis regroupé en ligne par similarité cosinus → « Intervenant
   1, 2… ». Le son d'une visio (Opus, réduction de bruit, même salle) donne à toutes les voix une
   « signature de canal » commune : on la retire en soustrayant l'empreinte moyenne de la séance
   avant de comparer (sans cela, toutes les voix se ressemblent : similarité ~0,8 entre personnes
   différentes avec l'ancien modèle WeSpeaker ResNet34, d'où un seul « Intervenant 1 »).
   Fonctionne sur toutes les plateformes, et distingue aussi plusieurs personnes derrière un même micro.
2. **Orateur actif affiché par la plateforme** (Teams/Zoom/Jitsi/Meet) : relevé ~4 fois par seconde
   dans la page. Chaque phrase vote pour le nom affiché pendant qu'elle était prononcée → les voix
   reçoivent les vrais noms.
Le nom saisi à la main dans l'IHM prime toujours.

On préfère créer trop de groupes : deux groupes trop proches sont fusionnés ensuite (la fusion est
un simple réétiquetage), alors qu'une voix mélangée à une autre ne peut plus être séparée.
"""
import bisect
import logging
import os
from collections import Counter

import numpy as np

log = logging.getLogger("speakers")

MODEL = os.environ.get("TRANSCRIPT_SPK_MODEL",
                       "/opt/webconf/models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
# Seuils sur empreintes centrées (moyenne de la séance retirée), réglés sur deux vraies réunions
# (Teams, Meet) et une réunion de synthèse à 4 voix passée par Opus : tests/diar_real_eval.py
NEW_T = float(os.environ.get("TRANSCRIPT_SPK_NEW", "0.23"))      # sous ce score : nouvelle voix
MERGE_T = float(os.environ.get("TRANSCRIPT_SPK_MERGE", "0.48"))  # au-dessus : deux groupes fusionnés
NAME_MERGE_T = float(os.environ.get("TRANSCRIPT_SPK_NAME_MERGE", "0.12"))  # même nom affiché + voix assez proches
WARMUP = 8                # empreintes avant de retirer la moyenne de séance (trop bruitée avant)
MIN_EMB_SEC = 1.0         # en dessous, l'empreinte est trop bruitée : pas de nouveau groupe
RATE = 16000


class Diarizer:
    def __init__(self, bot_names: tuple = ()):
        self.extractor = None
        self.error = None
        if os.path.exists(MODEL):
            try:
                import sherpa_onnx
                self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                    sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2))
            except Exception as e:  # pragma: no cover
                self.error = f"modèle d'empreinte vocale indisponible : {e}"
                log.warning(self.error)
        else:
            self.error = "modèle d'empreinte vocale absent (voix non distinguées)"
        self.bot_names = {n.strip().lower() for n in bot_names if n}
        self.reset()

    def reset(self):
        self.spk: dict[str, dict] = {}   # id -> {sum, n, votes: Counter, name, num}
        self.counter = 0
        self.ui: list[tuple[float, tuple]] = []   # (epoch, noms affichés comme parlant)
        self.ui_t: list[float] = []
        self.last_id = None
        self.aliases: dict[str, str] = {}  # id fusionné -> id survivant
        self.mu_sum = None   # somme des empreintes de la séance (signature du canal)
        self.mu_n = 0

    def _mu(self):
        return self.mu_sum / self.mu_n if self.mu_n >= WARMUP else None

    @staticmethod
    def _unit(v):
        n = np.linalg.norm(v)
        return v / n if n else v

    # ------------------------------------------------------------------ orateur actif (page web)
    def ui_sample(self, ts: float, names):
        names = tuple(n for n in (names or ()) if n and n.strip().lower() not in self.bot_names)
        self.ui.append((ts, names))
        self.ui_t.append(ts)
        if len(self.ui) > 20000:  # ~80 min d'historique suffit largement
            self.ui, self.ui_t = self.ui[-15000:], self.ui_t[-15000:]

    def ui_name(self, t_start: float, t_end: float) -> str | None:
        """Nom affiché comme orateur pendant la phrase (majorité des relevés non vides).

        L'indicateur des plateformes réagit avec ~0,5-1 s de retard, et Jitsi garde l'orateur
        précédent affiché pendant les silences : on décale la fenêtre et on ignore le premier quart."""
        dur = max(0.0, t_end - t_start)
        a, b = t_start + min(0.8, dur * 0.25), t_end + 0.8
        i, j = bisect.bisect_left(self.ui_t, a), bisect.bisect_right(self.ui_t, b)
        samples = [n for _, n in self.ui[i:j]]
        named = [n for n in samples if n]
        if not samples or len(named) < 0.3 * len(samples):
            return None
        c = Counter(x for n in named for x in n)
        name, k = c.most_common(1)[0]
        return name if k >= 0.6 * len(named) else None

    @property
    def ui_active(self) -> bool:
        return any(n for _, n in self.ui[-400:])

    # ------------------------------------------------------------------ empreintes
    def embed(self, pcm: np.ndarray) -> np.ndarray | None:
        if self.extractor is None or len(pcm) < MIN_EMB_SEC * RATE:
            return None
        s = self.extractor.create_stream()
        s.accept_waveform(RATE, pcm.astype(np.float32) / 32768)
        s.input_finished()
        e = np.array(self.extractor.compute(s), dtype=np.float32)
        n = np.linalg.norm(e)
        return e / n if n else None

    def _centroid(self, sid):
        """Centroïde d'un groupe, débarrassé de la signature commune du canal."""
        v = self.spk[sid]
        c, mu = v["sum"] / max(1, v["n"]), self._mu()
        return self._unit(c if mu is None else c - mu)

    def _center(self, emb):
        mu = self._mu()
        return self._unit(emb if mu is None else emb - mu)

    def _new(self) -> str:
        self.counter += 1
        sid = f"S{self.counter}"
        self.spk[sid] = {"sum": None, "n": 0, "votes": Counter(), "name": None, "num": self.counter}
        return sid

    def assign(self, emb: np.ndarray | None, dur: float, ui_name: str | None) -> str:
        """Attribue un intervenant à une phrase (empreinte + nom affiché)."""
        sid = None
        ce = self._center(emb) if emb is not None else None
        scored = [(float(ce @ self._centroid(k)), k) for k, v in self.spk.items()
                  if emb is not None and v["sum"] is not None]
        if ui_name:
            # une même voix ne parle pas depuis deux appareils : on écarte les voix déjà associées
            # à un autre nom affiché par la plateforme
            scored = [(sc, k) for sc, k in scored if self._voted(self.spk[k]) in (None, ui_name)]
        best, bid = max(scored) if scored else (-1.0, None)
        if emb is not None:
            if bid and best >= NEW_T:
                sid = bid
            elif ui_name and (k := self._by_name(ui_name)) and \
                    (self.spk[k]["sum"] is None or float(ce @ self._centroid(k)) >= NAME_MERGE_T):
                sid = k  # voix un peu différente (micro, ton) mais même orateur affiché
            else:
                sid = self._new()
            v = self.spk[sid]
            v["sum"] = emb.copy() if v["sum"] is None else v["sum"] + emb
            v["n"] += 1
            self.mu_sum = emb.copy() if self.mu_sum is None else self.mu_sum + emb
            self.mu_n += 1
        else:  # phrase trop courte ou pas de modèle : nom affiché, sinon meilleure voix, sinon précédent
            if ui_name:
                sid = self._by_name(ui_name)
                if sid is None and self.extractor is None:
                    sid = self._new()
            if sid is None and bid and best >= NEW_T:
                sid = bid
            if sid is None:
                sid = self.last_id if self.last_id in self.spk else (next(iter(self.spk), None) or self._new())
        if ui_name:
            self.spk[sid]["votes"][ui_name] += max(1.0, dur)
        self.last_id = sid
        return sid

    def _by_name(self, name: str) -> str | None:
        best = None
        for k, v in self.spk.items():
            if v["name"] == name or self._voted(v) == name:
                if best is None or self.spk[best]["n"] < v["n"]:
                    best = k
        return best

    @staticmethod
    def _top(v) -> str | None:
        return v["votes"].most_common(1)[0][0] if v["votes"] else None

    @staticmethod
    def _voted(v) -> str | None:
        if not v["votes"]:
            return None
        name, w = v["votes"].most_common(1)[0]
        return name if w >= 2 and w >= 0.6 * sum(v["votes"].values()) else None

    # ------------------------------------------------------------------ fusion / noms
    def merge_pass(self) -> dict[str, str]:
        """Fusionne les groupes trop proches. Renvoie {ancien_id: nouvel_id} pour réétiqueter."""
        remap = {}
        changed = True
        while changed:
            changed = False
            ids = [k for k, v in self.spk.items() if v["sum"] is not None]
            best = None
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    sim = float(self._centroid(a) @ self._centroid(b))
                    na, nb = self._label_auto(a), self._label_auto(b)
                    same_name = na and na == nb
                    manual_diff = self.spk[a]["name"] and self.spk[b]["name"] and self.spk[a]["name"] != self.spk[b]["name"]
                    # deux voix rattachées à deux noms affichés différents = deux appareils = deux personnes
                    # (votes bruts : la fusion est irréversible, on n'attend pas que le nom soit confirmé)
                    ta, tb = self._top(self.spk[a]), self._top(self.spk[b])
                    ui_diff = (na and nb and na != nb) or (ta and tb and ta != tb)
                    if manual_diff or ui_diff:
                        continue
                    if sim >= MERGE_T or (same_name and sim >= NAME_MERGE_T):
                        if best is None or sim > best[0]:
                            best = (sim, a, b)
            if best:
                _, a, b = best
                keep, drop = (a, b) if self.spk[a]["n"] >= self.spk[b]["n"] else (b, a)
                k, d = self.spk[keep], self.spk.pop(drop)
                k["sum"] = k["sum"] + d["sum"]
                k["n"] += d["n"]
                k["votes"].update(d["votes"])
                k["name"] = k["name"] or d["name"]
                k["num"] = min(k["num"], d["num"])
                remap[drop] = keep
                self.aliases[drop] = keep
                if self.last_id == drop:
                    self.last_id = keep
                log.info("fusion %s -> %s (sim %.2f)", drop, keep, best[0])
                changed = True
        # chaînes de fusion : a -> b -> c
        for k in list(remap):
            while remap[k] in remap:
                remap[k] = remap[remap[k]]
        return remap

    def resolve(self, sid: str) -> str:
        while sid in self.aliases:
            sid = self.aliases[sid]
        return sid

    def _label_auto(self, sid):
        v = self.spk.get(sid)
        return (v["name"] or self._voted(v)) if v else None

    def rename(self, sid: str, name: str | None) -> bool:
        sid = self.resolve(sid)
        if sid not in self.spk:
            return False
        self.spk[sid]["name"] = (name or "").strip() or None
        return True

    def labels(self) -> dict[str, dict]:
        """id -> {label, color, source} ; numérotation 1..n dans l'ordre d'apparition."""
        out, seen = {}, Counter()
        order = sorted(self.spk.items(), key=lambda kv: kv[1]["num"])
        for idx, (k, v) in enumerate(order, 1):
            auto = self._voted(v) or f"Intervenant {idx}"
            label = v["name"] or auto
            src = "manual" if v["name"] else "ui" if self._voted(v) else "voice"
            seen[label] += 1
            if seen[label] > 1:  # plusieurs voix derrière le même nom affiché (salle de réunion…)
                label = f"{label} ({seen[label]})"
            out[k] = {"label": label, "auto": auto, "name": v["name"], "color": idx, "source": src}
        return out

    def state(self) -> dict:
        """Pour la sauvegarde JSON (relecture / renommage après coup)."""
        return {k: {"name": v["name"], "votes": dict(v["votes"]), "n": v["n"], "num": v["num"]}
                for k, v in self.spk.items()}
