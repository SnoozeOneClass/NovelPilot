import json
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.core import config
from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import AgentRole
from app.harness.agents.policy import resolve_agent_policy
from app.schemas.experiments import (
    ExperimentModelBinding,
    ExperimentRunConfiguration,
    ExperimentRunConfigurationRequest,
    ExperimentRunConfigurationResponse,
    ExperimentSchemaSnapshot,
)
from app.storage.experiment_fixtures import verify_fixture
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import write_json
from app.storage.profiles import profile_fingerprint
from app.storage.projects import read_project_metadata
from app.schemas.projects import ProjectMetadata


def create_run_configuration(
    project_path: Path,
    request: ExperimentRunConfigurationRequest,
) -> ExperimentRunConfigurationResponse:
    fixture_path = config.OUTPUT_DIR / "experiments" / "fixtures" / request.fixture_id
    manifest = verify_fixture(fixture_path)
    metadata = read_project_metadata(project_path)
    bindings = _model_bindings(metadata)
    schemas = ExperimentSchemaSnapshot(
        tool_registry=build_default_tool_registry().version_map()
    )
    fingerprint_payload = {
        "fixture_id": request.fixture_id,
        "checkpoint_fingerprint": manifest.checkpoint.checkpoint_fingerprint,
        "agent_policy": metadata.agent_policy.model_dump(mode="json"),
        "model_bindings": [item.model_dump(mode="json") for item in bindings],
        "schemas": schemas.model_dump(mode="json"),
        "arms": [item.model_dump(mode="json") for item in request.arms],
    }
    configuration = ExperimentRunConfiguration(
        run_id=f"experiment-run-{uuid4()}",
        created_at=datetime.now(UTC),
        fixture_id=request.fixture_id,
        checkpoint_fingerprint=manifest.checkpoint.checkpoint_fingerprint,
        agent_policy=metadata.agent_policy,
        model_bindings=bindings,
        schemas=schemas,
        arms=request.arms,
        configuration_fingerprint=_fingerprint(fingerprint_payload),
    )
    experiment_root = config.OUTPUT_DIR / "experiments"
    root = experiment_root / "runs" / configuration.run_id
    staging = experiment_root / ".creating" / configuration.run_id
    with exclusive_file_lock(config.OUTPUT_DIR / "experiments" / ".runs.lock"):
        if root.exists():
            raise FileExistsError(f"Experiment run already exists: {configuration.run_id}")
        try:
            staging.mkdir(parents=True)
            write_json(staging / "config.json", configuration.model_dump(mode="json"))
            root.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return ExperimentRunConfigurationResponse(configuration=configuration)


def _model_bindings(metadata: ProjectMetadata) -> list[ExperimentModelBinding]:
    roles: tuple[AgentRole, ...] = ("book", "story_arc", "chapter")
    resolved = {
        role: resolve_agent_policy(metadata, role)
        for role in roles
    }
    bindings: list[ExperimentModelBinding] = []
    for role, policy in resolved.items():
        bindings.extend(
            [
                ExperimentModelBinding(
                    role=role,
                    purpose="agent",
                    profile_id=policy.profile.id,
                    protocol=policy.profile.protocol,
                    model=policy.profile.model,
                    profile_fingerprint=profile_fingerprint(policy.profile),
                ),
                ExperimentModelBinding(
                    role=role,
                    purpose="evaluator",
                    profile_id=policy.evaluator_profile.id,
                    protocol=policy.evaluator_profile.protocol,
                    model=policy.evaluator_profile.model,
                    profile_fingerprint=profile_fingerprint(
                        policy.evaluator_profile
                    ),
                ),
            ]
        )
    return bindings


def _fingerprint(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
