import json

import pytest

from apps.api.client_inference import infer_client_id, load_domain_map


@pytest.fixture
def synthetic_domain_map():
    return load_domain_map()


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://brightleaf.example/offer", "brightleaf"),
        ("http://shop.brightleaf.example:8099/offer", "brightleaf"),
        ("checkout.brightleaf.example/offer", "brightleaf"),
        ("//calmleaf.example/offer", "calmleaf"),
        ("https://unknown.example/offer", None),
        ("https://tallyhq.example/path", None),
        (None, None),
        ("https://[invalid/path", None),
        ("http://localhost:8099/oakandhome/offer", "oakandhome"),
        ("http://localhost:8099/not-a-client/offer", None),
    ],
)
def test_infers_synthetic_client_from_url(synthetic_domain_map, url, expected):
    assert infer_client_id(url, synthetic_domain_map) == expected


def test_domain_map_ignores_local_storefront_base_as_a_host(tmp_path):
    path = tmp_path / "domain_map.json"
    path.write_text(
        json.dumps(
            {
                "local_storefront_base": "http://localhost:8099/<client>",
                "brightleaf.example": "brightleaf",
            }
        ),
        encoding="utf-8",
    )

    assert load_domain_map(path) == {"brightleaf.example": "brightleaf"}
