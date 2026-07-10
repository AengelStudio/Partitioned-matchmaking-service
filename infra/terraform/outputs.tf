output "cluster_name" {
  value = google_container_cluster.pms.name
}

output "get_credentials_command" {
  description = "Run this to point kubectl at the new cluster"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.pms.name} --zone ${var.zone} --project ${var.project_id}"
}

output "artifact_registry_repository" {
  description = "Docker push target, e.g. docker push <this>/pms:local"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.pms.repository_id}"
}

output "node_count" {
  value = var.node_count
}
