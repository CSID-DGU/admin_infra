import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-master-admin.yaml"
RULES_VALIDATOR = ROOT / ".github" / "scripts" / "phase5g2b-reader-effective-rules-validator.jq"
MONITORING_VALIDATOR = ROOT / ".github" / "scripts" / "phase5g2b-monitoring-render-validator.sh"
MONITORING_IMAGE_PINS = ROOT / ".github" / "config" / "master-monitoring-image-pins.yaml"

PROPOSED_SHA = "9600e377c15ed9f678ae039dd7daa72d615039b4"
FRONTEND_SHA = "bb1c5f80c9313619d7897c3a25a96032d2b2f6e4"
BACKEND_SHA = "d9be83b8fe699ba034fa5b3d5f50ce945d61c980"

MONITORING_RENDER_FIXTURE = """---
apiVersion: v1
kind: Service
metadata:
  name: master-monitoring
spec:
  type: ClusterIP
  ports:
    - port: 9090
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: master-monitoring-operator
spec:
  template:
    spec:
      hostNetwork: false
      initContainers:
        - name: busybox
          image: docker.io/library/busybox:1.31.1@sha256:95cf004f559831017cdf4628aaf1bb30133677be8702a8c5f2994629f637a209
      containers:
        - name: grafana
          image: docker.io/grafana/grafana:12.3.0@sha256:70d9599b186ce287be0d2c5ba9a78acb2e86c1a68c9c41449454d0fc3eeb84e8
        - name: sidecar
          image: quay.io/kiwigrid/k8s-sidecar:2.1.2@sha256:716b0b33ff2dc938a3f2bc64e5ea791d81fb09760bcd27cec1eb896968d6e134
        - name: operator
          image: quay.io/prometheus-operator/prometheus-operator:v0.87.1@sha256:6dbbbbeca6d7b94aa30723bfaa55262e41b9f8c15304c484611c696503840aac
          args:
            - --prometheus-config-reloader=quay.io/prometheus-operator/prometheus-config-reloader:v0.87.1@sha256:257ce3543e8f45c059dab488bcf897d8cc598cabd78afba12f41d6dbe36e22d2
            - --thanos-default-base-image=quay.io/thanos/thanos:v0.40.1@sha256:aae7b2b030ed00d88abd8c5a357d08b46f22151ca0d50d9c796223fa332389c8
        - name: node-exporter
          image: quay.io/prometheus/node-exporter:v1.10.2@sha256:337ff1d356b68d39cef853e8c6345de11ce7556bb34cda8bd205bcf2ed30b565
        - name: kube-state-metrics
          image: registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0@sha256:2bbc915567334b13632bf62c0a97084aff72a36e13c4dabd5f2f11c898c5bacd
---
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: master-monitoring-prometheus
spec:
  image: quay.io/prometheus/prometheus:v3.8.0@sha256:d936808bdea528155c0154a922cd42fd75716b8bb7ba302641350f9f3eaeba09
---
apiVersion: monitoring.coreos.com/v1
kind: Alertmanager
metadata:
  name: master-monitoring-alertmanager
spec:
  image: quay.io/prometheus/alertmanager:v0.29.0@sha256:88743b63b3e09ea6e31e140ced5bf45f4a8e82c617c2a963f78841f4995ad1d7
---
apiVersion: batch/v1
kind: Job
metadata:
  name: master-monitoring-admission-create
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
spec:
  template:
    spec:
      containers:
        - name: create
          image: ghcr.io/jkroepke/kube-webhook-certgen:1.7.3@sha256:c7f925cc3ec386e11b7393f941a8844786311fd2328943fe3e7c744c373ed864
---
apiVersion: batch/v1
kind: Job
metadata:
  name: master-monitoring-admission-patch
  annotations:
    "helm.sh/hook": post-install,post-upgrade
spec:
  template:
    spec:
      containers:
        - name: patch
          image: ghcr.io/jkroepke/kube-webhook-certgen:1.7.3@sha256:c7f925cc3ec386e11b7393f941a8844786311fd2328943fe3e7c744c373ed864
"""


def valid_reader_rules_review():
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectRulesReview",
        "metadata": {},
        "spec": {},
        "status": {
            "incomplete": False,
            "resourceRules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
                {
                    "apiGroups": ["authorization.k8s.io"],
                    "resources": ["selfsubjectaccessreviews", "selfsubjectrulesreviews"],
                    "verbs": ["create"],
                },
                {
                    "apiGroups": ["authentication.k8s.io"],
                    "resources": ["selfsubjectreviews"],
                    "verbs": ["create"],
                },
            ],
            "nonResourceRules": [
                {"nonResourceURLs": ["/api", "/api/*", "/version"], "verbs": ["get"]}
            ],
        },
    }


class Phase5G2BMasterAdminWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()

    def validator_accepts(self, fixture):
        completed = subprocess.run(
            [
                "jq",
                "-e",
                "-f",
                str(RULES_VALIDATOR),
            ],
            input=json.dumps(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed.returncode == 0

    def monitoring_validator_accepts(self, rendered):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            render_path = directory / "rendered.yaml"
            render_path.write_text(rendered)
            completed = subprocess.run(
                [
                    "bash",
                    str(MONITORING_VALIDATOR),
                    str(render_path),
                    str(directory / "live-images.txt"),
                    str(directory / "operator-args.txt"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            return completed.returncode == 0

    def test_only_probe_restore_shadow_can_be_dispatched(self):
        self.assertIn("options: [probe, restore, shadow]", self.source)
        self.assertNotIn("options: [probe, restore, shadow,", self.source)
        self.assertNotIn("cutover_receipt_sha256:", self.source)
        self.assertNotIn("rollback_revision:", self.source)
        self.assertNotIn("            activate)", self.source)
        self.assertNotIn("            rollback)", self.source)
        self.assertNotIn("--set-string dataMode=ACTIVE", self.source)
        self.assertNotIn("--set-string runtimeMode=ACTIVE", self.source)
        self.assertNotIn("--set activation.confirmSingleWriter=true", self.source)
        self.assertNotIn("--set ingress.enabled=true", self.source)
        self.assertNotIn("helm rollback", self.source)

    def test_merge_alone_cannot_dispatch_the_workflow(self):
        trigger = self.source.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertNotIn("pull_request:", trigger)

    def test_shared_operation_concurrency_is_exact(self):
        self.assertIn("group: proposed-stack-operation", self.source)
        self.assertIn("cancel-in-progress: false", self.source)

    def test_receipt_inputs_are_fail_closed_by_action(self):
        validation = self.source.split("- name: 입력의 상호배타 경계 검증", 1)[1].split(
            "- name: Master Admin chart 소스 체크아웃", 1
        )[0]
        self.assertIn("probe|restore)", validation)
        self.assertIn("shadow)", validation)
        self.assertIn('test -z "$MYSQL_RECEIPT_SHA256"', validation)
        self.assertIn('test -z "$REDIS_RECEIPT_SHA256"', validation)
        self.assertIn('[[ "$MYSQL_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]', validation)
        self.assertIn('[[ "$REDIS_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]', validation)

    def test_all_three_sources_are_hard_pinned_and_reverified(self):
        for sha, repository, variable in (
            (PROPOSED_SHA, "CSID-DGU/admin_infra-proposed", "REVIEWED_PROPOSED_SHA"),
            (FRONTEND_SHA, "CSID-DGU/admin_fe", "REVIEWED_FRONTEND_SHA"),
            (BACKEND_SHA, "CSID-DGU/admin_be", "REVIEWED_BACKEND_SHA"),
        ):
            with self.subTest(repository=repository):
                self.assertIn(f"repository: {repository}", self.source)
                self.assertIn(f"ref: {sha}", self.source)
                self.assertIn(f"{variable}: {sha}", self.source)
                self.assertIn(f'= "${variable}"', self.source)
        for forbidden in ("inputs.proposed_ref", "inputs.fe_ref", "inputs.be_ref"):
            self.assertNotIn(forbidden, self.source)

    def test_builds_are_hosted_and_only_shadow_builds_images(self):
        boundaries = {"backend-image": "frontend-image", "frontend-image": "execute"}
        for job, next_job in boundaries.items():
            block = self.source.split(f"\n  {job}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]
            self.assertIn("if: inputs.action == 'shadow'", block)
            self.assertIn("runs-on: ubuntu-24.04", block)
            self.assertNotIn("provi-master-deploy", block)
        self.assertIn("./gradlew build", self.source)
        self.assertIn("org.opencontainers.image.revision=", self.source)

    def test_exact_master_runner_kubeconfig_and_current_workflow_are_pinned(self):
        execute = self.source.split("  execute:", 1)[1]
        self.assertIn("group: provi-master-deploy", execute)
        self.assertIn("labels: provi-master-deploy", execute)
        self.assertIn("repository: CSID-DGU/admin_infra", execute)
        self.assertIn("ref: ${{ github.sha }}", execute)
        self.assertIn('test "$(git -C workflow-source rev-parse HEAD)" = "$GITHUB_SHA"', execute)
        self.assertIn('test "$(hostname)" = "CSID-DGU-Desktop"', execute)
        self.assertIn('test "$(id -un)" = "svmanager"', execute)
        self.assertIn("KUBECONFIG: /etc/kubernetes/provi-master-ci-deployer.conf", execute)
        self.assertIn('test "$(kubectl config current-context)" = "provi-master"', execute)
        self.assertIn('= "https://192.168.2.199:6443"', execute)

    def test_farm_credential_uses_fail_closed_effective_rule_review(self):
        validator = RULES_VALIDATOR.read_text()
        self.assertIn("SelfSubjectRulesReview", self.source)
        self.assertIn("selfsubjectrulesreviews", self.source)
        self.assertIn("phase5g2b-reader-effective-rules-validator.jq", self.source)
        self.assertIn(".status.incomplete == false", validator)
        self.assertIn('all(.apiGroups[]; . != "*")', validator)
        self.assertIn('all(.resources[]; . != "*")', validator)
        self.assertIn('all(.verbs[]; . != "*")', validator)
        self.assertIn("resourceNames", validator)
        self.assertIn("(.spec == {})", validator)
        self.assertNotIn("expected_namespace", validator)
        self.assertTrue(self.validator_accepts(valid_reader_rules_review()))

    def test_ssrr_request_is_exact_validated_before_posting_the_same_file(self):
        boundary = self.source.split("require_precreated_secrets()", 1)[1].split(
            "require_fresh_restore_storage()", 1
        )[0]
        request_path = (
            'farm_ssrr_request="$RUNNER_TEMP/'
            'farm-reader-self-subject-rules-review-request.json"'
        )
        response_path = (
            'farm_ssrr_response="$RUNNER_TEMP/'
            'farm-reader-self-subject-rules-review-response.json"'
        )
        exact_request_check = '. == {'
        post_same_file = '-f "$farm_ssrr_request" > "$farm_ssrr_response"'
        response_validation = '"$farm_ssrr_response" >/dev/null'
        exact_request_input = '"$farm_ssrr_request" >/dev/null'
        for expected in (
            request_path,
            response_path,
            'jq -n --arg namespace "$FARM_NAMESPACE"',
            'spec: {namespace: $expected_namespace}',
            exact_request_check,
            post_same_file,
            response_validation,
            exact_request_input,
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, boundary)
        self.assertLess(boundary.index(exact_request_check), boundary.index(post_same_file))
        self.assertLess(boundary.index(exact_request_input), boundary.index(post_same_file))
        self.assertLess(boundary.index(post_same_file), boundary.index(response_validation))

    def test_effective_rule_validator_requires_canonical_empty_response_spec(self):
        echoed_request_spec = valid_reader_rules_review()
        echoed_request_spec["spec"] = {"namespace": "ailab-operation"}
        missing_spec = valid_reader_rules_review()
        missing_spec.pop("spec")
        nonempty_spec = valid_reader_rules_review()
        nonempty_spec["spec"] = {"unexpected": True}
        for label, fixture in (
            ("echoed-request-spec", echoed_request_spec),
            ("missing-spec", missing_spec),
            ("nonempty-spec", nonempty_spec),
        ):
            with self.subTest(label=label):
                self.assertFalse(self.validator_accepts(fixture))

    def test_effective_rule_validator_rejects_incomplete_wildcard_and_extras(self):
        mutations = []
        incomplete = valid_reader_rules_review()
        incomplete["status"]["incomplete"] = True
        mutations.append(("incomplete", incomplete))
        evaluation_error = valid_reader_rules_review()
        evaluation_error["status"]["evaluationError"] = "rules are incomplete"
        mutations.append(("evaluation-error", evaluation_error))
        wildcard_api_group = valid_reader_rules_review()
        wildcard_api_group["status"]["resourceRules"][0]["apiGroups"] = ["*"]
        mutations.append(("wildcard-api-group", wildcard_api_group))
        wildcard_verb = valid_reader_rules_review()
        wildcard_verb["status"]["resourceRules"][0]["verbs"].append("*")
        mutations.append(("wildcard-verb", wildcard_verb))
        wildcard_resource = valid_reader_rules_review()
        wildcard_resource["status"]["resourceRules"][0]["resources"].append("*")
        mutations.append(("wildcard-resource", wildcard_resource))
        resource_names = valid_reader_rules_review()
        resource_names["status"]["resourceRules"][0]["resourceNames"] = ["one-pod"]
        mutations.append(("resourceNames", resource_names))
        pods_exec = valid_reader_rules_review()
        pods_exec["status"]["resourceRules"].append(
            {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]}
        )
        mutations.append(("pods-exec", pods_exec))
        extra_watch = valid_reader_rules_review()
        extra_watch["status"]["resourceRules"][0]["verbs"].append("watch")
        mutations.append(("extra-watch", extra_watch))
        nonresource_wildcard = valid_reader_rules_review()
        nonresource_wildcard["status"]["nonResourceRules"][0]["nonResourceURLs"].append("*")
        mutations.append(("nonresource-wildcard", nonresource_wildcard))
        extra_nonresource = valid_reader_rules_review()
        extra_nonresource["status"]["nonResourceRules"][0]["nonResourceURLs"].append("/metrics")
        mutations.append(("extra-nonresource", extra_nonresource))
        missing_required = valid_reader_rules_review()
        missing_required["status"]["resourceRules"] = missing_required["status"]["resourceRules"][1:]
        mutations.append(("missing-required", missing_required))
        for label, fixture in mutations:
            with self.subTest(label=label):
                self.assertFalse(self.validator_accepts(fixture))

    def test_defense_in_depth_denies_source_exec_and_writes(self):
        for denied in (
            "auth can-i get secrets",
            "auth can-i create pods ",
            "auth can-i create pods/exec",
            "auth can-i create pods/attach",
            "auth can-i create pods/portforward",
            "auth can-i update pods/ephemeralcontainers",
            "auth can-i create jobs.batch",
            "auth can-i delete pods",
            "auth can-i patch deployments.apps",
            "auth can-i impersonate users",
            "auth can-i get nodes",
            "auth can-i list pods -n default",
        ):
            with self.subTest(denied=denied):
                self.assertIn(denied, self.source)

    def test_restore_requires_fresh_release_storageclass_pvs_and_pvcs(self):
        restore = self.source.rsplit("            restore)", 1)[1].split("            shadow)", 1)[0]
        gate = self.source.split("require_fresh_restore_storage()", 1)[1].split(
            "assert_namespace_not_exposed()", 1
        )[0]
        self.assertIn("helm list --all", gate)
        for name in (
            "master-admin-mysql",
            "master-admin-redis",
            "master-admin-backup",
            "provi-master-admin-mysql",
            "provi-master-admin-redis",
            "provi-master-admin-backup",
            "provi-master-admin-local",
        ):
            self.assertIn(name, gate)
        self.assertIn("--ignore-not-found -o name", gate)
        self.assertLess(
            restore.index("require_fresh_restore_storage"),
            restore.index('helm upgrade --install "$MASTER_RELEASE"'),
        )

    def test_restore_is_initial_only_writable_datastore_with_no_apps(self):
        restore = self.source.rsplit("            restore)", 1)[1].split("            shadow)", 1)[0]
        for expected in (
            "--set-string dataMode=RESTORE",
            "--set-string runtimeMode=SHADOW",
            "--set adminBackend.replicas=0",
            "--set adminFrontend.replicas=0",
            "--set ingress.enabled=false",
            "--set mysql.readOnly=false",
            "--set redis.readOnly=false",
            "--set activation.confirmSingleWriter=false",
            "assert_namespace_not_exposed",
        ):
            self.assertIn(expected, restore)
        self.assertNotIn("kubectl delete", restore)
        self.assertNotIn("helm uninstall", restore)

    def test_shadow_forces_read_only_and_no_external_exposure(self):
        shadow = self.source.rsplit("\n            shadow)", 1)[1].split("\n            *)", 1)[0]
        for expected in (
            "--set-string dataMode=SHADOW",
            "--set-string runtimeMode=SHADOW",
            "--set ingress.enabled=false",
            "--set mysql.readOnly=true",
            "--set redis.readOnly=true",
            "--set activation.confirmSingleWriter=false",
            "assert_namespace_not_exposed",
        ):
            self.assertIn(expected, shadow)
        # A failed receipt/init gate must leave the target StatefulSets in the
        # new read-only/blocked state.  Automatic rollback would reopen the
        # prior RESTORE release, whose datastores are intentionally writable
        # only for the one-time importer.
        master_upgrade = shadow.split('helm upgrade --install "$MASTER_RELEASE"', 1)[1]
        self.assertNotIn("--rollback-on-failure", master_upgrade)
        self.assertIn('mysql.expectedReceiptSha256="$MYSQL_RECEIPT_SHA256"', shadow)
        self.assertIn('redis.expectedReceiptSha256="$REDIS_RECEIPT_SHA256"', shadow)
        self.assertNotIn("kubectl delete", shadow)
        self.assertNotIn("helm uninstall", shadow)

    def test_render_and_live_exposure_assertions_are_exhaustive(self):
        for expected in (
            "assert_render_not_exposed",
            "assert_namespace_not_exposed",
            "master-admin-backend\\tClusterIP\\tcluster",
            "master-admin-frontend\\tClusterIP\\tcluster",
            "master-admin-mysql\\tClusterIP\\theadless",
            "master-admin-redis\\tClusterIP\\theadless",
            '(.spec.type // "ClusterIP") == "ClusterIP"',
            "externalIPs",
            "nodePort",
            "loadBalancerIP",
            "loadBalancerClass",
            "hostNetwork",
            "hostPort",
            "Ingress|Gateway|HTTPRoute|GRPCRoute|TCPRoute|TLSRoute|UDPRoute|Route",
            "ingresses.networking.k8s.io",
            "gateways.gateway.networking.k8s.io",
            "httproutes.gateway.networking.k8s.io",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.source)

    def test_shadow_installs_separate_pinned_master_monitoring_release(self):
        self.assertIn("MONITORING_NAMESPACE: provi-monitoring", self.source)
        self.assertIn("MONITORING_RELEASE: master-monitoring", self.source)
        self.assertIn("MONITORING_CHART_VERSION: 80.2.2", self.source)
        self.assertIn(
            "MONITORING_CHART_SHA256: 33896f603cdbbcaad03ee618cda2da61ce6549b469582682a85d715ae4aea1d3",
            self.source,
        )
        self.assertIn("sha256sum --check --strict", self.source)
        self.assertIn('helm upgrade --install "$MONITORING_RELEASE"', self.source)
        self.assertEqual(
            self.source.count("--set prometheus-node-exporter.hostNetwork=false"), 2
        )
        self.assertEqual(
            self.source.count("--set prometheus-node-exporter.hostPID=false"), 2
        )
        self.assertEqual(
            self.source.count(
                "--values workflow-source/.github/config/master-monitoring-image-pins.yaml"
            ),
            2,
        )
        self.assertIn("phase5g2b-monitoring-render-validator.sh", self.source)
        self.assertIn("assert_monitoring_namespace_not_exposed", self.source)
        monitoring_assertion = self.source.split(
            "assert_monitoring_namespace_not_exposed()", 1
        )[1].split("live_values()", 1)[0]
        self.assertIn("MONITORING_RELEASE-prometheus-node-exporter", monitoring_assertion)
        self.assertIn(".spec.template.spec.hostNetwork == false", monitoring_assertion)
        self.assertIn(".spec.template.spec.hostPID == false", monitoring_assertion)
        self.assertIn("hostPort", monitoring_assertion)
        self.assertIn("ingresses.networking.k8s.io", monitoring_assertion)

    def test_monitoring_images_and_operator_defaults_are_exactly_digest_pinned(self):
        pins = MONITORING_IMAGE_PINS.read_text()
        validator = MONITORING_VALIDATOR.read_text()
        self.assertIn("MONITORING_IMAGE_VALUES_SHA256: 9cc6b513ca6d0897549f2c06cc3bd4d09a0040574f0cd2addaad899eabf9acae", self.source)
        self.assertIn("testFramework:\n    enabled: false", pins)
        for digest in (
            "70d9599b186ce287be0d2c5ba9a78acb2e86c1a68c9c41449454d0fc3eeb84e8",
            "95cf004f559831017cdf4628aaf1bb30133677be8702a8c5f2994629f637a209",
            "716b0b33ff2dc938a3f2bc64e5ea791d81fb09760bcd27cec1eb896968d6e134",
            "2bbc915567334b13632bf62c0a97084aff72a36e13c4dabd5f2f11c898c5bacd",
            "337ff1d356b68d39cef853e8c6345de11ce7556bb34cda8bd205bcf2ed30b565",
            "6dbbbbeca6d7b94aa30723bfaa55262e41b9f8c15304c484611c696503840aac",
            "257ce3543e8f45c059dab488bcf897d8cc598cabd78afba12f41d6dbe36e22d2",
            "c7f925cc3ec386e11b7393f941a8844786311fd2328943fe3e7c744c373ed864",
            "aae7b2b030ed00d88abd8c5a357d08b46f22151ca0d50d9c796223fa332389c8",
            "88743b63b3e09ea6e31e140ced5bf45f4a8e82c617c2a963f78841f4995ad1d7",
            "d936808bdea528155c0154a922cd42fd75716b8bb7ba302641350f9f3eaeba09",
        ):
            with self.subTest(digest=digest):
                self.assertIn(digest, pins)
                self.assertIn(digest, self.source + validator)

    def test_monitoring_render_validator_is_offline_and_fail_closed(self):
        validator = MONITORING_VALIDATOR.read_text()
        self.assertNotIn("kubectl", validator)
        self.assertNotIn("jq", validator)
        self.assertTrue(self.monitoring_validator_accepts(MONITORING_RENDER_FIXTURE))
        unsafe_variants = {
            "nodeport": MONITORING_RENDER_FIXTURE.replace(
                "  type: ClusterIP", "  type: NodePort", 1
            ),
            "host-network": MONITORING_RENDER_FIXTURE.replace(
                "      hostNetwork: false", "      hostNetwork: true", 1
            ),
            "host-port": MONITORING_RENDER_FIXTURE.replace(
                "          image: quay.io/prometheus/node-exporter",
                "          hostPort: 9100\n          image: quay.io/prometheus/node-exporter",
                1,
            ),
            "tag-only-image": MONITORING_RENDER_FIXTURE.replace(
                "@sha256:70d9599b186ce287be0d2c5ba9a78acb2e86c1a68c9c41449454d0fc3eeb84e8",
                "",
                1,
            ),
            "tag-only-operator-default": MONITORING_RENDER_FIXTURE.replace(
                "@sha256:aae7b2b030ed00d88abd8c5a357d08b46f22151ca0d50d9c796223fa332389c8",
                "",
                1,
            ),
        }
        for label, rendered in unsafe_variants.items():
            with self.subTest(label=label):
                self.assertFalse(self.monitoring_validator_accepts(rendered))

    def test_workflow_never_deletes_data_or_uses_ssh(self):
        for forbidden in (
            "kubectl delete",
            "helm uninstall",
            "kubeadm reset",
            "rm -rf",
            "appleboy/ssh-action",
            "K8S_PRIVATE_KEY",
            "ssh ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_external_actions_are_pinned_to_full_commits(self):
        for line in self.source.splitlines():
            if "uses: actions/" in line or "uses: docker/" in line:
                action_ref = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(action_ref, r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
