# `deploy/operator/` — KEDA autoscaling for sandbox-pool

These manifests scale the `sandbox-pool` Deployment based on the warm-VM
gauge it exports (`sandbox_pool_idle{template="base"}`). KEDA produces an
HPA under the hood; we use KEDA rather than a plain HPA so we can poll a
custom Prometheus metric without standing up the `custom.metrics.k8s.io`
adapter ourselves.

See `docs/design/b-m2-sandbox-runtime-v1.md §5` for the full design.

## Files

| File                                       | Purpose                                                              |
| ------------------------------------------ | -------------------------------------------------------------------- |
| `keda-scaledobject-sandbox-pool.yaml`      | The `ScaledObject` itself — Prometheus trigger + min/max replicas.   |
| `keda-prometheus-trigger-auth.yaml`        | Optional `TriggerAuthentication` for bearer-token-protected scrapes. |

## Prerequisites

1. **Kubernetes 1.27+** — KEDA v2.13 supports 1.27 onward.
2. **KEDA v2.13+** installed cluster-wide. Helm:

    ```bash
    helm repo add kedacore https://kedacore.github.io/charts
    helm repo update
    helm upgrade --install keda kedacore/keda \
      --namespace keda --create-namespace \
      --version 2.13.2
    ```

3. **Prometheus Operator** (or any Prometheus reachable in-cluster) scraping
   sandbox-pool's `/metrics` endpoint. The `serverAddress` in the
   `ScaledObject` defaults to `prometheus-operated.monitoring.svc:9090`;
   adjust to match your install.

   A minimal `ServiceMonitor` (left out of this directory because the rest of
   agent-platform's Prometheus config lives in your platform repo) needs:

    ```yaml
    spec:
      selector:
        matchLabels:
          app.kubernetes.io/name: sandbox-pool
      endpoints:
        - port: http
          path: /metrics
          interval: 15s
    ```

4. **sandbox-pool Deployment** named `sandbox-pool` in the `agent-platform`
   namespace. If you deploy under a different name/namespace, edit
   `scaleTargetRef` / `metadata.namespace` accordingly.

5. *(Optional)* If your Prometheus is protected by kube-rbac-proxy or a
   bearer token, create the secret expected by the `TriggerAuthentication`:

    ```bash
    kubectl -n agent-platform create secret generic \
      sandbox-pool-prometheus-credentials \
      --from-file=bearerToken=/path/to/token \
      --from-file=ca=/path/to/prometheus-ca.crt
    ```

   If your Prometheus is open to in-cluster scrapes, you can delete
   `keda-prometheus-trigger-auth.yaml` and drop the `authenticationRef`
   block from the `ScaledObject`.

## Apply

```bash
kubectl apply -f deploy/operator/keda-prometheus-trigger-auth.yaml
kubectl apply -f deploy/operator/keda-scaledobject-sandbox-pool.yaml
```

Verify:

```bash
kubectl -n agent-platform get scaledobject sandbox-pool
kubectl -n agent-platform get hpa keda-hpa-sandbox-pool
```

The HPA name is `keda-hpa-<ScaledObject name>` by convention.

## Tuning

* **`threshold` / `activationThreshold`** in the ScaledObject must match
  `SANDBOX_POOL_IDLE_LOW_WATERMARK` for the deployment. Keep these in sync
  per tier (dev: 2, small-business: 5, SaaS large: 50).
* **`maxReplicaCount`** caps the blast radius if Prometheus loses data and
  reports `idle == 0` for an extended period. 10 is a safe starting point
  for the SaaS-large tier; raise only after observing real saturation.
* **`cooldownPeriod`** is set to 5 min to avoid flapping on bursty agent
  traffic.
