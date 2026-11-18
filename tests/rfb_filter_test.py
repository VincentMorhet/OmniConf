"""Écran en lecture seule (spectateur d'une réunion partagée) : le filtre RFB ne laisse passer que les demandes
d'images, quel que soit le découpage du flux en trames.

    python tests/rfb_filter_test.py
"""
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
from rfb_filter import RfbViolation, ViewOnlyFilter  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


HAND = b"RFB 003.008\n" + b"\x01" + b"\x00"                      # version, sécurité « aucune », ClientInit exclusif
PIXFMT = b"\x00\x00\x00\x00" + bytes(16)
ENC = b"\x02\x00" + struct.pack(">H", 3) + struct.pack(">iii", 0, 7, -223)
FBUR = b"\x03\x01" + struct.pack(">HHHH", 0, 0, 1920, 1200)
CONT = b"\x96\x01" + struct.pack(">HHHH", 0, 0, 1920, 1200)
FENCE = b"\xf8\x00\x00\x00" + struct.pack(">I", 3) + b"\x04" + b"abcd"
KEY = b"\x04\x01\x00\x00" + struct.pack(">I", 0x61)
PTR = b"\x05\x01" + struct.pack(">HH", 100, 200)
CUT = b"\x06\x00\x00\x00" + struct.pack(">i", 11) + b"mot secret!"
CUTX = b"\x06\x00\x00\x00" + struct.pack(">i", -8) + bytes(8)       # presse-papiers étendu
SIZE = b"\xfb\x00" + struct.pack(">HH", 800, 600) + b"\x01\x00" + bytes(16)
QKEY = b"\xff\x00" + struct.pack(">HII", 1, 0x61, 30)
stream = HAND + PIXFMT + ENC + FBUR + KEY + PTR + CONT + CUT + FENCE + SIZE + CUTX + QKEY + PTR + FBUR
want = b"RFB 003.008\n\x01\x01" + PIXFMT + ENC + FBUR + CONT + FENCE + FBUR

f = ViewOnlyFilter()
got = f.feed(stream)
check(got == want, "flux d'un coup : demandes d'images transmises, clavier, souris, presse-papiers et redimensionnement jetés")
check(f.dropped == 7, f"7 messages d'entrée jetés ({f.dropped})")
check(got[13:14] == b"\x01", "ClientInit forcé en partagé (le propriétaire n'est pas déconnecté)")

random.seed(7)
for trial in range(200):
    f, out, i = ViewOnlyFilter(), b"", 0
    while i < len(stream):
        n = random.randint(1, 9)
        out += f.feed(stream[i:i + n])
        i += n
    if out != want:
        break
check(out == want, f"200 découpages au hasard (1 à 9 octets par trame) : même résultat ({trial + 1} essais)")


def refused(data, what):
    try:
        ViewOnlyFilter().feed(data)
        check(False, what)
    except RfbViolation as e:
        check(True, f"{what} ({e})")


refused(b"RFB 003.003\n", "version 3.3 refusée")
refused(b"HTTP/1.1 200\n", "autre protocole refusé")
refused(b"RFB 003.008\n\x02", "sécurité par mot de passe refusée (échange qui ne serait pas filtré)")
refused(HAND + b"\x07" + bytes(20), "message inconnu : connexion fermée")
refused(HAND + b"\xff\x01" + bytes(10), "extension QEMU inconnue : connexion fermée")
refused(HAND + b"\x06\x00\x00\x00" + struct.pack(">i", 5 << 20), "presse-papiers démesuré : connexion fermée")
f = ViewOnlyFilter()
check(f.feed(HAND + KEY[:5]) == b"RFB 003.008\n\x01\x01" and f.feed(KEY[5:]) == b"" and f.dropped == 1,
      "touche coupée en deux trames : jetée entière")
print(f"\n{ok} ok, {fails} échec(s)")
sys.exit(1 if fails else 0)
