"""Lexique de l'installation : personnes, corrections de transcription, sigles, registre des sujets.

Fichier Markdown modifiable dans l'IHM (le contrôle le pousse à chaque assistant). Sections reconnues par leur
titre (« ## 1. PERSONNES », « 2. CORRECTIONS DE TRANSCRIPTION », « 3. SIGLES ET TERMES », « 4. REGISTRE … ») :

- PERSONNES : lignes « - Prénom Nom : rôle [variantes entendues] », ou listes « - Groupe : Nom (rôle) ; Nom… » ;
- CORRECTIONS : « entendu, autre forme → correct ; … » ;
- SIGLES / REGISTRE : texte libre, transmis tel quel aux modèles.

Sert à :
- Whisper : prompt court (≈ 224 jetons au plus) avec les noms des participants et les termes difficiles ;
- la mise en forme (Qwen) : table des corrections, appliquée selon le contexte (le texte brut de Whisper est
  conservé tel quel dans la transcription, pour la traçabilité) ;
- les noms des intervenants : casse, noms tronqués par la plateforme (« Camille Dur… »), variantes ;
- le compte rendu : personnes, sigles et registre des sujets donnés aux modèles.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PATH = Path(os.environ.get("LEXICON_FILE", "/srv/webconf/lexicon.md"))
WHISPER_CHARS = int(os.environ.get("LEXICON_WHISPER_CHARS", "650"))   # ≈ 200 jetons
ARROW = re.compile(r"\s*(?:→|->|=>)\s*")
# « Prénom Nom (Société / Ville) », « Prénom Nom - Société »
ORG = re.compile(r"^(?P<name>.+?)\s*(?:[(\[](?P<org>[^()\[\]]{2,60})[)\]]|\s[-–|]\s(?P<org2>.{2,60}))\s*$")


def norm(s: str) -> str:
    """Comparaison des noms : sans accents, casse ni ponctuation."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def proper_case(n: str) -> str:
    """« CAMILLE DURAND » ou « camille durand » -> casse de nom propre ; « Jean-Marc DUPONT » -> « Jean-Marc Dupont »."""
    out = []
    for w in (n or "").split():
        if (w.isupper() and len(w) > 1) or w.islower():
            w = "-".join(x[:1].upper() + x[1:].lower() for x in w.split("-"))
        out.append(w)
    return " ".join(out)


def _split_top(s: str, seps=(",", ";", " et "), quotes: bool = False) -> list[str]:
    """Découpe sur les séparateurs hors parenthèses et crochets (et hors « guillemets » si quotes)."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(s):
        c = s[i]
        if c in "([" or (quotes and c == "«"):
            depth += 1
        elif c in ")]" or (quotes and c == "»"):
            depth = max(0, depth - 1)
        if depth == 0:
            sep = next((x for x in seps if s.startswith(x, i)), None)
            if sep:
                out.append(cur)
                cur, i = "", i + len(sep)
                continue
        cur += c
        i += 1
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


_NAME_WORD = r"(?:[A-ZÀ-ÖØ-Þ][\w'’.-]*|d['’][A-ZÀ-ÖØ-Þ][\w'’.-]*|de|De|du|Du|d'|N'|El|Ben|von|van|Le|La)"
LOOKS_NAME = re.compile(rf"^{_NAME_WORD}(?:\s+{_NAME_WORD}){{1,4}}$")


def _looks_person(s: str) -> bool:
    s = s.strip()
    if not LOOKS_NAME.match(s):
        return False
    words = s.split()
    # sigles (« SI Enrôlement », « Projet IA », « COTECH ») : pas une personne
    return not any(len(w) > 1 and w.isupper() for w in words)


@dataclass
class Person:
    name: str
    role: str = ""
    group: str = ""
    variants: list[str] = field(default_factory=list)

    def line(self) -> str:
        extra = ", ".join(x for x in (self.group, self.role) if x)
        return self.name + (f" ({extra})" if extra else "")


def _display_form(v: str) -> bool:
    """Variante qui peut être un nom affiché par la plateforme (et non une erreur de l'oral)."""
    v = v.strip()
    return " " in v or "," in v or v.endswith(("…", "..."))


def _close(a: str, b: str, p: "Person") -> bool:
    """Mot entier d'un nom tronqué : identique, variante connue (« Pominique » pour « Dominique »), ou une lettre près (lecture d'image)."""
    if a == b or any(norm(v) == a for v in p.variants):
        return True
    return len(a) >= 5 and len(a) == len(b) and sum(x != y for x, y in zip(a, b)) <= 1


class Lexicon:
    def __init__(self, text: str = ""):
        self.text = text or ""
        self.version = hashlib.sha1(self.text.encode()).hexdigest()[:12] if self.text.strip() else ""
        self.sections: dict[str, str] = {}
        self.people: list[Person] = []
        self.corrections: list[tuple[list[str], str]] = []   # ([formes entendues], forme correcte)
        self._parse()

    # ------------------------------------------------------------------ lecture
    def _parse(self):
        cur, buf = "intro", []
        head = re.compile(r"^\s*(?:#{1,4}\s*)?(\d+)\s*[.)-]\s*(.+?)\s*#*\s*$")
        for line in self.text.splitlines():
            m = head.match(line)
            if m and m.group(2).strip()[:1].isalpha() and not line.lstrip().startswith("-"):
                self.sections[cur] = "\n".join(buf).strip()
                t = norm(m.group(2))
                cur = ("people" if "personne" in t or "participant" in t else
                       "corrections" if "correction" in t or "transcription" in t else
                       "registry" if "registre" in t or "sujet" in t else
                       "terms" if "sigle" in t or "terme" in t or "glossaire" in t else t)
                buf = []
            else:
                buf.append(line)
        self.sections[cur] = "\n".join(buf).strip()
        self._people(self.sections.get("people", ""))
        self._corrections(self.sections.get("corrections", ""))

    def _people(self, text: str):
        seen = set()
        group = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if not line.startswith(("-", "*")):           # titre de groupe : « Équipe X : », « Partenaires (A, B) : »
                group = re.sub(r"\s*:\s*$", "", line).strip()
                continue
            line = line.lstrip("-* ").strip()
            label = ""                                   # « Architectes : A (…) ; B ; C » : vaut pour toute la ligne
            for piece in _split_top(line, (" ; ",)):
                items = None
                if " : " in piece:
                    left, right = piece.split(" : ", 1)
                    head_right = re.split(r"\s*[(\[]", _split_top(right)[0] if right else "")[0]
                    if _looks_person(head_right):           # « Groupe : Nom, Nom » (le rôle n'est jamais un nom)
                        label, items = left.strip(), _split_top(right)
                    else:                                   # « Nom : rôle, précisions [variantes] »
                        items = [piece]
                else:
                    items = _split_top(piece)
                for item in items:
                    m = re.match(r"^(?P<n>[^(\[:]+?)\s*(?::\s*(?P<r1>[^\[]+?))?\s*(?:\((?P<r2>[^)]*)\))?\s*"
                                 r"(?:\[(?P<v>[^\]]*)\])?\s*$", item)
                    if not m:
                        continue
                    name = m.group("n").strip()
                    role = (m.group("r1") or m.group("r2") or "").strip()
                    variants = [re.sub(r"^vu\s*:\s*", "", v).strip(" «»\"") for v in
                                _split_top(m.group("v") or "", (",",), quotes=True) if v.strip(" «»\"")]
                    if not _looks_person(name) or norm(name) in seen:
                        continue
                    seen.add(norm(name))
                    self.people.append(Person(name, role, label or group, variants))

    def _corrections(self, text: str):
        for piece in re.split(r"\s*;\s*|\n", text):
            parts = ARROW.split(piece.strip().lstrip("-* "), maxsplit=1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                continue
            heard = _split_top(parts[0], (",",))
            right = parts[1].strip().rstrip(".")
            rights = _split_top(right, (",",))
            if len(rights) == len(heard) > 1:          # « comité des ADT, ADA → Comité DA/DT, DA »
                for h, r in zip(heard, rights):
                    self.corrections.append(([h], r))
            else:
                self.corrections.append((heard, right))

    # ------------------------------------------------------------------ noms
    def canonical(self, name: str | None) -> str | None:
        """Nom affiché par la plateforme -> nom du lexique (casse, accents, variantes, nom tronqué « … »).
        L'organisation affichée (« - Société », « (Société / Ville) ») est gardée telle quelle.
        Nom inconnu : rendu en casse de nom propre. None si vide."""
        if not name or not name.strip():
            return name
        m = ORG.match(name.strip())
        if m and not re.search(r"(…|\.\.\.)\s*$", m.group("name")):
            base, org = m.group("name").strip(), (m.group("org") or m.group("org2")).strip()
            if not re.fullmatch(r"\d+", org):
                b = self._canonical(base)
                return f"{b} ({org})" if m.group("org") else f"{b} - {org}"
        return self._canonical(name)

    def _canonical(self, name: str) -> str:
        n = name.strip()
        for p in self.people:   # forme affichée connue telle quelle (« cdu… », « Durand, Camille »)
            if any(_display_form(v) and norm(v) == norm(n) and v.endswith("…") == n.endswith(("…", "...")) for v in p.variants):
                return p.name
        if "," in n:   # annuaire « Nom, Prénom » -> « Prénom Nom »
            last, first = (x.strip() for x in n.split(",", 1))
            if last and first:
                n = f"{first} {last}"
        truncated = bool(re.search(r"(…|\.\.\.)", n))
        n = re.sub(r"\s*(…|\.\.\.)\s*", " ", n).strip() if truncated else n
        key = norm(n)
        if not key:
            return name
        for p in self.people:
            if norm(p.name) == key or any(norm(v) == key for v in p.variants if _display_form(v)):
                return p.name   # variantes d'un seul mot (« Gilles ») : erreurs de l'oral, pas des noms affichés
        words = key.split()
        cands = []
        for p in self.people:
            pw = norm(p.name).split()
            if truncated and len(words) <= len(pw):
                # « Camille Dur… » ou « V… Durand » (annuaire « Durand, V… ») : chaque mot est le mot entier,
                # à une lettre près, ou le début du mot (lettre tronquée)
                # (« Stéphane … » : le reste est coupé, le prénom suffit s'il est unique)
                if all(b.startswith(a) or _close(a, b, p) for a, b in zip(words, pw)) and \
                        (any(b.startswith(a) and a != b for a, b in zip(words, pw)) or len(words) < len(pw)):
                    cands.append(p)
            elif len(words) >= 3 and len(pw) >= 2 and words[0] == pw[0] and words[-1] == pw[-1]:
                # « Camille Marie dit Kim Durand » -> « Camille Durand »
                cands.append(p)
        if len(cands) == 1:
            return cands[0].name
        if not cands and len(words) == 2 and truncated:   # ordre « Nom P… » (annuaire sans virgule)
            rev = [p for p in self.people if len(norm(p.name).split()) == 2
                   and norm(p.name).split()[1] == words[0] and norm(p.name).split()[0].startswith(words[1])]
            if len(rev) == 1:
                return rev[0].name
        return proper_case(n) + ("…" if truncated else "")

    def known(self, name: str) -> bool:
        k = norm(name)
        return any(norm(p.name) == k or any(norm(v) == k for v in p.variants) for p in self.people)

    # ------------------------------------------------------------------ prompts
    def whisper_prompt(self, names=(), extra: str = "", limit: int = WHISPER_CHARS) -> str:
        """Prompt initial de Whisper : participants vus, puis termes souvent mal reconnus (formes correctes)."""
        out, used = [], 0
        items = [self.canonical(n) or n for n in names if n]
        items += [r for _, r in self.corrections if " ou " not in r]
        items += [x.strip() for x in extra.split(",") if x.strip()]
        seen = set()
        for it in items:
            it = re.sub(r"\s*\(.*?\)", "", it).strip()
            k = norm(it)
            if not k or k in seen or len(it.split()) > 4:
                continue
            if used + len(it) + 2 > limit:
                break
            seen.add(k)
            out.append(it)
            used += len(it) + 2
        return ", ".join(out)

    def corrections_text(self) -> str:
        return "\n".join(f"- {', '.join(h)} → {r}" for h, r in self.corrections)

    def llm_block(self, people_limit: int = 400) -> str:
        """Bloc donné aux modèles du compte rendu : personnes, sigles, registre des sujets."""
        if not self.version:
            return ""
        parts = []
        if self.sections.get("people") or self.people:
            parts.append("Personnes connues (orthographe de référence, rôle, [variantes entendues]) :\n" +
                         (self.sections.get("people") or "\n".join(f"- {p.line()}" for p in self.people[:people_limit])))
        if self.corrections:
            parts.append("Mots souvent mal reconnus par la transcription (entendu → correct) :\n" + self.corrections_text())
        if self.sections.get("terms"):
            parts.append("Sigles et termes du domaine :\n" + self.sections["terms"])
        if self.sections.get("registry"):
            parts.append("Registre des sujets suivis (état connu avant la réunion ; « le 120 », « la 134 » "
                         "désignent ces numéros) :\n" + self.sections["registry"])
        return "\n\n".join(parts)


_cache: tuple[float, Lexicon] | None = None


def load(path: Path | None = None) -> Lexicon:
    """Lexique courant (relu si le fichier a changé)."""
    global _cache
    p = path or PATH
    try:
        mt = p.stat().st_mtime
    except OSError:
        return Lexicon("")
    if _cache is None or _cache[0] != mt or path is not None:
        lex = Lexicon(p.read_text(encoding="utf-8", errors="replace"))
        if path is not None:
            return lex
        _cache = (mt, lex)
    return _cache[1]


def save(text: str, path: Path | None = None) -> Lexicon:
    p = path or PATH
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return load()
