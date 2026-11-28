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

    def test_probe_upgrade_and_lab_activate_source_is_release_tag_on_protected_main(self):
        for filename, target, *_ in self.TARGETS:
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text()
                expected_description = (
                    'description: "probe/upgrade/activate 공통: admin_infra-proposed의 보호된 릴리스 태그(vX.Y.Z)"'
                    if target == "lab"
                    else 'description: "probe/upgrade 공통: admin_infra-proposed의 보호된 릴리스 태그(vX.Y.Z)"'
                )
                self.assertIn(expected_description, source)
                self.assertIn('[[ "$SOURCE_REF" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]', source)
                self.assertIn(
                    'git -C proposed merge-base --is-ancestor "$sha" refs/remotes/origin/main', source
                )
                self.assertIn(
                    'tag_commit=$(git -C proposed rev-parse "refs/tags/${SOURCE_REF}^{commit}")', source
                )
                self.assertIn('test "$tag_commit" = "$sha"', source)
                self.assertNotIn('test "$SOURCE_REF" = "main"', source)
                self.assertNotIn("if [ \"$ACTION\" = \"upgrade\" ]; then\n            tag_commit=", source)
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

    def test_farm_upgrade_requires_the_exact_lab_deployed_image_digest(self):
        farm = (WORKFLOWS / "deploy-farm-config-server.yaml").read_text()
        lab = (WORKFLOWS / "deploy-lab-config-server.yaml").read_text()

        self.assertIn(
            '''      expected_image_digest:
        description: "upgrade: 성공한 LAB run의 LAB_DEPLOYED_IMAGE_DIGEST / probe: 비워 둠"
        required: false
        type: string
        default: ""''',
            farm,
        )
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ inputs.expected_image_digest }}", farm)
        self.assertIn('test -z "$EXPECTED_IMAGE_DIGEST" || {', farm)
        self.assertIn(
            '[[ "$EXPECTED_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {', farm
        )
        self.assertIn("image_contract:", farm)
        self.assertIn("BUILT_IMAGE_DIGEST: ${{ needs.image.outputs.image_digest }}", farm)
        self.assertIn('[[ "$BUILT_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]', farm)
        self.assertIn('test "$BUILT_IMAGE_DIGEST" = "$EXPECTED_IMAGE_DIGEST" || {', farm)
        self.assertIn("needs: [source, image, image_contract]", farm)
        self.assertIn("needs.image_contract.result == 'success'", farm)
        contract = farm.split("  image_contract:", 1)[1].split("\n  deploy:", 1)[0]
        self.assertIn("runs-on: ubuntu-24.04", contract)
        self.assertNotIn("provi-farm-mgmt-deploy", contract)
        self.assertLess(
            farm.index("  image_contract:"),
            farm.index("      group: provi-farm-mgmt-deploy"),
        )
        self.assertNotIn("expected_image_digest:", lab)
        self.assertIn("FARM 승격용 LAB 성공 digest 기록", lab)
        self.assertIn('echo "LAB_DEPLOYED_IMAGE_DIGEST=$IMAGE_DIGEST"', lab)
        self.assertIn(
            "FARM upgrade의 expected_image_digest 입력에 아래 값을 그대로 사용한다.", lab
        )
        self.assertLess(
            lab.index('bash proposed/ops/cluster-config/deploy.sh "${args[@]}"'),
            lab.index("FARM 승격용 LAB 성공 digest 기록"),
        )

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
