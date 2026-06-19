"""Experiment planning, orchestration, and analysis boundary."""

from manager.analysis import ContextResult, analyze_run, summarize_results
from manager.dataset_profile import profile_dataset
from manager.executor import ExperimentExecutor
from manager.manifest import ExperimentManifest, RunManifest, context_registry_payload, write_manifests
from manager.orchestrator import ExperimentOrchestrator
from manager.planner import MatrixPlanOptions, build_matrix_plan
from manager.router_process import RouterProcessSpec, SharedRouterLauncher

__all__ = [
    "ContextResult",
    "ExperimentExecutor",
    "ExperimentManifest",
    "ExperimentOrchestrator",
    "MatrixPlanOptions",
    "RouterProcessSpec",
    "RunManifest",
    "SharedRouterLauncher",
    "analyze_run",
    "build_matrix_plan",
    "context_registry_payload",
    "profile_dataset",
    "summarize_results",
    "write_manifests",
]
