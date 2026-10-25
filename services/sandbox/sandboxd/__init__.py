"""sandboxd: the agent that runs inside every project sandbox.

The control plane never touches a workspace directly. Each project gets its own
container (or pod) with one AgentSociety workspace, and this agent exposes that
workspace over an authenticated HTTP API: files, experiment runs, the upstream
agentsociety2 backend, the AI Social Scientist chat runtime and the browser IDE.
"""

__version__ = "0.1.0"
