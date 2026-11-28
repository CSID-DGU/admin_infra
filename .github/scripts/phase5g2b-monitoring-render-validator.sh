#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -eq 3 ]] || {
  printf 'usage: %s <rendered-yaml> <expected-live-images> <expected-operator-args>\n' "$0" >&2
  exit 2
}

readonly RENDERED=$1
readonly EXPECTED_LIVE_IMAGES=$2
readonly EXPECTED_OPERATOR_ARGS=$3
readonly CERTGEN_IMAGE='ghcr.io/jkroepke/kube-webhook-certgen:1.7.3@sha256:c7f925cc3ec386e11b7393f941a8844786311fd2328943fe3e7c744c373ed864'
readonly CONFIG_RELOADER_IMAGE='quay.io/prometheus-operator/prometheus-config-reloader:v0.87.1@sha256:257ce3543e8f45c059dab488bcf897d8cc598cabd78afba12f41d6dbe36e22d2'
readonly THANOS_IMAGE='quay.io/thanos/thanos:v0.40.1@sha256:aae7b2b030ed00d88abd8c5a357d08b46f22151ca0d50d9c796223fa332389c8'

[[ -f "${RENDERED}" && ! -L "${RENDERED}" && -s "${RENDERED}" ]] \
  || { printf 'unsafe or empty monitoring render: %s\n' "${RENDERED}" >&2; exit 1; }

for command_name in awk chmod cmp diff find grep mktemp rm sort; do
  command -v "${command_name}" >/dev/null
done

work_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/phase5g2b-monitoring-render.XXXXXXXX")
trap 'rm -rf -- "${work_dir}"' EXIT
render_images="${work_dir}/render-images.txt"
expected_render_images="${work_dir}/expected-render-images.txt"
operator_args="${work_dir}/operator-args.txt"
hooks="${work_dir}/hooks.txt"
services="${work_dir}/services.txt"
: > "${render_images}"
: > "${operator_args}"
: > "${hooks}"
: > "${services}"

awk -v directory="${work_dir}" '
  BEGIN { document=0; path=sprintf("%s/document-%04d.yaml",directory,document) }
  /^---[[:space:]]*$/ {
    close(path)
    document++
    path=sprintf("%s/document-%04d.yaml",directory,document)
    next
  }
  { print > path }
' "${RENDERED}"

while IFS= read -r manifest; do
  kind=$(awk '
    /^kind:[[:space:]]+/ {
      value=$0
      sub(/^kind:[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      print value
      exit
    }
  ' "${manifest}")
  [[ -n "${kind}" ]] || continue
  name=$(awk '
    /^metadata:[[:space:]]*$/ { in_metadata=1; next }
    in_metadata && /^[^[:space:]]/ { exit }
    in_metadata && /^  name:[[:space:]]+/ {
      value=$0
      sub(/^  name:[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' "${manifest}")
  case "${kind}" in
    Service)
      [[ -n "${name}" ]] || { printf 'monitoring Service has no root metadata.name\n' >&2; exit 1; }
      if grep -Eq '^  type:[[:space:]]*"?(NodePort|LoadBalancer|ExternalName)"?[[:space:]]*$' "${manifest}"; then
        printf 'monitoring Service is externally exposed: %s\n' "${name}" >&2
        exit 1
      fi
      if grep -Eq '^[[:space:]]+(externalIPs|loadBalancerSourceRanges|loadBalancerIP|loadBalancerClass|externalName|nodePort):' "${manifest}"; then
        printf 'monitoring Service contains an external exposure field: %s\n' "${name}" >&2
        exit 1
      fi
      printf '%s\n' "${name}" >> "${services}"
      ;;
    Ingress|Gateway|HTTPRoute|GRPCRoute|TCPRoute|TLSRoute|UDPRoute|Route|IngressRoute|IngressRouteTCP|IngressRouteUDP|VirtualService)
      printf 'monitoring render contains externally routable kind: %s\n' "${kind}" >&2
      exit 1
      ;;
  esac

  if grep -Eq '^[[:space:]]+hostNetwork:[[:space:]]*(true|"true")[[:space:]]*$' "${manifest}"; then
    printf 'monitoring workload enables hostNetwork: %s/%s\n' "${kind}" "${name}" >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]+hostPort:' "${manifest}"; then
    printf 'monitoring workload declares hostPort: %s/%s\n' "${kind}" "${name}" >&2
    exit 1
  fi

  awk '
    /^[[:space:]]+image:[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]+image:[[:space:]]*/, "", value)
      sub(/[[:space:]]+#.*$/, "", value)
      sub(/[[:space:]]+$/, "", value)
      if ((substr(value,1,1) == "\"" && substr(value,length(value),1) == "\"") ||
          (substr(value,1,1) == "\047" && substr(value,length(value),1) == "\047")) {
        value=substr(value,2,length(value)-2)
      }
      if (length(value) > 0) print value
    }
  ' "${manifest}" >> "${render_images}"
  awk '
    /^[[:space:]]*-[[:space:]]+--(prometheus-config-reloader|thanos-default-base-image)=/ {
      value=$0
      sub(/^[^=]*=/, "", value)
      sub(/[[:space:]]+$/, "", value)
      gsub(/^"|"$/, "", value)
      print value
    }
  ' "${manifest}" >> "${operator_args}"

  if [[ "${kind}" == Job && "${name}" == master-monitoring-admission-create ]]; then
    grep -Fxq '    "helm.sh/hook": pre-install,pre-upgrade' "${manifest}"
    [[ $(grep -Fxc "          image: ${CERTGEN_IMAGE}" "${manifest}") -eq 1 ]]
    printf 'create\n' >> "${hooks}"
  fi
  if [[ "${kind}" == Job && "${name}" == master-monitoring-admission-patch ]]; then
    grep -Fxq '    "helm.sh/hook": post-install,post-upgrade' "${manifest}"
    [[ $(grep -Fxc "          image: ${CERTGEN_IMAGE}" "${manifest}") -eq 1 ]]
    printf 'patch\n' >> "${hooks}"
  fi
  if [[ "${kind}" == Pod && "${name}" =~ grafana.*test ]]; then
    printf 'Grafana test Pod must remain disabled\n' >&2
    exit 1
  fi
done < <(find "${work_dir}" -maxdepth 1 -type f -name 'document-*.yaml' | LC_ALL=C sort)

[[ -s "${services}" ]] || { printf 'monitoring render contains no Service\n' >&2; exit 1; }

cat > "${expected_render_images}" <<'EOF'
docker.io/grafana/grafana:12.3.0@sha256:70d9599b186ce287be0d2c5ba9a78acb2e86c1a68c9c41449454d0fc3eeb84e8
docker.io/library/busybox:1.31.1@sha256:95cf004f559831017cdf4628aaf1bb30133677be8702a8c5f2994629f637a209
ghcr.io/jkroepke/kube-webhook-certgen:1.7.3@sha256:c7f925cc3ec386e11b7393f941a8844786311fd2328943fe3e7c744c373ed864
quay.io/kiwigrid/k8s-sidecar:2.1.2@sha256:716b0b33ff2dc938a3f2bc64e5ea791d81fb09760bcd27cec1eb896968d6e134
quay.io/prometheus-operator/prometheus-operator:v0.87.1@sha256:6dbbbbeca6d7b94aa30723bfaa55262e41b9f8c15304c484611c696503840aac
quay.io/prometheus/alertmanager:v0.29.0@sha256:88743b63b3e09ea6e31e140ced5bf45f4a8e82c617c2a963f78841f4995ad1d7
quay.io/prometheus/node-exporter:v1.10.2@sha256:337ff1d356b68d39cef853e8c6345de11ce7556bb34cda8bd205bcf2ed30b565
quay.io/prometheus/prometheus:v3.8.0@sha256:d936808bdea528155c0154a922cd42fd75716b8bb7ba302641350f9f3eaeba09
registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0@sha256:2bbc915567334b13632bf62c0a97084aff72a36e13c4dabd5f2f11c898c5bacd
EOF

LC_ALL=C sort -u -o "${render_images}" "${render_images}"
LC_ALL=C sort -u -o "${expected_render_images}" "${expected_render_images}"
cmp -s "${render_images}" "${expected_render_images}" || {
  printf 'monitoring render image allowlist mismatch\n' >&2
  diff -u "${expected_render_images}" "${render_images}" >&2 || true
  exit 1
}
while IFS= read -r image; do
  [[ "${image}" =~ @sha256:[0-9a-f]{64}$ ]] \
    || { printf 'monitoring image is not digest-pinned: %s\n' "${image}" >&2; exit 1; }
done < "${render_images}"

printf '%s\n' "${CONFIG_RELOADER_IMAGE}" "${THANOS_IMAGE}" | LC_ALL=C sort \
  > "${EXPECTED_OPERATOR_ARGS}"
LC_ALL=C sort -u -o "${operator_args}" "${operator_args}"
cmp -s "${operator_args}" "${EXPECTED_OPERATOR_ARGS}" || {
  printf 'monitoring operator image arguments differ\n' >&2
  exit 1
}
printf '%s\n' create patch | LC_ALL=C sort > "${work_dir}/hooks.expected"
LC_ALL=C sort -u -o "${hooks}" "${hooks}"
cmp -s "${hooks}" "${work_dir}/hooks.expected" || {
  printf 'monitoring admission hook contract differs\n' >&2
  exit 1
}

grep -Fvx "${CERTGEN_IMAGE}" "${expected_render_images}" > "${EXPECTED_LIVE_IMAGES}"
printf '%s\n' "${CONFIG_RELOADER_IMAGE}" >> "${EXPECTED_LIVE_IMAGES}"
LC_ALL=C sort -u -o "${EXPECTED_LIVE_IMAGES}" "${EXPECTED_LIVE_IMAGES}"
chmod 0600 "${EXPECTED_LIVE_IMAGES}" "${EXPECTED_OPERATOR_ARGS}"
