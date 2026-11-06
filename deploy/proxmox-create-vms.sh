#!/usr/bin/env bash
# Crée les 3 VM légères (Debian 12 cloud-init) sur le cluster Proxmox.
# À lancer depuis pve-00 (qui a accès SSH root aux nœuds).
#
#   VM 230 webconf-ctl  (pve-01) : IHM + API + stockage enregistrements   2 vCPU / 2 Go
#   VM 231 webconf-w1   (pve-02) : worker navigateur (Teams/Zoom)          4 vCPU / 6 Go
#   VM 232 webconf-w2   (pve-03) : worker navigateur (Teams/Zoom)          4 vCPU / 6 Go
#   (option) VM 233 webconf-ct (pve-01) : hôte Docker des workers en conteneurs   8 vCPU / 12 Go
set -euo pipefail

IMAGE=${IMAGE:-/var/lib/vz/template/iso/debian-12-genericcloud-amd64.qcow2}
SSHKEY=${SSHKEY:-/root/.ssh/id_rsa.pub}
GW=${GW:-192.168.0.254}
DNS=${DNS:-192.168.0.1}
BRIDGE=${BRIDGE:-vmbr0}
STORAGE=${STORAGE:-local-lvm}

#      vmid node        ip          name         cores mem   disk
VMS=(
  "230 192.168.0.201 192.168.0.230 webconf-ctl 2 2048 20G"
  "231 192.168.0.202 192.168.0.231 webconf-w1  4 6144 40G"
  "232 192.168.0.203 192.168.0.232 webconf-w2  4 6144 40G"
)
# Autres VM : VMS_SPEC="233 192.168.0.201 192.168.0.233 webconf-ct 8 12288 60G" (une ligne par VM)
# ex. la VM hôte des conteneurs workers (voir deploy/deploy-containers.sh)
if [ -n "${VMS_SPEC:-}" ]; then mapfile -t VMS <<<"$VMS_SPEC"; fi

for spec in "${VMS[@]}"; do
  read -r VMID NODE IP NAME CORES MEM DISK <<<"$spec"
  echo "=== $NAME (vmid $VMID) sur $NODE -> $IP"
  if ssh root@"$NODE" "qm status $VMID" >/dev/null 2>&1; then
    echo "    existe déjà, ignorée"; continue
  fi
  scp -q "$IMAGE" root@"$NODE":/root/debian-12-genericcloud.qcow2
  scp -q "$SSHKEY" root@"$NODE":/root/webconf-key.pub
  ssh root@"$NODE" bash -s <<EOF
set -e
qm create $VMID --name $NAME --memory $MEM --cores $CORES --cpu host \
  --net0 virtio,bridge=$BRIDGE --scsihw virtio-scsi-single --ostype l26 \
  --agent 1 --serial0 socket --vga serial0 --onboot 1 --tags webconf
qm set $VMID --scsi0 $STORAGE:0,import-from=/root/debian-12-genericcloud.qcow2,discard=on,ssd=1
qm resize $VMID scsi0 $DISK
qm set $VMID --ide2 $STORAGE:cloudinit --boot order=scsi0 \
  --ciuser debian --sshkeys /root/webconf-key.pub \
  --ipconfig0 ip=$IP/24,gw=$GW --nameserver $DNS --searchdomain ${SEARCHDOMAIN:-lan}
qm start $VMID
rm -f /root/debian-12-genericcloud.qcow2 /root/webconf-key.pub
EOF
done

echo "Attente SSH..."
for spec in "${VMS[@]}"; do
  read -r _ _ IP NAME _ <<<"$spec"
  for i in $(seq 1 60); do
    ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new debian@"$IP" true 2>/dev/null && { echo "  $NAME OK"; break; }
    sleep 5
  done
done
