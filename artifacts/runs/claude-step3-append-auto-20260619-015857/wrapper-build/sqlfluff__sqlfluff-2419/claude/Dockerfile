ARG BASE_IMAGE
ARG AGENT_TOOL_IMAGE
FROM ${AGENT_TOOL_IMAGE} AS agent_tools
FROM ${BASE_IMAGE}
COPY --from=agent_tools /usr/local /usr/local
ENV PATH=/usr/local/bin:$PATH
WORKDIR /testbed
