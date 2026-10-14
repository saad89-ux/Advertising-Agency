"""Unit tests for production-readiness enhancements:
1. Cloudinary / Unified storage adapter
2. PII detection and redaction
3. Runtime LLM-as-a-Judge evaluation node
4. Multi-agent supervisor workflow
5. MCP platform dispatcher
6. Dynamic real client inference
"""

from types import SimpleNamespace

from apps.api.client_inference import infer_client_id, allowed_origins_for_client

from apps.worker import judge_eval
from apps.worker.file_storage import UnifiedFileStore, get_file_store
from apps.worker.judge_eval import evaluate_with_llm_judge
from apps.worker.mcp_client import MCPPlatformDispatcher
from apps.worker.supervisor_agent import CampaignSupervisorAgent
from packages.contracts import AdContract, CheckType, FindingContract
from packages.guards.pii_guard import scrub_pii


def test_pii_scrubbing():
    raw_text = "Contact me at alice.smith@agency.com or call +1 (555) 234-5678. Card: 4111 2222 3333 4444"
    result = scrub_pii(raw_text)
    assert result.has_pii is True
    assert "[REDACTED_EMAIL]" in result.cleaned_text
    assert "[REDACTED_PHONE]" in result.cleaned_text
    assert "[REDACTED_PAYMENT_CARD]" in result.cleaned_text
    assert "alice.smith@agency.com" not in result.cleaned_text
    assert "555" not in result.cleaned_text


def test_unified_file_store_local_fallback(tmp_path):
    store = UnifiedFileStore(root=tmp_path)
    content = b"Sample ad creative binary evidence"
    ref = store.put(content)
    assert ref is not None
    retrieved = store.get(ref)
    assert retrieved == content


def test_runtime_llm_judge_clean_pass():
    ad = AdContract(
        ad_id="ad-test-judge-1",
        batch_id="batch-1",
        client_id="brightleaf",
        row_number=1,
        platform="instagram",
        headline="Glow Deeply",
        ad_text="Experience natural daily radiance with pure plant oils.",
        destination_url="https://brightleaf.com/glow",
        row_hash="hash1",
        sheet_hash="sheethash",
    )
    judge_res = evaluate_with_llm_judge(ad, findings=[])
    assert judge_res.verdict == "SKIPPED"
    assert judge_res.confidence_score == 1.0
    assert judge_res.needs_person is False


def test_runtime_llm_judge_with_findings(monkeypatch):
    # Keep the judge contract test deterministic and independent of PostgreSQL/model credentials.
    monkeypatch.setattr(
        judge_eval,
        "call_gemini_model",
        lambda **_kwargs: SimpleNamespace(
            text=(
                '{"judge_verdict":"CONFIRMED","confidence_score":0.95,'
                '"faithfulness_score":0.95,"hallucinations_detected":false,'
                '"false_alarms_dismissed":[],"remediation_approved":true,'
                '"judge_reasoning":"The claim is directly unsupported."}'
            ),
            cost_usd=0.0,
        ),
    )
    ad = AdContract(
        ad_id="ad-test-judge-2",
        batch_id="batch-1",
        client_id="brightleaf",
        row_number=1,
        platform="instagram",
        headline="Instant 100% Miracle",
        ad_text="Cures all fine lines in 3 minutes guaranteed.",
        destination_url="https://brightleaf.com/cure",
        row_hash="hash2",
        sheet_hash="sheethash",
    )
    finding = FindingContract(
        finding_id="ad-test-judge-2:wording:BL-C-01",
        ad_id="ad-test-judge-2",
        row_number=1,
        check_type=CheckType.WORDING,
        message="Non-compliant medical claim detected.",
        field="headline",
        rule_id="BL-C-01",
    )
    judge_res = evaluate_with_llm_judge(ad, findings=[finding])
    assert judge_res.verdict in {"CONFIRMED", "REQUIRES_HUMAN"}
    assert judge_res.confidence_score > 0.8


def test_mcp_platform_dispatcher(monkeypatch):
    monkeypatch.delenv("MCP_META_SERVER_URL", raising=False)
    monkeypatch.delenv("MCP_TIKTOK_SERVER_URL", raising=False)
    monkeypatch.delenv("MCP_X_SERVER_URL", raising=False)
    dispatcher = MCPPlatformDispatcher()
    tools = dispatcher.list_tools("instagram")
    assert len(tools) == 4
    tool_names = [t.name for t in tools]
    assert "ads_create_campaign" in tool_names
    assert "ads_create_ad_set" in tool_names
    assert "ads_create_creative" in tool_names
    assert "ads_create_ad" in tool_names
    assert "instagram_dispatch_ad" not in tool_names


def test_meta_dispatch_stays_disabled_without_live_write_configuration(monkeypatch):
    from apps.worker.meta_mcp_client import MetaMCPClient

    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "false")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    monkeypatch.setenv("MCP_META_SERVER_URL", "https://mcp.facebook.com/ads")
    monkeypatch.setenv("META_MCP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123456")
    monkeypatch.setattr(
        MetaMCPClient,
        "call_tool_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external write attempted")),
    )

    response = MCPPlatformDispatcher().call_tool(
        platform="facebook",
        tool_name="ads_create_campaign",
        arguments={"ad_id": "test-ad"},
    )

    assert response.success is False
    assert response.error == "real_platform_calls_disabled"


def test_meta_mcp_client_refuses_writes_when_live_flag_is_false(monkeypatch):
    from apps.worker.meta_mcp_client import MetaMCPClient

    client = MetaMCPClient(
        server_url="https://mcp.facebook.com/ads",
        access_token="test-token",
        allow_writes=True,
        enable_live=False,
    )
    result = client.call_tool_sync(
        "ads_create_ad", {}, tool_annotations=SimpleNamespace(readOnlyHint=True)
    )

    assert result.success is False
    assert result.is_write_refusal is True
    assert "META_MCP_ENABLE_LIVE" in result.error


def test_meta_mcp_client_requires_configured_account_before_write(monkeypatch):
    from apps.worker.meta_mcp_client import MetaMCPClient

    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "true")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "")
    client = MetaMCPClient(
        server_url="https://mcp.facebook.com/ads",
        access_token="test-token",
        allow_writes=True,
        enable_live=True,
    )
    result = client.call_tool_sync("ads_create_ad", {})

    assert result.success is False
    assert result.is_write_refusal is False
    assert "META_AD_ACCOUNT_ID" in result.error


def test_dynamic_client_inference():
    domain_map = {"brightleaf.com": "brightleaf"}
    # Known client
    assert infer_client_id("https://www.brightleaf.com/oil", domain_map) == "brightleaf"
    # Dynamic real client from arbitrary domain
    inferred = infer_client_id("https://shop.gymshark.com/collections/new", domain_map)
    assert inferred == "gymshark"
    # Allowed origins includes the dynamic client
    origins = allowed_origins_for_client(inferred, domain_map)
    assert f"https://www.{inferred}.com" in origins or f"https://{inferred}.com" in origins


def test_multi_agent_supervisor(tmp_path):
    store = get_file_store(root=tmp_path)
    supervisor = CampaignSupervisorAgent(file_store=store)
    ad = AdContract(
        ad_id="ad-agent-01",
        batch_id="batch-01",
        client_id="brightleaf",
        row_number=1,
        platform="instagram",
        headline="Organic Hydration Daily",
        ad_text="Cold-pressed rosehip seed oil deeply nourishes dry skin.",
        destination_url="http://localhost:8099/brightleaf/serum",
        row_hash="rh1",
        sheet_hash="sh1",
    )
    report = supervisor.run_campaign_audit(ad)
    assert report.supervisor_decision in {"READY_FOR_DISPATCH", "REMEDIATION_REQUIRED"}
    assert report.shared_state is not None
    assert report.verdict is not None
