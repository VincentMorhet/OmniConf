"""Écran distant en lecture seule : filtre le flux RFB (VNC) envoyé par le navigateur d'un spectateur.

Seuls passent la poignée de main (version, sécurité « aucune », ClientInit forcé en partagé) et les messages qui
demandent des images (format, encodages, mise à jour, mises à jour continues, Fence). Clavier, souris,
presse-papiers, redimensionnement, extensions : jetés. Tout message inconnu ou mal formé ferme la connexion
(on ne devine pas sa longueur).
"""
import struct


MAX_MSG = 1 << 20                  # 1 Mo : au-delà, la connexion est fermée


class RfbViolation(Exception):
    pass


# type de message client -> autorisé ?
ALLOWED = {0, 2, 3, 150, 248}      # SetPixelFormat, SetEncodings, FramebufferUpdateRequest, EnableContinuousUpdates, Fence
DROPPED = {4, 5, 6, 250, 251, 255}  # KeyEvent, PointerEvent, ClientCutText, xvp, SetDesktopSize, QEMU


class ViewOnlyFilter:
    def __init__(self):
        self.buf = b""
        self.phase = "version"
        self.dropped = 0           # messages d'entrée jetés (journal)

    def _size(self, b: bytes) -> int | None:
        """Taille du message client en tête de `b`, None s'il faut plus d'octets."""
        t = b[0]
        fixed = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 250: 4}
        if t in fixed:
            return fixed[t]
        if t == 2:                      # SetEncodings : 4 + 4 × nombre
            return None if len(b) < 4 else 4 + 4 * struct.unpack(">H", b[2:4])[0]
        if t == 6:                      # ClientCutText : 8 + longueur (négative : presse-papiers étendu)
            return None if len(b) < 8 else 8 + abs(struct.unpack(">i", b[4:8])[0])
        if t == 248:                    # Fence : 9 + longueur des données (≤ 64)
            if len(b) < 9:
                return None
            if b[8] > 64:
                raise RfbViolation("Fence trop long")
            return 9 + b[8]
        if t == 251:                    # SetDesktopSize : 8 + 16 × écrans
            return None if len(b) < 8 else 8 + 16 * b[6]
        if t == 255:                    # QEMU : seule la touche étendue (sous-type 0) a une taille connue
            if len(b) < 2:
                return None
            if b[1] != 0:
                raise RfbViolation("message QEMU inconnu")
            return 12
        raise RfbViolation(f"message client inconnu ({t})")

    def feed(self, data: bytes) -> bytes:
        """Octets reçus du navigateur -> octets à transmettre au serveur VNC."""
        if len(self.buf) + len(data) > MAX_MSG:
            raise RfbViolation("message trop long")
        self.buf += data
        out = bytearray()
        while self.buf:
            if self.phase == "version":
                if len(self.buf) < 12:
                    break
                v = self.buf[:12]
                if not (v.startswith(b"RFB 003.") and v.endswith(b"\n") and v[8:11].isdigit()):
                    raise RfbViolation("version RFB invalide")
                if int(v[8:11]) < 7:
                    raise RfbViolation("version RFB trop ancienne")
                out += v
                self.buf, self.phase = self.buf[12:], "security"
            elif self.phase == "security":
                if self.buf[0] != 1:     # « aucune » seulement (x11vnc -nopw) : pas d'échange caché à suivre
                    raise RfbViolation(f"type de sécurité {self.buf[0]} non pris en charge")
                out += self.buf[:1]
                self.buf, self.phase = self.buf[1:], "init"
            elif self.phase == "init":
                out += b"\x01"            # ClientInit : toujours partagé (ne déconnecte pas le propriétaire)
                self.buf, self.phase = self.buf[1:], "msgs"
            else:
                n = self._size(self.buf)
                if n is not None and n > MAX_MSG:
                    raise RfbViolation("message trop long")
                if n is None or len(self.buf) < n:
                    break
                msg, self.buf = self.buf[:n], self.buf[n:]
                if msg[0] in ALLOWED:
                    out += msg
                else:
                    self.dropped += 1
        return bytes(out)
