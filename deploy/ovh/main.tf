# Omniconf chez OVHcloud Public Cloud : une instance Debian 12 qui porte le contrôle (IHM)
# et les workers en conteneurs, comme la VM de test webconf-ct. Le logiciel est installé
# ensuite par deploy-ovh.sh (les secrets ne passent jamais par Terraform ni par son état).

resource "openstack_compute_keypair_v2" "admin" {
  name       = "${var.name}-admin"
  public_key = file(pathexpand(var.ssh_public_key_file))
}

# ----------------------------------------------------------------------------- pare-feu
resource "openstack_networking_secgroup_v2" "webconf" {
  name        = "${var.name}-sg"
  description = "Omniconf : SSH admin, IHM HTTPS, audio WebRTC"
}

locals {
  rules = concat(
    [for c in var.admin_cidrs : { key = "ssh-${c}", proto = "tcp", min = 22, max = 22, cidr = c }],
    # 80 ouvert à tous : défi Let's Encrypt et redirection vers HTTPS
    [{ key = "http", proto = "tcp", min = 80, max = 80, cidr = "0.0.0.0/0" }],
    [for c in var.web_cidrs : { key = "https-${c}", proto = "tcp", min = 443, max = 443, cidr = c }],
    [for c in var.webrtc_cidrs : { key = "webrtc-${c}", proto = "udp", min = 1024, max = 65535, cidr = c }],
  )
}

resource "openstack_networking_secgroup_rule_v2" "in" {
  for_each          = { for r in local.rules : r.key => r }
  security_group_id = openstack_networking_secgroup_v2.webconf.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = each.value.proto
  port_range_min    = each.value.min
  port_range_max    = each.value.max
  remote_ip_prefix  = each.value.cidr
}

resource "openstack_networking_secgroup_rule_v2" "icmp" {
  security_group_id = openstack_networking_secgroup_v2.webconf.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "icmp"
  remote_ip_prefix  = "0.0.0.0/0"
}

# ----------------------------------------------------------------------------- instance
resource "openstack_compute_instance_v2" "webconf" {
  name            = var.name
  image_name      = var.image_name
  flavor_name     = var.flavor
  key_pair        = openstack_compute_keypair_v2.admin.name
  security_groups = [openstack_networking_secgroup_v2.webconf.name]
  user_data       = templatefile("${path.module}/cloud-init.yaml", { hostname = var.name })

  network {
    name = "Ext-Net" # réseau public OVH : IP publique directe, WebRTC sans NAT
  }

  lifecycle {
    # une nouvelle version de l'image Debian ne doit pas recréer l'instance
    ignore_changes = [image_name, image_id, user_data]
  }
}

# ----------------------------------------------------------------------------- données
# Disque séparé monté sur /srv/webconf (enregistrements, transcriptions, comptes rendus) :
# il survit à la reconstruction de l'instance. « terraform destroy » le supprime aussi.
resource "openstack_blockstorage_volume_v3" "data" {
  name        = "${var.name}-data"
  size        = var.data_volume_gb
  volume_type = var.volume_type
}

resource "openstack_compute_volume_attach_v2" "data" {
  instance_id = openstack_compute_instance_v2.webconf.id
  volume_id   = openstack_blockstorage_volume_v3.data.id
}
