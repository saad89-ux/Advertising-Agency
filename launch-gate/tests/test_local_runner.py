from types import SimpleNamespace
from unittest.mock import Mock

import run_local


def test_local_runner_binds_services_to_loopback(monkeypatch):
    monkeypatch.setattr(run_local, "is_port_in_use", lambda _port: False)

    storefront_addresses = []

    class FakeServer:
        def __init__(self, address, _handler):
            storefront_addresses.append(address)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def serve_forever(self):
            return None

    monkeypatch.setattr(run_local.socketserver, "TCPServer", FakeServer)
    run_local.run_storefront_server()
    assert storefront_addresses == [(run_local.LOCAL_HOST, 8099)]

    uvicorn_run = Mock()
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", SimpleNamespace(run=uvicorn_run))
    run_local.run_stand_in_platform()
    run_local.run_backend_api()
    assert [call.kwargs["host"] for call in uvicorn_run.call_args_list] == [
        run_local.LOCAL_HOST,
        run_local.LOCAL_HOST,
    ]

    popen = Mock(return_value=object())
    monkeypatch.setattr(run_local.subprocess, "Popen", popen)
    run_local.run_frontend()
    frontend_command = popen.call_args.args[0]
    assert frontend_command[-2:] == ["--hostname", run_local.LOCAL_HOST]


def test_local_runner_stops_when_jwt_secret_is_invalid(monkeypatch):
    import apps.api.auth

    monkeypatch.setattr(run_local, "init_environment", lambda: None)
    monkeypatch.setattr(
        apps.api.auth,
        "get_jwt_secret",
        Mock(side_effect=RuntimeError("JWT_SECRET must be set")),
    )
    monkeypatch.setattr(
        run_local,
        "is_port_in_use",
        Mock(side_effect=AssertionError("services must not start")),
    )

    run_local.main()