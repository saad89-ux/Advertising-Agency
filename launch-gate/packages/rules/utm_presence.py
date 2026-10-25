"""Pure mechanical check for required UTM query parameters."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from packages.contracts import AdContract, CheckType, FindingContract


# Required parameter names match data/naming_convention.json (SYNTHETIC config).
REQUIRED_UTM_PARAMS = ("utm_source", "utm_medium", "utm_campaign")


def _query_parameters(destination_url: str) -> dict[str, list[str]]:
    candidate = destination_url.strip()
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = "//" + candidate
    try:
        query = urlsplit(candidate).query
    except ValueError:
        return {}

    parameters: dict[str, list[str]] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        parameters.setdefault(key, []).append(value)
    return parameters


def check_utm_presence(
    ad: AdContract,
    *,
    required_params: tuple[str, ...] = REQUIRED_UTM_PARAMS,
) -> list[FindingContract]:
    """Return one mechanical finding for each absent or blank required UTM value."""
    query_parameters = _query_parameters(ad.destination_url)
    findings: list[FindingContract] = []
    for parameter in required_params:
        values = query_parameters.get(parameter, [])
        if any(value.strip() for value in values):
            continue
        findings.append(
            FindingContract(
                finding_id=f"{ad.ad_id}:utm_presence:{parameter}",
                ad_id=ad.ad_id,
                row_number=ad.row_number,
                check_type=CheckType.MECHANICAL,
                message=f"Required UTM parameter '{parameter}' is missing or empty.",
                field=parameter,
                proposed_fix=f"Add a non-empty {parameter} query parameter.",
                actual_value="" if values else "<missing>",
                expected_format="a non-empty value",
            )
        )
    return findings
