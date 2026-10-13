from app.main import PlanIn


def test_plan_payload_maps_serials_to_service_argument() -> None:
    payload = PlanIn(
        organization_id="org",
        source_network_id="source",
        destination_network_id="destination",
        serials=["Q1"],
        groups=["ports"],
    )
    assert payload.serials == ["Q1"]
