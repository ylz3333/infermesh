# Deployment guide

## Local Docker + Jaeger

```bash
cp .env.example .env
# Set a fresh gateway key in .env; do not commit it.
docker compose up --build
```

Gateway: http://localhost:8000. Jaeger: http://localhost:16686. Choose the `infermesh`
service after sending a request. Compose is loopback-only and uses mock providers.
To use real endpoints, mount a local config, change `INFERMESH_CONFIG`, and pass
provider secrets through the environment. The API base URLs are operator-trusted.

## Kubernetes demo

Requires a cluster, Docker-compatible build tooling, and Metrics Server for HPA.
No cluster deployment or autoscaling test is claimed in this repository.

```bash
docker build -t infermesh:0.1.0 .
# For kind: kind load docker-image infermesh:0.1.0
# For a remote cluster: push to your registry and change the image in the manifest.
kubectl apply -f deploy/kubernetes.yaml
kubectl -n infermesh create secret generic infermesh-secrets \
  --from-literal=gateway-key="$INFERMESH_API_KEY"
kubectl -n infermesh rollout status deployment/infermesh
kubectl -n infermesh port-forward service/infermesh 8000:8000
```

Pods wait for the required Secret. Supply a strong key before exposing an ingress.
The sample Service is internal; configure TLS, ingress authentication/rate controls,
and cluster-specific network policies for an external deployment. Replace the mock
ConfigMap with actual endpoint configuration and add environment references to provider
Secrets. Add `OTEL_EXPORTER_OTLP_ENDPOINT` for the cluster collector.

The manifest includes two replicas, rolling updates without voluntary unavailability,
health/readiness/startup probes, CPU/memory constraints, a non-root read-only filesystem,
HPA (2–5 replicas), and a disruption budget. These are configured behaviors, not proof
of high availability. `readyz` may be false under saturation or open circuits.

## Planned production work

Global rate limits and budgets, tenant identity, secret rotation, durable audit storage,
model-specific capability tests, trace-retention policy, load-based HPA signals, and
multi-node fault tests are required before asserting production readiness or an SLA.
