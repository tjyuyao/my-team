import asyncio

from my_team.kernel.authority import Authority
from my_team.kernel.event_protocol import VOID


def _cmd(command, **payload):
    return {
        "source": "system",
        "target": "authority",
        "kind": "system",
        "payload": {"command": command, **payload},
    }


def test_authority_registration_grants_and_injection():
    """Covers the complete grant-to-memory contract in one deterministic path."""
    asyncio.run(_authority_registration_grants_and_injection())


async def _authority_registration_grants_and_injection():
    import tempfile
    tmpdir = tempfile.mkdtemp()
    authority = Authority("authority", tmpdir)
    assert await authority.respond(
        _cmd("register_request", identity="alice", agent=True, position="worker")
    ) == VOID
    assert await authority.respond(
        _cmd(
            "register_request",
            identity="echo",
            tools=[{"name": "echo", "trigger": ["echo"]}],
            scopes=[{"token": "public", "default": True, "explanation": "basic"}],
        )
    ) == VOID
    await authority.respond(_cmd("grant_request", position="worker", device="echo", token="public"))
    injected = await authority.respond(_cmd("inject_request", agent="alice"))
    names = {entry["content"]["name"] for entry in injected["payload"]["entries"]}
    assert names == {"echo", "echo:public"}

    auth = await authority.respond(_cmd("auth_request", identity="alice"))
    assert auth["payload"]["auth"]["position"] == "worker"
    assert auth["payload"]["auth"]["scopes"] == [{"device": "echo", "token": "public"}]


def test_authority_command_surface_is_kernel_only():
    """Protects the trust boundary: callers cannot invoke Authority commands directly."""
    asyncio.run(_authority_command_surface_is_kernel_only())


async def _authority_command_surface_is_kernel_only():
    import tempfile
    tmpdir = tempfile.mkdtemp()
    authority = Authority("authority", tmpdir)
    denied = await authority.respond(
        {
            "source": "alice",
            "target": "authority",
            "kind": "system",
            "payload": {"command": "agents_request"},
        }
    )
    assert denied["payload"]["command"] == "denied"
