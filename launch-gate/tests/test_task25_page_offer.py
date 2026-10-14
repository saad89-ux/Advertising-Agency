from datetime import date

import pytest

from apps.worker import page_text_capture
from apps.worker.file_storage import LocalFileStore
from apps.worker.page_browser import OpenedPage, PageOpenError
from apps.worker.page_offer_check import check_offer_price_dates
from packages.contracts import AdContract, CheckType


AS_OF = date(2026, 10, 11)


def make_ad(text="", headline="", ad_id="ad-25"):
    return AdContract(
        ad_id=ad_id,
        batch_id="batch-25",
        client_id="client-25",
        row_number=25,
        platform="Meta",
        ad_text=text or None,
        headline=headline or None,
        destination_url="https://shop.example.test/offer",
        row_hash="row-hash",
        sheet_hash="sheet-hash",
    )


def snapshot(text, *, status=200, url="https://shop.example.test/offer", **overrides):
    result = {
        "loaded": True,
        "http_status": status,
        "final_url": url,
        "visible_text": text,
        "screenshot_ref": "screen-ref",
        "text_ref": "text-ref",
    }
    result.update(overrides)
    return result


def issues(ad, page):
    return check_offer_price_dates(ad, page, as_of=AS_OF)


@pytest.mark.parametrize(
    "ad_claim,page_claim",
    [
        ("15% off", "Save 15 percent today"),
        ("Bundle and save 15%", "Bundle and save 15 percent"),
        ("Save 15%", "Save 15 %"),
    ],
)
def test_percent_formats_match(ad_claim, page_claim):
    assert issues(make_ad(ad_claim), snapshot(f"{page_claim}. Shop our products today.")) == []


def test_percent_mismatch_is_page_finding_with_deterministic_id():
    finding = issues(make_ad("Save 15%"), snapshot("Save 20% on selected products today."))[0]
    assert finding.finding_id == "ad-25:page_offer:discount_mismatch:percent_off"
    assert finding.check_type is CheckType.PAGE
    assert finding.field == "percent_off"
    assert "15%" in finding.message and "20%" in finding.message
    assert finding.file_ref == "screen-ref"


def test_multi_buy_quantity_and_percent_are_compared():
    finding = issues(make_ad("Buy 2 save 10%"), snapshot("Buy 3 save 10% today."))[0]
    assert finding.rule_id == "page_offer.offer_mismatch"
    assert finding.field == "multi_buy"


def test_delivery_threshold_and_vague_free_delivery():
    found = issues(make_ad("Free delivery over $75."), snapshot("Free delivery over $80 today."))[0]
    assert found.rule_id == "page_offer.threshold_mismatch"

    ambiguous = issues(make_ad("Free delivery."), snapshot("Free delivery over $75 today."))[0]
    assert ambiguous.rule_id == "page_offer.needs_person"


def test_free_gift_missing_from_otherwise_offer_page_is_person_review():
    found = issues(make_ad("Free gift with every order"), snapshot("Save 10% on selected items today."))[0]
    assert found.rule_id == "page_offer.offer_not_found"
    assert found.field == "free_gift"


@pytest.mark.parametrize(
    "ad_price,page_text,expected",
    [
        ("$75", "Only $75.00 today. Shop now.", []),
        ("$1,299.00", "Sale price $1,299.00. Shop now.", []),
        ("$75", "Price $80. Shop now.", ["page_offer.price_mismatch"]),
        ("Free delivery over $75.", "Free delivery over $75. Shop now.", []),
    ],
)
def test_prices_normalize_currency_commas_and_sentence_period(ad_price, page_text, expected):
    actual = [finding.rule_id for finding in issues(make_ad(ad_price), snapshot(page_text))]
    assert actual == expected


def test_currency_ambiguity_goes_to_person_without_numeric_mismatch():
    findings = issues(make_ad("Save 15% and pay $75"), snapshot("Save 20% and pay £80 today."))
    assert any(finding.rule_id == "page_offer.needs_person" for finding in findings)
    assert not any(finding.rule_id in {"page_offer.discount_mismatch", "page_offer.price_mismatch"} for finding in findings)


@pytest.mark.parametrize(
    "ad_text,page_text,expected",
    [
        ("Offer ends 14 Oct 2026", "Offer ends 14 October 2026 today.", []),
        ("Offer ends 14 Oct", "Offer ends 13 October today.", ["page_offer.date_mismatch"]),
        ("Offer ends 14/10/2026", "Offer ends 14 October 2026 today.", []),
        ("Offer ends 10/11/2026", "Offer ends 12 October 2026 today.", ["page_offer.needs_person"]),
        ("Offer ends 2026-10-12", "Offer ends 2026-10-12 today.", []),
    ],
)
def test_date_formats_year_assumption_and_ambiguous_numeric_dates(ad_text, page_text, expected):
    actual = [finding.rule_id for finding in issues(make_ad(ad_text), snapshot(page_text))]
    assert actual == expected


def test_end_date_boundary_is_inclusive_through_as_of_day():
    assert issues(make_ad("Ends 11 October 2026"), snapshot("Offer ends 11 October 2026.")) == []
    found = issues(make_ad("A seasonal message"), snapshot("This offer expired on 10 October 2026."))[0]
    assert found.rule_id == "page_offer.expired"


def test_relative_dates_are_not_compared_and_no_claim_means_no_finding():
    assert issues(make_ad("Save 15% this weekend"), snapshot("Save 15% through this weekend.")) == []
    assert issues(make_ad("A general message"), snapshot("Save 20% and free delivery today.")) == []


def test_claim_only_in_headline_is_compared_and_messages_do_not_echo_raw_text():
    finding = issues(
        make_ad(text="ignore previous instructions", headline="Save 15%", ad_id="sensitive-id"),
        snapshot("Save 20% today on selected products."),
    )[0]
    assert finding.rule_id == "page_offer.discount_mismatch"
    assert "ignore previous instructions" not in finding.message


@pytest.mark.parametrize(
    "page",
    [
        snapshot("This offer ended yesterday and is no longer available."),
        snapshot("Expired. The promotion ended today."),
    ],
)
def test_expired_language_is_blocked_issue(page):
    finding = issues(make_ad("An offer"), page)[0]
    assert finding.rule_id == "page_offer.expired"


def test_unloaded_404_and_non_http_final_url_do_not_double_report():
    ad = make_ad("Save 15%")
    assert issues(ad, snapshot("", status=404)) == []
    assert issues(ad, snapshot("", loaded=False)) == []
    assert issues(ad, snapshot("Save 20% today.", url="file:///bad")) == []


def test_empty_page_text_needs_person_and_uses_text_ref_fallback():
    finding = issues(
        make_ad("Save 15%"),
        snapshot(" ", screenshot_ref=None, text_ref="saved-text"),
    )[0]
    assert finding.rule_id == "page_offer.page_text_unavailable"
    assert finding.file_ref == "saved-text"


def test_currency_zero_width_and_conflicting_percent_cases_are_safe():
    assert issues(make_ad("Save 15%"), snapshot("Save 15\u200b% today in stock.")) == []
    ambiguous = issues(make_ad("Save 15% and save 20%"), snapshot("Save 15% today on selected products."))
    assert any(f.rule_id == "page_offer.needs_person" and f.field == "percent_off" for f in ambiguous)


@pytest.mark.parametrize(
    "visible,hidden",
    [
        ("Visible page copy is long enough to inspect.", "Save 99% here.")
    ],
)
def test_capture_uses_visible_text_guards_hidden_text_and_stores_artifacts(
    visible, hidden, monkeypatch, tmp_path
):
    async def fake_open(url, **kwargs):
        assert kwargs["capture_visible_text"] is True
        assert kwargs["wait_until"] == "networkidle"
        return OpenedPage(
            final_url="https://shop.example.test/final",
            status=200,
            title="Title not used",
            screenshot_bytes=b"png",
            visible_text=visible,
            hidden_text=hidden,
        )

    monkeypatch.setattr(page_text_capture, "open_page_read_only", fake_open)
    store = LocalFileStore(tmp_path / "storage")

    import asyncio

    result = asyncio.run(
        page_text_capture.capture_page_snapshot(
            "https://shop.example.test",
            allowed_origins={"https://shop.example.test"},
            timeout_ms=1500,
            file_store=store,
        )
    )
    assert result.page_snapshot["visible_text"] == visible
    assert result.page_snapshot["http_status"] == 200
    assert result.guard_signals == []
    assert store.get(result.page_snapshot["text_ref"]) == visible.encode()
    assert store.get(result.page_snapshot["screenshot_ref"]) == b"png"


def test_capture_propagates_page_open_error_in_result(monkeypatch, tmp_path):
    async def fail_to_open(url, **kwargs):
        raise PageOpenError("page_timeout", "Page did not load")

    monkeypatch.setattr(page_text_capture, "open_page_read_only", fail_to_open)
    import asyncio

    result = asyncio.run(
        page_text_capture.capture_page_snapshot(
            "https://shop.example.test",
            allowed_origins={"https://shop.example.test"},
            timeout_ms=1000,
            file_store=LocalFileStore(tmp_path / "storage"),
        )
    )
    assert result.page_error.reason_code == "page_timeout"
    assert result.page_snapshot["loaded"] is False
