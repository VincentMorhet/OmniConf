"""Génère une transcription de réunion fictive (format <base>.transcript.json) pour tester l'analyse
et le compte rendu. Contient des pièges : décision modifiée en cours de réunion, action sans échéance,
chiffres, question adressée à « Moi », passage inaudible, mot mal reconnu."""
import json, sys

S = {"S1": "Alice Martin", "S2": "Bruno Petit", "S3": "Salle Nord", "S4": "Salle Nord (2)", "ME": "Moi"}
lines = [
 ("00:05", "S1", "Bonjour à tous, merci d'être là. L'objet de la réunion c'est le point d'avancement sur le déploiement du nouveau portail client dans les agences, et la préparation du comité de pilotage du 12 décembre."),
 ("00:21", "S2", "Bonjour. Côté technique on a terminé la recette fonctionnelle la semaine dernière, sur 214 cas de test on en a 197 qui passent, donc il reste 17 anomalies dont 3 bloquantes."),
 ("00:40", "S1", "Les 3 bloquantes, c'est quoi exactement ?"),
 ("00:44", "S2", "La première c'est l'authentification par carte à puce qui échoue sur les postes Windows 11 en mode kiosque. La deuxième c'est la synchronisation avec le CRM quand le client a un identifiant non vérifié, on a un rejet silencieux. Et la troisième c'est un problème de performance, l'envoi des documents de plus de 5 mégas prend plus de 40 secondes."),
 ("01:15", "S3", "Pour l'identifiant non vérifié, on a eu le même retour de l'agence de Lille. Il faudrait que l'interface affiche un message clair au conseiller plutôt qu'un rejet silencieux."),
 ("01:30", "S1", "D'accord. Camille, est-ce que tu peux nous dire où on en est côté certification de sécurité ?"),
 ("01:37", "ME", "Oui, le dossier de certification a été déposé le 3 novembre. On attend un retour sous six semaines en théorie, donc mi-décembre. Par contre ils nous ont demandé une pièce complémentaire, l'analyse de risques RGPD mise à jour, que je n'ai pas encore."),
 ("02:05", "S4", "L'analyse de risques c'est nous qui l'avons, on peut te l'envoyer d'ici vendredi."),
 ("02:12", "ME", "Parfait, merci."),
 ("02:15", "S1", "Bon. Sur le calendrier, initialement on visait une mise en production le 15 janvier pour la première vague de 12 agences."),
 ("02:28", "S2", "Honnêtement, avec 3 bloquantes et la certification pas encore obtenue, le 15 janvier c'est très risqué. Je proposerais plutôt le 2 février."),
 ("02:41", "S3", "Nous on a des agences qui sont prêts et qui attendent, ça fait déjà deux reports. Si on repousse encore, on risque de perdre l'adhésion des équipes terrain."),
 ("02:58", "S1", "Je comprends. Proposition : on maintient le 15 janvier mais seulement pour 4 agences pilotes, et la vague complète des 12 agences passe au 2 février. Ça vous va ?"),
 ("03:12", "S2", "Ça me va si les 3 bloquantes sont corrigées avant le 20 décembre."),
 ("03:18", "S3", "Oui pour nous c'est acceptable."),
 ("03:21", "S1", "Donc c'est acté : 4 agences pilotes au 15 janvier, généralisation aux 12 agences le 2 février."),
 ("03:30", "S2", "Pour la correction des bloquantes, je mets deux développeurs dessus dès lundi. Pour la carte à puce je dois quand même vérifier avec l'éditeur du middleware, ça dépend de leur version."),
 ("03:48", "S1", "Tu peux me faire un point écrit sur les bloquantes d'ici mercredi prochain ?"),
 ("03:53", "S2", "Oui, mercredi prochain."),
 ("03:56", "S4", "Et pour la formation des conseillers, on avait prévu des sessions en visio. Il nous faudrait le support de formation finalisé."),
 ("04:08", "S1", "Le support, c'est Bruno ou c'est la MOA ?"),
 ("04:12", "S2", "C'est la MOA normalement, mais je peux fournir les captures d'écran."),
 ("04:18", "S1", "OK, je relance la MOA. Personne ne prend de date pour l'instant, on verra au copil."),
 ("04:26", "S3", "Il y a aussi la question du budget. On avait 180 000 euros pour la phase pilote, et on a déjà consommé 142 000. Avec le décalage on va dépasser."),
 ("04:43", "S1", "De combien à peu près ?"),
 ("04:46", "S3", "On estime le dépassement à 25 000 euros environ, essentiellement de la régie développement."),
 ("04:55", "S1", "Ça il faudra le présenter au copil, je ne peux pas le valider seule. Camille, tu peux préparer une note sur le dépassement budgétaire pour le copil ?"),
 ("05:05", "ME", "Oui, je la prépare. Je la veux pour le 5 décembre pour qu'on ait le temps de la relire."),
 ("05:13", "S2", "Petite remarque, le serveur de pré-production [inaudible] la semaine du 25, il y aura une coupure d'une demi-journée."),
 ("05:25", "S1", "Il faudra prévenir les testeurs. Bruno tu t'en occupes ?"),
 ("05:29", "S2", "Oui."),
 ("05:31", "S4", "Dernier point de notre côté : est-ce qu'on a une réponse sur l'accès au portail pour les partenaires sous-traitants ? C'était une question ouverte depuis septembre."),
 ("05:44", "S1", "Non, pas encore, c'est au niveau de la direction juridique. Je n'ai pas de visibilité."),
 ("05:50", "ME", "Je peux poser la question à mon contact à la direction des achats, mais sans garantie."),
 ("05:56", "S1", "Merci. Bon, prochaine réunion de suivi le jeudi 28 novembre à 14 heures, même lien. Merci à tous."),
]
def sec(t):
    m, s = t.split(":"); return int(m) * 60 + int(s)
paras = [{"id": i + 1, "t": float(sec(t)), "spk": k, "text": x} for i, (t, k, x) in enumerate(lines)]
raw = []
for i, p in enumerate(paras):
    te = paras[i + 1]["t"] - 0.5 if i + 1 < len(paras) else p["t"] + 4
    raw.append({"t": p["t"], "te": te, "spk": p["spk"], "text": p["text"]})
speakers = {k: {"label": v, "auto": v, "name": None, "color": i, "source": "me" if k == "ME" else "ui"}
            for i, (k, v) in enumerate(S.items(), 1)}
speakers["ME"]["color"] = 0
doc = {"meta": {"title": "Point avancement portail client", "platform": "teams", "url": "https://teams.microsoft.com/l/meetup-join/x",
                "started_at": "2026-11-20T10:00:00+01:00", "ended_at": "2026-11-20T10:06:10+01:00"},
       "t0": 0, "speakers": speakers, "paragraphs": paras, "raw": raw}
json.dump(doc, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
