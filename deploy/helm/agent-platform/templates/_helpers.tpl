{{/* =============================================================================
     Agent Platform chart helpers
     照底座 saas-foundation/_service-template/_helpers.tpl 范式，风格对齐。
     ============================================================================= */}}

{{/*
Expand the chart name.
*/}}
{{- define "agent-platform.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Full name for a given service component.
Usage: {{ include "agent-platform.fullname" (dict "svcName" "bff" "root" .) }}
*/}}
{{- define "agent-platform.fullname" -}}
{{- printf "ap-%s" .svcName | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels (applied to all resources).
Usage: {{ include "agent-platform.labels" (dict "svcName" "bff" "root" .) }}
*/}}
{{- define "agent-platform.labels" -}}
app.kubernetes.io/name: {{ .svcName }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .svcName }}
app.kubernetes.io/part-of: agent-platform
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
agent-platform.example.com/profile: {{ .root.Values.global.profile | default "small" }}
{{- end -}}

{{/*
Selector labels (used in Deployment.spec.selector & PDB).
*/}}
{{- define "agent-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ .svcName }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/*
Render the container image reference.
Usage: {{ include "agent-platform.image" (dict "img" .Values.bff.image "root" .) }}
*/}}
{{- define "agent-platform.image" -}}
{{- $registry := .root.Values.global.imageRegistry | default "" -}}
{{- $repo := .img.repository -}}
{{- $tag := .img.tag | default .root.Chart.AppVersion -}}
{{- if $registry -}}
{{ $registry }}/{{ $repo }}:{{ $tag }}
{{- else -}}
{{ $repo }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/*
Common environment variables injected into every agent-platform service.
Reads from .root.Values.global.foundation.* for all foundation endpoints.
Usage: {{ include "agent-platform.commonEnv" (dict "svcName" "bff" "root" .) }}
*/}}
{{- define "agent-platform.commonEnv" -}}
- name: AP_SERVICE_NAME
  value: {{ .svcName | quote }}
- name: AP_PROFILE
  value: {{ .root.Values.global.profile | default "small" | quote }}
- name: TZ
  value: {{ .root.Values.global.timezone | default "Asia/Shanghai" | quote }}
# OTEL
- name: OTEL_SERVICE_NAME
  value: {{ printf "ap-%s" .svcName | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.global.foundation.otelCollector.endpoint | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "service.namespace=agent-platform,deployment.environment=%s" (.root.Values.global.profile | default "small") | quote }}
# Foundation service URLs
- name: FOUNDATION_AUTHZ_URL
  value: {{ .root.Values.global.foundation.authzUrl | quote }}
- name: FOUNDATION_BILLING_URL
  value: {{ .root.Values.global.foundation.billingUrl | quote }}
- name: FOUNDATION_IDENTITY_URL
  value: {{ .root.Values.global.foundation.identityUrl | quote }}
# Database
- name: DB_HOST
  value: {{ .root.Values.global.foundation.database.host | quote }}
- name: DB_PORT
  value: {{ .root.Values.global.foundation.database.port | quote }}
- name: DB_NAME
  value: {{ .root.Values.global.foundation.database.name | quote }}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.database.existingSecret }}
      key: app_user
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.database.existingSecret }}
      key: app_user_password
# Redis
{{- if .root.Values.global.foundation.redis.url }}
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.redis.existingSecret }}
      key: url
{{- else }}
- name: REDIS_HOST
  value: {{ .root.Values.global.foundation.redis.host | quote }}
- name: REDIS_PORT
  value: {{ .root.Values.global.foundation.redis.port | quote }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.redis.existingSecret }}
      key: password
{{- end }}
# S3 / MinIO
- name: S3_ENDPOINT_URL
  value: {{ .root.Values.global.foundation.s3.endpoint | quote }}
- name: S3_REGION
  value: {{ .root.Values.global.foundation.s3.region | quote }}
- name: S3_BUCKET
  value: {{ .root.Values.global.foundation.s3.bucket | quote }}
- name: S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.s3.existingSecret }}
      key: access_key_id
- name: S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.global.foundation.s3.existingSecret }}
      key: secret_access_key
{{- end -}}

{{/*
Render HPA if autoscaling.enabled=true.
Usage: {{ include "agent-platform.hpa" (dict "svcName" "bff" "svc" .Values.bff "root" .) }}
*/}}
{{- define "agent-platform.hpa" -}}
{{- if and .svc.autoscaling ((.svc.autoscaling).enabled) -}}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "agent-platform.fullname" (dict "svcName" .svcName "root" .root) }}
  labels: {{- include "agent-platform.labels" (dict "svcName" .svcName "root" .root) | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "agent-platform.fullname" (dict "svcName" .svcName "root" .root) }}
  minReplicas: {{ .svc.autoscaling.minReplicas | default 1 }}
  maxReplicas: {{ .svc.autoscaling.maxReplicas | default 10 }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .svc.autoscaling.targetCPUUtilizationPercentage | default 70 }}
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
        - type: Pods
          value: 4
          periodSeconds: 30
      selectPolicy: Max
{{- end -}}
{{- end -}}

{{/*
Render PDB if podDisruptionBudget.enabled=true.
Usage: {{ include "agent-platform.pdb" (dict "svcName" "bff" "svc" .Values.bff "root" .) }}
*/}}
{{- define "agent-platform.pdb" -}}
{{- if and .svc.podDisruptionBudget ((.svc.podDisruptionBudget).enabled) -}}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "agent-platform.fullname" (dict "svcName" .svcName "root" .root) }}
  labels: {{- include "agent-platform.labels" (dict "svcName" .svcName "root" .root) | nindent 4 }}
spec:
  minAvailable: {{ .svc.podDisruptionBudget.minAvailable | default 1 }}
  selector:
    matchLabels: {{- include "agent-platform.selectorLabels" (dict "svcName" .svcName "root" .root) | nindent 6 }}
{{- end -}}
{{- end -}}

{{/*
Render NetworkPolicy (medium/large profile).
Usage: {{ include "agent-platform.networkPolicy" (dict "svcName" "bff" "svc" .Values.bff "root" . "allowFrom" (list "web") "egressPorts" (list 8006 5432) ) }}
*/}}
{{- define "agent-platform.networkPolicy" -}}
{{- if and .svc.networkPolicy ((.svc.networkPolicy).enabled) -}}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "agent-platform.fullname" (dict "svcName" .svcName "root" .root) }}
  labels: {{- include "agent-platform.labels" (dict "svcName" .svcName "root" .root) | nindent 4 }}
spec:
  podSelector:
    matchLabels: {{- include "agent-platform.selectorLabels" (dict "svcName" .svcName "root" .root) | nindent 6 }}
  policyTypes: [Ingress, Egress]
  ingress:
    # Prometheus scrape
    - from:
        - namespaceSelector:
            matchLabels:
              foundation.example.com/role: observability
      ports:
        - port: {{ .port | default 8080 }}
          protocol: TCP
    # 同 namespace 内服务互访
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .root.Release.Namespace }}
          podSelector:
            matchLabels:
              app.kubernetes.io/part-of: agent-platform
      ports:
        - port: {{ .port | default 8080 }}
          protocol: TCP
    # Ingress controller
    - from:
        - namespaceSelector:
            matchLabels:
              foundation.example.com/role: ingress
      ports:
        - port: {{ .port | default 8080 }}
          protocol: TCP
  egress:
    # DNS
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    # agent-platform 内部服务
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .root.Release.Namespace }}
          podSelector:
            matchLabels:
              app.kubernetes.io/part-of: agent-platform
    # Foundation 服务（跨 namespace）
    - to:
        - podSelector:
            matchExpressions:
              - key: app.kubernetes.io/part-of
                operator: In
                values: [saas-foundation]
    # DB / Redis / MinIO / OTel
    - to:
        - namespaceSelector: {}
      ports:
        - port: 5432
          protocol: TCP
        - port: 6379
          protocol: TCP
        - port: 9000
          protocol: TCP
        - port: 4317
          protocol: TCP
{{- end -}}
{{- end -}}
