variable "region" {
  description = "Région OVHcloud Public Cloud (ex. GRA11 Gravelines, SBG5 Strasbourg, EU-WEST-PAR Paris)"
  type        = string
  default     = "GRA11"
}

variable "name" {
  description = "Nom de l'instance et préfixe des ressources"
  type        = string
  default     = "omniconf"
}

variable "flavor" {
  description = "Gabarit : b3-16 (4 vCPU, 16 Go) pour 2 à 3 workers, b3-32 (8 vCPU, 32 Go) pour 4 à 6"
  type        = string
  default     = "b3-16"
}

variable "image_name" {
  description = "Image système (Debian 12, comme les VM Proxmox)"
  type        = string
  default     = "Debian 12"
}

variable "ssh_public_key_file" {
  description = "Clé publique SSH de l'administrateur (utilisateur « debian »)"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "admin_cidrs" {
  description = "Adresses autorisées en SSH (ex. [\"203.0.113.10/32\"])"
  type        = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && !contains(var.admin_cidrs, "0.0.0.0/0")
    error_message = "Indiquer au moins une adresse d'administration, et pas 0.0.0.0/0."
  }
}

variable "web_cidrs" {
  description = "Adresses autorisées sur l'IHM (HTTPS 443). Tout Internet par défaut (protégé par mot de passe)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "webrtc_cidrs" {
  description = "Adresses autorisées pour l'audio WebRTC (UDP) entre le navigateur et les workers"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "domain" {
  description = "Nom de domaine de l'IHM (enregistrement A vers l'IP de l'instance). Vide : <ip>.sslip.io"
  type        = string
  default     = ""
}

variable "worker_count" {
  description = "Nombre de workers (conteneurs), donc de réunions simultanées"
  type        = number
  default     = 2
  validation {
    condition     = var.worker_count >= 1 && var.worker_count <= 20
    error_message = "Entre 1 et 20 workers."
  }
}

variable "data_volume_gb" {
  description = "Taille du disque des enregistrements (Go), conservé si l'instance est recréée"
  type        = number
  default     = 100
}

variable "volume_type" {
  description = "Type de disque : classic ou high-speed"
  type        = string
  default     = "classic"
}
