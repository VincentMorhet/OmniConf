# Tests sans compte OVH : fournisseur OpenStack simulé.   cd deploy/ovh && terraform test
mock_provider "openstack" {}

variables {
  ssh_public_key_file = "tests/test_key.pub" # clé jetable, sans clé privée
  admin_cidrs         = ["203.0.113.10/32"]
}

override_resource {
  target = openstack_compute_instance_v2.webconf
  values = { access_ip_v4 = "198.51.100.7" }
}

run "par_defaut" {
  command = apply

  assert {
    condition     = output.site_address == "https://198-51-100-7.sslip.io"
    error_message = "Sans domaine, l'IHM doit être sur <ip>.sslip.io"
  }
  assert {
    condition     = output.worker_names == "w1 w2"
    error_message = "Deux workers par défaut"
  }
  assert {
    condition     = openstack_compute_instance_v2.webconf.flavor_name == "b3-16" && openstack_compute_instance_v2.webconf.network[0].name == "Ext-Net"
    error_message = "Instance b3-16 sur le réseau public"
  }
  assert {
    condition = (
      openstack_networking_secgroup_rule_v2.in["ssh-203.0.113.10/32"].port_range_min == 22
      && openstack_networking_secgroup_rule_v2.in["http"].remote_ip_prefix == "0.0.0.0/0"
      && openstack_networking_secgroup_rule_v2.in["https-0.0.0.0/0"].port_range_min == 443
      && openstack_networking_secgroup_rule_v2.in["webrtc-0.0.0.0/0"].protocol == "udp"
      && length(openstack_networking_secgroup_rule_v2.in) == 4
    )
    error_message = "Règles : SSH admin, 80, 443, UDP WebRTC, rien d'autre"
  }
  assert {
    condition     = alltrue([for r in openstack_networking_secgroup_rule_v2.in : !contains([8000, 8001, 5900, 6080], r.port_range_min)])
    error_message = "Les ports internes (IHM, agents, VNC) ne doivent pas être ouverts"
  }
}

run "domaine_et_acces_restreint" {
  command = apply
  variables {
    domain       = "omniconf.exemple.fr"
    web_cidrs    = ["203.0.113.0/24", "198.51.100.0/24"]
    worker_count = 4
  }
  assert {
    condition     = output.site_address == "https://omniconf.exemple.fr"
    error_message = "L'IHM doit utiliser le domaine"
  }
  assert {
    condition     = output.worker_names == "w1 w2 w3 w4"
    error_message = "Quatre workers"
  }
  assert {
    condition     = length([for k, r in openstack_networking_secgroup_rule_v2.in : k if r.port_range_min == 443]) == 2
    error_message = "443 ouvert seulement aux deux réseaux"
  }
}

run "ssh_ouvert_a_tous_refuse" {
  command = plan
  variables {
    admin_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.admin_cidrs]
}
