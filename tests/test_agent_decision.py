from my_team.agent.agent import Agent
from my_team.kernel.event_protocol import VOID


def _agent():
    # emit is unused by the deterministic decision path.
    import tempfile
    tmpdir = tempfile.mkdtemp()
    return Agent(lambda _event: None, runtime_root=tmpdir,
                 identity="test_agent", position="worker")


def test_agent_injection_match_and_tool_result_path():
    """Justifies the minimum agent loop: injected capability, dispatch, and result return."""
    agent = _agent()
    assert agent._on_inject(
        {
            "entries": [
                {
                    "content": {"name": "echo"},
                    "trigger": ["echo"],
                    "associated": ["echo-device"],
                }
            ]
        }
    ) == VOID
    outbound = agent._on_task(
        {
            "source": "caller",
            "payload": {"command": "task", "content": "echo 北京"},
        }
    )
    assert outbound["target"] == "echo-device"
    assert outbound["payload"]["command"] == "tool_run"
    assert outbound["payload"]["arguments"] == {"city": "北京"}
    result = agent._on_tool_result(
        {
            "payload": {"task": "caller", "ok": True, "content": "hello"}
        }
    )
    assert result["payload"] == {
        "command": "agent_result",
        "ok": True,
        "content": "hello",
        "error": None,
    }


def test_agent_unmatched_task_is_explicit_failure():
    """Unknown work must produce an observable failure instead of silent loss."""
    agent = _agent()
    result = agent._on_task(
        {"source": "caller", "payload": {"command": "task", "content": "unknown"}}
    )
    assert result["payload"]["ok"] is False
    assert "没有匹配" in result["payload"]["error"]
