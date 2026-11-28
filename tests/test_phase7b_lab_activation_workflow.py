from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/deploy-lab-config-server.yaml"
)


def test_activate_builds_image_and_runs_only_on_restricted_lab_runner():
    source = WORKFLOW.read_text()
    assert "options: [probe, upgrade, activate]" in source
    assert "inputs.action == 'upgrade' || inputs.action == 'activate'" in source
    assert "group: provi-lab-mgmt-deploy" in source
    assert "labels: provi-lab-mgmt-deploy" in source
    assert 'test "$(hostname)" = "lab-mgmt"' in source


def test_activate_passes_only_reviewed_digests_not_secret_values():
    source = WORKFLOW.read_text()
    assert "vars.LAB_PHASE7B_PROFILE_SHA256" in source
    assert "vars.LAB_PHASE7B_EXCLUDED_BASELINE_SHA256" in source
    assert '--profile-sha256 "$LAB_PHASE7B_PROFILE_SHA256"' in source
    assert '--excluded-baseline-sha256 "$LAB_PHASE7B_EXCLUDED_BASELINE_SHA256"' in source
    activate_block = source.split('if [ "$ACTION" = "activate" ]; then', 1)[1]
    assert "secrets." not in activate_block
    assert "kubectl create secret" not in source
    assert "kubectl delete secret" not in source


def test_activate_is_release_tag_and_exact_source_bound():
    source = WORKFLOW.read_text()
    assert 'upgrade|activate)' in source
    assert '[[ "$SOURCE_REF" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in source
    assert 'test "$tag_commit" = "$sha"' in source
    assert 'test "$IMAGE_TAG" = "$SOURCE_SHA"' in source
