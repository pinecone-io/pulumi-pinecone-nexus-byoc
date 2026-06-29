{{/* Base name used as prefix for all resources. */}}
{{- define "nexus.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "nexus.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "nexus.labels" -}}
app.kubernetes.io/name: {{ include "nexus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "nexus.componentLabels" -}}
{{ include "nexus.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nexus.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nexus.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Service name per component (kept short for DNS). */}}
{{- define "nexus.svc" -}}
{{- printf "%s-%s" (include "nexus.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* PVC name helper. */}}
{{- define "nexus.pvc" -}}
{{- printf "%s-%s" (include "nexus.fullname" .root) .volume | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* True ("true") when pc-blob uses the filesystem `local` provider, which needs
     a PersistentVolume for its local_root. Cloud providers (gcp/aws/azure) read
     from object storage and provision nothing. Emits "" (falsey) otherwise. */}}
{{- define "nexus.usesFsStorage" -}}
{{- if eq .Values.config.cloud.provider "local" -}}
true
{{- end -}}
{{- end -}}

{{/* The local provider's filesystem root — the mount path for the data volume
     and PINECONE_STORAGE__LOCAL_ROOT. Fails the render with a clear message if
     the provider is local but the root is unset, rather than emitting an empty
     mountPath that only fails at apply. */}}
{{- define "nexus.localRoot" -}}
{{- required "config.storage.localRoot must be set when config.cloud.provider=local" .Values.config.storage.localRoot -}}
{{- end -}}

{{/* Scheduling block for services-pool pods (nodeSelector + tolerations). */}}
{{- define "nexus.servicesScheduling" -}}
{{- with .Values.scheduling.services.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.scheduling.services.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Runtime image for task pods. Falls back to registry/nexus_runtime:tag. */}}
{{- define "nexus.runtimeImage" -}}
{{- if .Values.orchestrator.runtime.image -}}
{{- .Values.orchestrator.runtime.image -}}
{{- else if .Values.image.registry -}}
{{- printf "%s/nexus_runtime:%s" .Values.image.registry .Values.image.tag -}}
{{- else -}}
{{- printf "nexus_runtime:%s" .Values.image.tag -}}
{{- end -}}
{{- end -}}

{{/* Shared environment variables injected into all Nexus services.
     Add new per-environment settings here; all service templates pick them up
     via: {{- include "nexus.envSettings" . | nindent 12 }} */}}
{{- define "nexus.envSettings" -}}
- name: PINECONE_CONFIG_PROFILES
  value: {{ .Values.configProfiles | quote }}
{{- if .Values.localRuntime }}
- name: NEXUS_LOCAL_RUNTIME
  value: "1"
{{- end }}
{{- if .Values.logFormat }}
- name: NEXUS_LOG_FORMAT
  value: {{ .Values.logFormat | quote }}
{{- end }}
{{- if .Values.config.pineconeProd }}
- name: PINECONE_PINECONE__PROD
  value: "true"
{{- end }}
{{- if .Values.config.deploymentMode }}
- name: PINECONE_PINECONE__DEPLOYMENT_MODE
  value: {{ .Values.config.deploymentMode | quote }}
{{- end }}
{{- if .Values.config.environment }}
- name: PINECONE_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
{{- end }}
{{- with .Values.config.cloud }}
{{- if .provider }}
- name: PINECONE_CLOUD__PROVIDER
  value: {{ .provider | quote }}
{{- end }}
{{- if .region }}
- name: PINECONE_CLOUD__REGION
  value: {{ .region | quote }}
{{- end }}
{{- end }}
{{- with .Values.config.host }}
{{- if .url }}
- name: PINECONE_HOST__URL
  value: {{ .url | quote }}
{{- end }}
{{- if .name }}
- name: PINECONE_HOST__NAME
  value: {{ .name | quote }}
{{- end }}
{{- end }}
{{- if .Values.config.byocProjectId }}
- name: PINECONE_PINECONE__BYOC_PROJECT_ID
  value: {{ .Values.config.byocProjectId | quote }}
{{- end }}
{{- if .Values.config.byocVaultId }}
- name: PINECONE_PINECONE__BYOC_VAULT_ID
  value: {{ .Values.config.byocVaultId | quote }}
{{- end }}
{{- with .Values.config.embeddingModel }}
{{- if .model }}
- name: PINECONE_PINECONE__EMBEDDING_MODEL__MODEL
  value: {{ .model | quote }}
- name: PINECONE_PINECONE__EMBEDDING_MODEL__DIMENSION
  value: {{ .dimension | quote }}
- name: PINECONE_PINECONE__EMBEDDING_MODEL__METRIC
  value: {{ .metric | quote }}
- name: PINECONE_PINECONE__EMBEDDING_MODEL__VECTOR_TYPE
  value: {{ .vectorType | quote }}
{{- end }}
{{- end }}
{{- if .Values.config.indexMetadata.indexId }}
- name: PINECONE_PINECONE__STATIC_INDEX_METADATA_PATH
  value: /etc/nexus/index-metadata/metadata.json
{{- end }}
{{- if .Values.config.indexCloud }}
- name: PINECONE_PINECONE__INDEX_CLOUD
  value: {{ .Values.config.indexCloud | quote }}
{{- end }}
{{- if .Values.config.indexRegion }}
- name: PINECONE_PINECONE__INDEX_REGION
  value: {{ .Values.config.indexRegion | quote }}
{{- end }}
{{- with .Values.config.storage }}
{{- if .endpoint }}
- name: PINECONE_STORAGE__ENDPOINT
  value: {{ .endpoint | quote }}
{{- end }}
{{- if .localRoot }}
- name: PINECONE_STORAGE__LOCAL_ROOT
  value: {{ .localRoot | quote }}
{{- end }}
{{- if .source }}
- name: PINECONE_STORAGE__SOURCE
  value: {{ .source | quote }}
{{- end }}
{{- if .knowledge }}
- name: PINECONE_STORAGE__KNOWLEDGE
  value: {{ .knowledge | quote }}
{{- end }}
{{- if .archive }}
- name: PINECONE_STORAGE__ARCHIVE
  value: {{ .archive | quote }}
{{- end }}
{{- with .azure }}
{{- if and .account (eq $.Values.config.cloud.provider "azure") }}
- name: AZURE_STORAGE_ACCOUNT
  value: {{ .account | quote }}
- name: AZURE_ACCOUNT_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus.fullname" $ }}-config
      key: azure-storage-access-key
      optional: true
{{- end }}
{{- end }}
{{- end }}
{{- with .Values.config.preview }}
{{- if .enabled }}
# Preview Playground usage constraints (read by nexus-api).
- name: PINECONE_PREVIEW__ENABLED
  value: "true"
{{- if .allowedImportExtensions }}
# Comma-separated; the PreviewSettings deserializer splits it (the pc-settings
# env source has no list separator). Empty leaves the config default.
- name: PINECONE_PREVIEW__ALLOWED_IMPORT_EXTENSIONS
  value: {{ join "," .allowedImportExtensions | quote }}
{{- end }}
{{- if .maxContextsPerProject }}
- name: PINECONE_PREVIEW__MAX_CONTEXTS_PER_PROJECT
  value: {{ .maxContextsPerProject | int64 | quote }}
{{- end }}
{{- if .maxFilesPerContext }}
- name: PINECONE_PREVIEW__MAX_FILES_PER_CONTEXT
  value: {{ .maxFilesPerContext | int64 | quote }}
{{- end }}
{{- if .maxBytesPerContext }}
# int64 avoids YAML coercing the large byte cap to scientific-notation float,
# which pc-settings can't parse back into a u64.
- name: PINECONE_PREVIEW__MAX_BYTES_PER_CONTEXT
  value: {{ .maxBytesPerContext | int64 | quote }}
{{- end }}
{{- if .maxActiveTasksPerContext }}
- name: PINECONE_PREVIEW__MAX_ACTIVE_TASKS_PER_CONTEXT
  value: {{ .maxActiveTasksPerContext | int64 | quote }}
{{- end }}
{{- if .maxActiveQueriesPerProject }}
- name: PINECONE_PREVIEW__MAX_ACTIVE_QUERIES_PER_PROJECT
  value: {{ .maxActiveQueriesPerProject | int64 | quote }}
{{- end }}
{{- if not .allowArchives }}
- name: PINECONE_PREVIEW__ALLOW_ARCHIVES
  value: "false"
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Observability env for app pods.

     The Rust services read o11y config from pc-settings (PINECONE_OBSERVABILITY__*);
     pc-settings bridges DD_AGENT_HOST -> observability.datadog.agent_host. The DD_*
     vars drive the Python ddtrace path (knowql). DD_AGENT_HOST is the node IP via the
     downward API (per-node Datadog agent DaemonSet); BYOC overrides the agent host in
     config/byoc.toml to the in-cluster global agent. Per-env metric tags
     (global_env/region/provider/kube_cluster_name) come from the profile TOMLs
     (development/production/cell-*), not from here.

     Usage: {{- include "nexus.datadogEnvSettings" (dict "root" . "component" "api") | nindent 12 }} */}}
{{- define "nexus.datadogEnvSettings" -}}
{{- if .root.Values.datadog.enabled }}
{{- $service := printf "%s-%s" (include "nexus.fullname" .root) .component }}
- name: DD_AGENT_HOST
  valueFrom:
    fieldRef:
      fieldPath: status.hostIP
{{- if not .skipPodName }}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
{{- end }}
# Rust o11y service name (pc-settings); DD_SERVICE below is the ddtrace equivalent.
- name: PINECONE_OBSERVABILITY__DATADOG__SERVICE_NAME
  value: {{ $service | quote }}
- name: DD_SERVICE
  value: {{ $service | quote }}
{{- if .root.Values.datadog.env }}
- name: DD_ENV
  value: {{ .root.Values.datadog.env | quote }}
{{- end }}
- name: DD_VERSION
  value: {{ .root.Values.image.tag | quote }}
- name: DD_TAGS
{{- if .root.Values.datadog.tags }}
  value: {{ printf "pod_name:$(POD_NAME),%s" .root.Values.datadog.tags | quote }}
{{- else }}
  value: "pod_name:$(POD_NAME)"
{{- end }}
# ddtrace runtime knobs. Read by the Python ddtrace package wrapping
# the Python services (mcp, inference-proxy); the Rust services (api,
# orchestrator) tolerate them harmlessly because their tracing is
# configured via nexus_common::o11y::O11ySettings::from_env, not these vars.
- name: DD_TRACE_ENABLED
  value: "true"
- name: DD_LOGS_INJECTION
  value: "true"
- name: DD_TRACE_PROPAGATION_STYLE
  value: "datadog"
# Keep `dd.trace_id` decimal lower-64 so it joins to the Rust JSON
# formatter's trace IDs (common/src/o11y/format.rs). ddtrace's log
# injection picks decimal vs hex-128 from the trace ID's value, so
# disabling 128-bit generation is the only knob that forces decimal
# output for traces that originate inside the Python service. Inbound
# traces propagated from nexus-api are already 64-bit on the wire.
- name: DD_TRACE_128_BIT_TRACEID_GENERATION_ENABLED
  value: "false"
{{- end }}
{{- end -}}

{{/* Deploy strategy block rendered from per-component values.
     Usage: {{- include "nexus.deployStrategy" .Values.api.strategy | nindent 2 }} */}}
{{- define "nexus.deployStrategy" -}}
strategy:
  type: {{ .type }}
{{- if and (eq .type "RollingUpdate") .rollingUpdate }}
  rollingUpdate:
    maxSurge: {{ .rollingUpdate.maxSurge }}
    maxUnavailable: {{ .rollingUpdate.maxUnavailable }}
{{- end }}
{{- end -}}

{{/* Resolve an image reference for a component. */}}
{{- define "nexus.image" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $tag := default $root.Values.image.tag (index $root.Values $component "image" "tag") -}}
{{- $repo := index $root.Values $component "image" "repository" -}}
{{- if $root.Values.image.registry -}}
{{- printf "%s/%s:%s" $root.Values.image.registry $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}
