# CMN-C2-234 - Unit tests: declared runtime config ARRIVES where it is consumed.
#
# A green suite proves nothing about configuration unless a declared value is
# traced to its consumer: a config loader that silently returns {} degrades
# every declared setting to its default without failing a single test. These
# tests read the REAL config/config.yaml and prove each declared value reaches
# the component that consumes it - the graph constructor path for the runtime
# scalars, and the inner workflow (through _parent_config() and the state
# injection) for the Teams integration section.

import pathlib

import yaml

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.domain_workflow_graph import TeamsWorkflowGraph
from src.graph.graph import TeamsWorkflowGraphNode
from src.schemas.state import from_json
from src.services.teams_client import TeamsClient

_RUNTIME_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _declared():
    return yaml.safe_load(_RUNTIME_PATH.read_text(encoding="utf-8"))


def test_parent_config_reads_the_live_runtime_file():
    """_parent_config() must forward the CURRENT config/config.yaml teams
    section - not a stale location (the legacy nested manifest) that would
    make it silently return {}."""
    declared = _declared()
    forwarded = TeamsWorkflowGraphNode()._parent_config()
    assert forwarded["configurable"]["teams"] == declared["teams"]
    assert forwarded["configurable"]["teams"]["base_url"]  # non-empty, from the file


def test_inner_graph_state_carries_the_declared_base_url():
    """File -> _parent_config() -> _extra_initial_state() -> State JSON."""
    declared = _declared()
    graph = TeamsWorkflowGraph(config=TeamsWorkflowGraphNode()._parent_config())
    extra = graph._extra_initial_state()
    assert from_json(extra["teams_config"], {}) == declared["teams"]


def test_declared_base_url_reaches_the_teams_client_end_to_end(monkeypatch):
    """Full outer invoke: the client the API node constructs must be built
    with the base_url DECLARED in config/config.yaml - proving the value
    survives every hop (file -> parent config -> inner state -> node)."""
    import src.nodes.call_teams_api_node as node_mod
    from src.graph.graph import TeamsMessageAgent

    captured: list[str] = []

    class RecordingClient(TeamsClient):
        def __init__(self, base_url=None, **kwargs):
            captured.append(base_url)
            if base_url is None:
                super().__init__(**kwargs)
            else:
                super().__init__(base_url, **kwargs)

    monkeypatch.setattr(node_mod, "TeamsClient", RecordingClient)

    agent = TeamsMessageAgent()
    agent.compile()
    result = agent.invoke(
        user_input=(
            "Post a channel message in team id a1b2c3d4-e5f6-4711-8abc-9def01234567 "
            'channel id 19:abcdef1234567890@thread.tacv2 saying "config arrival probe"'
        ),
        ctx=InvocationContext(caller_id="cfg-probe", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )
    assert result["status"] == "success", f"result={result!r}"
    assert captured == [_declared()["teams"]["base_url"]]


def test_max_retry_reaches_the_graph_when_config_is_passed():
    """The framework run loop reads max_retry from the config handed to the
    graph constructor - the standalone server loads config/config.yaml and
    passes it (asserted against the server module in the endpoint tests)."""
    from src.api.server import _load_runtime_config
    from src.graph.graph import TeamsMessageAgent

    declared = _declared()
    agent = TeamsMessageAgent(config=_load_runtime_config())
    assert agent.config.get("max_retry") == declared["max_retry"]
    assert agent.config.get("timeout_s") == declared["timeout_s"]


def test_missing_runtime_file_degrades_to_safe_defaults(monkeypatch, tmp_path):
    """A missing config file must not crash graph construction - the inner
    nodes fall back to the documented stub-client defaults."""
    import src.graph.graph as graph_mod

    monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
    forwarded = TeamsWorkflowGraphNode()._parent_config()
    assert forwarded == {"configurable": {}}
