locals {
  required_apis = [
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "pms" {
  name     = var.cluster_name
  project  = var.project_id
  location = var.zone

  # Zonal (single-zone) cluster: one free control plane per billing
  # account, and no cross-zone control-plane replication cost.
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = false

  depends_on = [google_project_service.required]
}

resource "google_container_node_pool" "pms_nodes" {
  name     = "pms-node-pool"
  project  = var.project_id
  location = var.zone
  cluster  = google_container_cluster.pms.name

  # Fixed size on purpose: no autoscaling block. Node count is a
  # manual knob (1 / 3 / 5) for the scalability benchmark, not
  # something GKE adjusts on its own.
  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}

resource "google_artifact_registry_repository" "pms" {
  project       = var.project_id
  location      = var.region
  repository_id = "pms"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}
