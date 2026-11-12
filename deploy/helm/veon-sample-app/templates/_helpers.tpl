{{- define "vsa.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vsa.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "vsa.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vsa.labels" -}}
helm.sh/chart: {{ include "vsa.chart" . }}
app.kubernetes.io/name: {{ include "vsa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: veon
{{- end -}}

{{/* Kullanım: include "vsa.componentLabels" (dict "ctx" $ "component" "backend") */}}
{{- define "vsa.componentLabels" -}}
{{ include "vsa.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "vsa.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vsa.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Pod label'ları: selector + VEON Alloy log discovery'nin kullandığı `app` label'ı */}}
{{- define "vsa.podLabels" -}}
{{ include "vsa.selectorLabels" . }}
app: {{ include "vsa.fullname" .ctx }}-{{ .component }}
{{- end -}}

{{/* Kullanım: include "vsa.image" (dict "ctx" $ "image" .Values.image.backend) */}}
{{- define "vsa.image" -}}
{{- $repository := .image.repository -}}
{{- with .ctx.Values.global.imageRegistry -}}
{{- $repository = printf "%s/%s" . $repository -}}
{{- end -}}
{{- if .image.digest -}}
{{- printf "%s@%s" $repository .image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (default .ctx.Chart.AppVersion .image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "vsa.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "vsa.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Gizli değerler: yalnız mevcut Secret'a referans */}}
{{- define "vsa.secretEnv" -}}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.redisPassword }}
{{- if .Values.secrets.keys.redisSentinelPassword }}
- name: REDIS_SENTINEL_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.redisSentinelPassword }}
{{- end }}
- name: MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.mongoUri }}
{{- end -}}

{{/* envFrom sırası: app config → tracing config. Açık `env` her ikisini de ezer. */}}
{{- define "vsa.envFrom" -}}
- configMapRef:
    name: {{ include "vsa.fullname" . }}-config
{{- if .Values.global.tracing.enabled }}
- configMapRef:
    name: {{ include "vsa.fullname" . }}-tracing
{{- end }}
{{- end -}}

{{/* Kullanım: include "vsa.containerSecurityContext" (dict "ctx" $ "uid" 10001) */}}
{{- define "vsa.containerSecurityContext" -}}
{{- $sc := deepCopy .ctx.Values.containerSecurityContext -}}
{{- $_ := set $sc "runAsUser" .uid -}}
{{- $_ := set $sc "runAsGroup" .uid -}}
{{- toYaml $sc -}}
{{- end -}}

{{/* Replikaları node ve zone'lara yay (tek node/zone kaybı tüm replikaları götürmesin) */}}
{{- define "vsa.topologySpread" -}}
{{- if .ctx.Values.topologySpread.enabled }}
topologySpreadConstraints:
  - maxSkew: {{ .ctx.Values.topologySpread.maxSkew }}
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "vsa.selectorLabels" . | nindent 8 }}
  - maxSkew: {{ .ctx.Values.topologySpread.maxSkew }}
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "vsa.selectorLabels" . | nindent 8 }}
{{- end }}
{{- end -}}

{{- define "vsa.checksums" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
checksum/tracing: {{ include (print .Template.BasePath "/tracing-configmap.yaml") . | sha256sum }}
{{- end -}}
