"""Pins the identity-owned device-home contract without starting subprocesses.

The two checks are intentionally small: together they prevent reintroducing a
shared source area or an install API that lets callers choose arbitrary paths.
"""

import inspect

from my_team.kernel.agent_os import AgentOS


def test_device_home_is_identity_based(tmp_path):
    """Ensures device home resolution is identity-based."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runtime_root: {tmp_path}\nagents: []\n")
    agent_os = AgentOS(str(config_path))
    expected = str(tmp_path / "home" / "echo")
    assert agent_os._device_home("echo") == expected


def test_install_contract_is_identity_based():
    """Ensures installation derives ``device.py`` from the device identity home."""
    source = inspect.getsource(AgentOS._install)
    assert 'payload.get("source_file")' not in source
    assert "device.py" in source or "identity" in source
