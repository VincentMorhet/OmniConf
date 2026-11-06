output "public_ipv4" {
  description = "IP publique de l'instance"
  value       = openstack_compute_instance_v2.webconf.access_ip_v4
}

output "site_address" {
  description = "Adresse de l'IHM"
  value = (var.domain != ""
    ? "https://${var.domain}"
  : "https://${replace(openstack_compute_instance_v2.webconf.access_ip_v4, ".", "-")}.sslip.io")
}

output "worker_names" {
  description = "Noms des workers (conteneurs)"
  value       = join(" ", [for i in range(var.worker_count) : "w${i + 1}"])
}

output "data_volume_id" {
  description = "Identifiant du disque de données"
  value       = openstack_blockstorage_volume_v3.data.id
}

output "ssh" {
  description = "Connexion SSH"
  value       = "ssh debian@${openstack_compute_instance_v2.webconf.access_ip_v4}"
}
