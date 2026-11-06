# Where Is the Cost of Third-Party API Routers in Agentic Software Development?

This paper introduces SIDEL to study response manipulation by third-party API routers across four coding agents and evaluate the effectiveness of client-side defenses.

## Project Structure

```text
SIDEL/
├── manager/             # Experiment planning, execution, and analysis
├── router/              # Request routing, replay, injection, and defense
├── swebench_container/  # Coding-agent adapters and isolated task execution
├── data/                # Intervention datasets
├── bench/               # SWE-bench tasks and prompt templates
├── scripts/             # Data, diagnostic, and analysis utilities
├── tests/               # Unit and integration tests
├── docs/                # Technical documentation
├── artifacts/           # Local experiment outputs (ignored by Git)
├── code_agents.py       # Agent definitions and supported modes
├── Dockerfile           # Agent tooling image
├── docker-compose.yml   # Router service configuration
├── requirements.txt     # Python dependencies
└── README.md            # Paper overview and project structure
```
