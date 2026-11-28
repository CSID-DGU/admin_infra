from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-master-admin.yaml"


class Phase5G2BMasterAdminWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()

    def test_actions_and_shared_operation_concurrency_are_exact(self):
        self.assertIn("options: [probe, restore, shadow, activate, rollback]", self.source)
        self.assertIn("group: proposed-stack-operation", self.source)
        self.assertIn("cancel-in-progress: false", self.source)

    def test_receipt_inputs_are_fail_closed_by_action(self):
        self.assertIn("mysql_receipt_sha256:", self.source)
        self.assertIn("redis_receipt_sha256:", self.source)
        validation = self.source.split("- name: 입력의 상호배타 경계 검증", 1)[1].split(
            "- name: Master Admin chart 소스 체크아웃", 1
        )[0]
        self.assertIn('[[ "$MYSQL_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]', validation)
        self.assertIn('[[ "$REDIS_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]', validation)
        self.assertIn("probe|restore)", validation)
        self.assertIn("shadow)", validation)
        self.assertIn("activate)", validation)

    def test_builds_are_hosted_and_only_shadow_builds_images(self):
        boundaries = {
            "backend-image": "frontend-image",
            "frontend-image": "execute",
        }
        for job, next_job in boundaries.items():
            block = self.source.split(f"\n  {job}:\n", 1)[1].split(
                f"\n  {next_job}:\n", 1
            )[0]
            self.assertIn("if: inputs.action == 'shadow'", block)
            self.assertIn("runs-on: ubuntu-24.04", block)
            self.assertNotIn("provi-master-deploy", block)
        self.assertIn("./gradlew build", self.source)
        self.assertIn("org.opencontainers.image.revision=", self.source)

    def test_exact_master_runner_and_kubeconfig_are_fail_closed(self):
        execute = self.source.split("  execute:", 1)[1]
        self.assertIn("group: provi-master-deploy", execute)
        self.assertIn("labels: provi-master-deploy", execute)
        self.assertIn('test "$(hostname)" = "CSID-DGU-Desktop"', execute)
        self.assertIn('test "$(id -un)" = "svmanager"', execute)
        self.assertIn("KUBECONFIG: /etc/kubernetes/provi-master-ci-deployer.conf", execute)
        self.assertIn('test "$(kubectl config current-context)" = "provi-master"', execute)
        self.assertIn('= "https://192.168.2.199:6443"', execute)
        self.assertIn("provi-master", execute)
        self.assertIn(".items | length", execute)

    def test_source_checkouts_include_chart_and_backend_refs(self):
        self.assertIn("repository: CSID-DGU/admin_infra-proposed", self.source)
        self.assertIn("ref: ${{ inputs.proposed_ref }}", self.source)
        self.assertIn("repository: CSID-DGU/admin_be", self.source)
        self.assertIn("ref: ${{ inputs.be_ref }}", self.source)
        self.assertIn("proposed/ops/master-admin/Chart/Chart.yaml", self.source)
        self.assertIn("RuntimeModeProperties.java", self.source)
        self.assertIn("ShadowModeMutationFilter.java", self.source)

    def test_shadow_forces_read_only_and_no_ingress(self):
        shadow = self.source.rsplit("            shadow)", 1)[1].split("            activate)", 1)[0]
        self.assertIn("--set-string dataMode=SHADOW", shadow)
        self.assertIn("--set-string runtimeMode=SHADOW", shadow)
        self.assertIn("--set ingress.enabled=false", shadow)
        self.assertIn("--set mysql.readOnly=true", shadow)
        self.assertIn("--set redis.readOnly=true", shadow)
        self.assertIn('mysql.expectedReceiptSha256="$MYSQL_RECEIPT_SHA256"', shadow)
        self.assertIn('redis.expectedReceiptSha256="$REDIS_RECEIPT_SHA256"', shadow)
        self.assertIn("--set activation.confirmSingleWriter=false", shadow)
        self.assertIn("--set adminBackend.farmReadOnlyCredential.enabled=true", shadow)
        self.assertIn(
            '--set-string "networkPolicy.backendExtraEgress[0].cidr=$FARM_READONLY_CIDR"',
            shadow,
        )
        self.assertIn(
            '--set "networkPolicy.backendExtraEgress[0].ports[0]=$FARM_CONFIG_SERVER_PORT"',
            shadow,
        )
        self.assertIn(
            '--set "networkPolicy.backendExtraEgress[0].ports[1]=$FARM_API_PORT"',
            shadow,
        )
        self.assertIn("--set networkPolicy.backendMonitoringEgress.enabled=true", shadow)
        self.assertIn('mysql.image.digest="$MYSQL_DIGEST"', shadow)
        self.assertIn('redis.image.digest="$REDIS_DIGEST"', shadow)
        # A failed receipt/init gate must leave the new read-only/blocked
        # StatefulSet in place. Automatic rollback would reopen RESTORE, whose
        # datastores are intentionally writable for the importer.
        master_upgrade = shadow.split('helm upgrade --install "$MASTER_RELEASE"', 1)[1]
        self.assertNotIn("--rollback-on-failure", master_upgrade)
        self.assertNotIn("kubectl delete", shadow)
        self.assertNotIn("helm uninstall", shadow)

    def test_farm_credential_is_precreated_read_only_and_live_mounted(self):
        self.assertIn("FARM_READONLY_CIDR: 192.168.2.198/32", self.source)
        self.assertIn('FARM_API_PORT: "6443"', self.source)
        self.assertIn('FARM_CONFIG_SERVER_PORT: "30482"', self.source)
        self.assertIn(
            'get secret master-admin-farm-readonly',
            self.source,
        )
        self.assertIn("require_secret_key master-admin-farm-readonly kubeconfig", self.source)
        self.assertIn("select(.immutable == true", self.source)
        self.assertIn(
            "system:serviceaccount:ailab-operation:provi-master-admin-shadow-reader",
            self.source,
        )
        for denied in (
            "auth can-i get secrets -n ailab-operation",
            "auth can-i create pods -n ailab-operation",
            "auth can-i patch deployments -n ailab-operation",
            "auth can-i get nodes",
            "auth can-i list pods -n default",
        ):
            self.assertIn(denied, self.source)
        self.assertIn('secretName == "master-admin-farm-readonly"', self.source)
        self.assertIn('mountPath == "/etc/provi/kubeconfigs/farm-readonly.yaml"', self.source)
        self.assertIn('"kubernetes.io/metadata.name":"provi-monitoring"', self.source)
        self.assertIn('"prometheus":"master-monitoring-kube-prometheus-prometheus"', self.source)

    def test_restore_is_initial_only_writable_datastore_with_no_apps_or_ingress(self):
        restore = self.source.rsplit("            restore)", 1)[1].split(
            "            shadow)", 1
        )[0]
        self.assertIn("--set-string dataMode=RESTORE", restore)
        self.assertIn("--set-string runtimeMode=SHADOW", restore)
        self.assertIn("--set adminBackend.replicas=0", restore)
        self.assertIn("--set adminFrontend.replicas=0", restore)
        self.assertIn("--set ingress.enabled=false", restore)
        self.assertIn("--set mysql.readOnly=false", restore)
        self.assertIn("--set redis.readOnly=false", restore)
        self.assertIn("helm list --all", restore)
        self.assertIn('test -z "$MYSQL_RECEIPT_SHA256"', restore)
        self.assertIn('test -z "$REDIS_RECEIPT_SHA256"', restore)
        self.assertNotIn("kubectl delete", restore)
        self.assertNotIn("helm uninstall", restore)

    def test_shadow_installs_separate_pinned_master_monitoring_release(self):
        self.assertIn("MONITORING_NAMESPACE: provi-monitoring", self.source)
        self.assertIn("MONITORING_RELEASE: master-monitoring", self.source)
        self.assertIn("MONITORING_CHART_VERSION: 80.2.2", self.source)
        self.assertIn(
            "MONITORING_CHART_SHA256: 33896f603cdbbcaad03ee618cda2da61ce6549b469582682a85d715ae4aea1d3",
            self.source,
        )
        self.assertIn("proposed/ops/master-monitoring/values-shadow.yaml", self.source)
        self.assertIn("sha256sum --check --strict", self.source)
        self.assertIn('helm upgrade --install "$MONITORING_RELEASE"', self.source)
        self.assertIn('helm upgrade --install "$MASTER_RELEASE"', self.source)
        self.assertIn('wait_ready_pods "prometheus=$prometheus_name"', self.source)
        self.assertIn('wait_ready_pods "alertmanager=$alertmanager_name"', self.source)
        self.assertNotIn("rollout status statefulset \\\n                -l app.kubernetes.io/instance=", self.source)
        self.assertNotIn("ailab-monitoring", self.source)

    def test_activate_requires_the_complete_cutover_receipt(self):
        activate = self.source.rsplit("            activate)", 1)[1].split("            rollback)", 1)[0]
        for key in (
            "verified_at",
            "mysql_snapshot_sha256",
            "redis_rdb_sha256",
            "source_freeze_receipt_sha256",
            "target_verify_receipt_sha256",
        ):
            self.assertIn(key, self.source)
        self.assertIn("validate_cutover_receipt", activate)
        self.assertIn("assert_live_shadow_identity", activate)
        self.assertIn(".immutable == true", self.source)
        self.assertIn('test "$value" = "$MYSQL_RECEIPT_SHA256"', self.source)
        self.assertIn('test "$value" = "$REDIS_RECEIPT_SHA256"', self.source)
        self.assertIn("--set-string dataMode=ACTIVE", activate)
        self.assertIn("--set-string runtimeMode=ACTIVE", activate)
        self.assertIn("--set activation.confirmSingleWriter=true", activate)
        self.assertIn('mysql.expectedReceiptSha256="$MYSQL_RECEIPT_SHA256"', activate)
        self.assertIn('redis.expectedReceiptSha256="$REDIS_RECEIPT_SHA256"', activate)
        self.assertIn("--set adminBackend.replicas=0", activate)
        self.assertIn("--set adminFrontend.replicas=0", activate)
        self.assertIn("--set adminBackend.replicas=1", activate)
        self.assertIn("--set adminFrontend.replicas=1", activate)
        self.assertIn('statefulset/"$MASTER_RELEASE-mysql"', activate)
        self.assertIn('statefulset/"$MASTER_RELEASE-redis"', activate)
        self.assertEqual(activate.count('helm upgrade "$MASTER_RELEASE"'), 2)
        self.assertLess(activate.index("validate_cutover_receipt"), activate.index("helm upgrade"))

    def test_rollback_can_only_target_a_shadow_revision(self):
        rollback = self.source.rsplit("            rollback)", 1)[1].split("            *)", 1)[0]
        self.assertIn("--revision \"$ROLLBACK_REVISION\"", rollback)
        self.assertIn(".dataMode", rollback)
        self.assertIn(".runtimeMode", rollback)
        self.assertIn(".mysql.expectedReceiptSha256", rollback)
        self.assertIn(".redis.expectedReceiptSha256", rollback)
        self.assertIn("SHADOW", rollback)
        self.assertIn(".ingress.enabled", rollback)
        self.assertIn("helm rollback", rollback)
        self.assertNotIn("kubectl delete", rollback)
        self.assertNotIn("helm uninstall", rollback)

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
