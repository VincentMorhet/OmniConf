terraform {
  required_version = ">= 1.5"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 3.4"
    }
  }
}

# Authentification : variables OS_* du fichier « openrc » d'un utilisateur Public Cloud
# (Manager OVHcloud > Public Cloud > Users & Roles > télécharger le fichier OpenStack RC).
provider "openstack" {
  region = var.region
}
