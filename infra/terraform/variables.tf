variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "se-proto"
}

variable "region" {
  description = "GCP region for the cluster and artifact registry"
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "GCP zone for the (zonal) GKE cluster"
  type        = string
  default     = "europe-west1-b"
}

variable "cluster_name" {
  description = "Name of the GKE cluster"
  type        = string
  default     = "pms-cluster"
}

variable "node_count" {
  description = "Fixed number of nodes in the node pool (set to 1, 3, or 5 for benchmarking)"
  type        = number
  default     = 1
}

variable "machine_type" {
  description = "Machine type for cluster nodes. Keep constant across node_count sweeps (1/3/5); only change it for the optional more-performant-machine comparison."
  type        = string
  default     = "e2-medium"
}

variable "disk_size_gb" {
  description = "Boot disk size per node, in GB"
  type        = number
  default     = 30
}
