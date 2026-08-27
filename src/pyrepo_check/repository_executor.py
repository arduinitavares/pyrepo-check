"""Compose repository safety with locked environment preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import time

from pyrepo_check.execution import (
    RepositoryEnvironmentObservation,
    RepositoryPreparation,
    ProcessRunner,
)
from pyrepo_check.planning import RunPlan
from pyrepo_check.repository_environment import (
    inspect_repository_lock,
    prepare_repository_environment,
    probe_repository_dependencies,
    unobserved_repository_dependencies,
)
from pyrepo_check.repository_safety import (
    RepositoryStateSnapshot,
    capture_repository_baseline,
)


@dataclass(frozen=True)
class SafeRepositoryPreparation:
    baseline: RepositoryStateSnapshot | None
    preparation: RepositoryPreparation


def prepare_safe_repository(
    plan: RunPlan,
    *,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> SafeRepositoryPreparation:
    """Inspect the lock, capture safety state, then prepare without a Check."""
    lock_presence = inspect_repository_lock(plan.root)
    if lock_presence.state != "present":
        preparation = prepare_repository_environment(
            plan,
            lock_presence=lock_presence,
            runner=runner,
            clock_ns=clock_ns,
        )
        return SafeRepositoryPreparation(
            baseline=None,
            preparation=replace(
                preparation,
                observation=replace(
                    preparation.observation,
                    dependencies=unobserved_repository_dependencies(plan),
                ),
            ),
        )

    baseline = capture_repository_baseline(
        plan.root,
        runner=runner,
        clock_ns=clock_ns,
    )
    if baseline.error is not None:
        observation = RepositoryEnvironmentObservation(
            manager_version=None,
            path=None,
            python_selection=plan.repository_python,
            python=None,
            lock_path=lock_presence.path,
            lock_status="unverified",
            mutation_protection="unobserved",
            dependencies=unobserved_repository_dependencies(plan),
            processes=baseline.processes,
            error=baseline.error,
        )
        return SafeRepositoryPreparation(
            baseline=None,
            preparation=RepositoryPreparation(None, observation),
        )

    if baseline.snapshot is None:
        raise RuntimeError("successful repository baseline has no snapshot")
    preparation = prepare_repository_environment(
        plan,
        lock_presence=lock_presence,
        runner=runner,
        clock_ns=clock_ns,
    )
    if preparation.prepared is not None:
        dependencies = probe_repository_dependencies(
            plan,
            preparation.prepared,
            runner=runner,
            clock_ns=clock_ns,
        )
        preparation = replace(
            preparation,
            observation=replace(
                preparation.observation,
                dependencies=dependencies,
            ),
        )
    else:
        preparation = replace(
            preparation,
            observation=replace(
                preparation.observation,
                dependencies=unobserved_repository_dependencies(plan),
            ),
        )
    observation = preparation.observation
    combined = replace(
        preparation,
        observation=replace(
            observation,
            processes=baseline.processes + observation.processes,
        ),
    )
    return SafeRepositoryPreparation(baseline.snapshot, combined)
