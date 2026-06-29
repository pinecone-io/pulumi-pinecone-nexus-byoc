{{/* Resource name prefix. Defaults to "nexus" via values.yaml so resource
     names match what the legacy combined chart produced. */}}
{{- define "nexus-fdb.fullname" -}}
{{- default "nexus" .Values.fullnameOverride -}}
{{- end -}}

{{/* Instance label held at the legacy value. StatefulSet selectors are
     immutable, so this must match what the resources were originally
     created with for `helm install --take-ownership` to succeed. */}}
{{- define "nexus-fdb.instance" -}}
{{- default "nexus" .Values.instanceOverride -}}
{{- end -}}

{{- define "nexus-fdb.labels" -}}
app.kubernetes.io/name: nexus
app.kubernetes.io/instance: {{ include "nexus-fdb.instance" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "nexus-fdb.componentLabels" -}}
{{ include "nexus-fdb.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nexus-fdb.selectorLabels" -}}
app.kubernetes.io/name: nexus
app.kubernetes.io/instance: {{ include "nexus-fdb.instance" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nexus-fdb.servicesScheduling" -}}
{{- with .Values.scheduling.services.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.scheduling.services.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
