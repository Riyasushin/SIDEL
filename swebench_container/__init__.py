"""SWE-bench task container execution boundary."""

from swebench_container.agents import AgentAdapter, ClaudeAdapter, CodexAdapter, adapter_for
from swebench_container.images import ImageBuildResult, WrapperImageBuilder, WrapperImageSpec
from swebench_container.models import AgentMode, AgentName, ContainerRunResult, ContainerRunSpec
from swebench_container.runner import DockerTaskRunner

__all__ = [
    "AgentAdapter",
    "AgentMode",
    "AgentName",
    "ClaudeAdapter",
    "CodexAdapter",
    "ContainerRunResult",
    "ContainerRunSpec",
    "DockerTaskRunner",
    "ImageBuildResult",
    "WrapperImageBuilder",
    "WrapperImageSpec",
    "adapter_for",
]
