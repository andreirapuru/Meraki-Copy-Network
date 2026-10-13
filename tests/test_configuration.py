from app.configuration import PORT_WRITABLE, writable


def test_writable_removes_read_only_port_fields() -> None:
    port = {"portId": "1", "name": "Uplink", "enabled": True, "status": "Connected", "speed": 1000}
    assert writable(port, PORT_WRITABLE) == {"name": "Uplink", "enabled": True}
