{{- define "containerssh.namespace" -}}
{{ .Values.config.namespace }}
{{- end }}

{{- define "containerssh.authPasswordUrl" -}}
http://containerssh-auth-service.{{ .Values.config.namespace }}.svc.cluster.local
{{- end }}

{{- define "containerssh.authPubkeyUrl" -}}
http://containerssh-auth-service.{{ .Values.config.namespace }}.svc.cluster.local
{{- end }}

{{- define "containerssh.configServerUrl" -}}
http://containerssh-config-service.{{ .Values.config.namespace }}.svc.cluster.local/config
{{- end }}

