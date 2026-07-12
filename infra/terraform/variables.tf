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
  description = "Fixed number of nodes when enable_autoscaling is false (set to 1, 3, or 5 for benchmarking)"
  type        = number
  default     = 1
}

variable "enable_autoscaling" {
  description = "When true, GKE adjusts node pool size between min_node_count and max_node_count"
  type        = bool
  default     = false
}

variable "min_node_count" {
  description = "Minimum nodes when enable_autoscaling is true"
  type        = number
  default     = 1
}

variable "max_node_count" {
  description = "Maximum nodes when enable_autoscaling is true (keep within project vCPU quota)"
  type        = number
  default     = 5
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
