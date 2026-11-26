from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_EXTERNAL_ACTIONS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1": "v7",
    "actions/setup-java@d1374f8ac74474200d16ef8ecee22c7ae7a51880": "v6",
    "docker/login-action@8aa5831fb8d48d8f76594c7ddde61ae58019f550": "v4",
    "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069": "v4",
    "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc": "v7",
}


class Phase5E1WorkflowContractTests(unittest.TestCase):
    TARGETS = [
        (
            "deploy-farm-config-server.yaml",
            "farm",
            "farm-mgmt",
            "https://192.168.2.198:6443",
            "provi-farm-mgmt-deploy",
        ),
        (
            "deploy-lab-config-server.yaml",
            "lab",
            "lab-mgmt",
            "https://192.168.1.198:6443",
            "provi-lab-mgmt-deploy",
        ),
    ]

    def test_target_workflow_has_one_fixed_local_executor_boundary(self):
        for filename, target, host, api, runner in self.TARGETS:
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text()
                self.assertIn(f"group: {runner}", source)
                self.assertIn(f"labels: {runner}", source)
                self.assertIn(f'test "$(hostname)" = "{host}"', source)
                self.assertIn(f'= "{api}"', source)
                self.assertIn("KUBECONFIG: /etc/kubernetes/ci-deployer.conf", source)
                self.assertIn('test "$(id -un)" = "svmanager"', source)
                self.assertIn(f"args=(--target {target} --action \"$ACTION\")", source)
                self.assertNotIn("appleboy/ssh-action", source)
                self.assertNotIn("K8S_PRIVATE_KEY", source)
                self.assertNotIn("ssh ", source)

    def test_both_target_workflows_share_legacy_operation_concurrency_boundary(self):
        legacy = (WORKFLOWS / "deploy-proposed-stack.yaml").read_text()
        farm = (WORKFLOWS / "deploy-farm-config-server.yaml").read_text()
        lab = (WORKFLOWS / "deploy-lab-config-server.yaml").read_text()
        # Legacy `stack=operation` resolves to the same literal group.
        self.assertIn("group: proposed-stack-${{ inputs.stack }}", legacy)
        for source in (farm, lab):
            self.assertIn("group: proposed-stack-operation", source)
            self.assertIn("cancel-in-progress: false", source)
            self.assertIn("timeout-minutes: 120", source)

    def test_upgrade_source_is_release_tag_on_protected_main(self):
        for filename, *_ in self.TARGETS:
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text()
                self.assertIn('[[ "$SOURCE_REF" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]', source)
                self.assertIn(
                    'git -C proposed merge-base --is-ancestor "$sha" refs/remotes/origin/main', source
                )
                self.assertIn(
                    'tag_commit=$(git -C proposed rev-parse "refs/tags/${SOURCE_REF}^{commit}")', source
                )
                self.assertIn('test "$tag_commit" = "$sha"', source)
                self.assertIn('test "$IMAGE_TAG" = "$SOURCE_SHA"', source)
                self.assertIn('[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]', source)

    def test_image_builder_seals_source_identity_and_registry_digest(self):
        source = (WORKFLOWS / "_build-config-server-image.yaml").read_text()
        self.assertIn("org.opencontainers.image.revision=${{ steps.source.outputs.sha }}", source)
        self.assertIn('test "$DOCKER_USERNAME" = "dguailab"', source)
        self.assertIn('[[ "$candidate" =~ ^sha256:[0-9a-f]{64}$ ]]', source)
        self.assertIn('[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]', source)
        self.assertIn('echo "tag=$SOURCE_SHA" >> "$GITHUB_OUTPUT"', source)
        self.assertIn('echo "digest=$digest" >> "$GITHUB_OUTPUT"', source)

    def test_external_actions_are_pinned_to_full_commits(self):
        targets = [
            "_build-config-server-image.yaml",
            "deploy-farm-config-server.yaml",
            "deploy-lab-config-server.yaml",
            "deploy-proposed-stack.yaml",
        ]
        for filename in targets:
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text()
                for line in source.splitlines():
                    if "uses: actions/" in line or "uses: docker/" in line:
                        action_ref = line.split("uses:", 1)[1].strip().split()[0]
                        self.assertRegex(action_ref, r"^[^@]+@[0-9a-f]{40}$")
                        self.assertIn(action_ref, PINNED_EXTERNAL_ACTIONS)
                        self.assertTrue(
                            line.rstrip().endswith(f"# {PINNED_EXTERNAL_ACTIONS[action_ref]}")
                        )

    def test_legacy_workflow_allows_operation_probe_but_blocks_mutation(self):
        source = (WORKFLOWS / "deploy-proposed-stack.yaml").read_text()
        self.assertIn("if: inputs.stack == 'operation' && inputs.action != 'probe'", source)
        self.assertIn("FARM Config Server는 Actions > Deploy FARM Config Server", source)
        self.assertIn("inputs.action != 'probe' && inputs.stack != 'operation'", source)
        self.assertIn('if [ "$STACK" = "operation" ]; then', source)
        self.assertIn("if: inputs.action == 'probe'", source)
        self.assertIn("options: [baseline, noprobe, full, operation]", source)
        self.assertIn("options: [probe, up, down]", source)
        self.assertIn('bash ops/proposed-stack/stack-up.sh "$STACK"', source)
        self.assertIn('bash ops/proposed-stack/stack-down.sh "$STACK"', source)

    def test_legacy_secret_sync_is_a_secretless_fail_closed_history_entrypoint(self):
        source = (WORKFLOWS / "sync-config-server-secrets.yaml").read_text()
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("permissions: {}", source)
        self.assertIn("Legacy secret-sync path is permanently blocked", source)
        self.assertIn("Deploy FARM Config Server or Deploy LAB Config Server", source)
        self.assertIn("exit 1", source)
        for forbidden in (
            "actions/checkout",
            "appleboy/",
            "secrets.",
            "K8S_HOST",
            "K8S_PRIVATE_KEY",
            "helm upgrade",
            "containerssh-config-server",
            "ailab-infra",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
