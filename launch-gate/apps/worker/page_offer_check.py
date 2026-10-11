"""Pure comparison of typed ad claims with rendered, guarded page text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import TypedDict
from urllib.parse import urlsplit

from packages.contracts import AdContract, CheckType, FindingContract, VerdictStatus
from packages.guards.input_guard import sanitize_text


class PageSnapshot(TypedDict):
    loaded: bool
    http_status: int | None
    final_url: str
    visible_text: str
    screenshot_ref: str | None
    text_ref: str | None


@dataclass(frozen=True)
class PageFindingExpectation:
    severity: str
    verdict: VerdictStatus


# Mirrors the SYNTHETIC dev-label classes for the three scored page issue types.
SYNTHETIC_LABEL_CLASSIFICATION = {
    "page_expired_offer": PageFindingExpectation("blocked", VerdictStatus.BLOCKED),
    "page_discount_mismatch": PageFindingExpectation("fix_needed", VerdictStatus.FIX_NEEDED),
    "page_delivery_threshold_mismatch": PageFindingExpectation(
        "fix_needed", VerdictStatus.FIX_NEEDED
    ),
}


_NUMBER = r"\d+(?:\.\d+)?"
_MONEY_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
_MONEY = re.compile(
    rf"(?:(?:\$\s*(?P<dollar>{_MONEY_NUMBER}))|(?:\bUSD\s*(?P<prefix>{_MONEY_NUMBER}))|"
    rf"(?:(?P<suffix>{_MONEY_NUMBER})\s*\bUSD))",
    re.IGNORECASE,
)
_NON_USD_CURRENCY = re.compile(r"[£€]|\b(?:gbp|eur)\b", re.IGNORECASE)
_MULTI_BUY = re.compile(
    rf"\bbuy\s+(?P<quantity>\d+)\s*(?:,\s*)?save\s+(?P<percent>{_NUMBER})\s*(?:%|percent)(?![a-z])"
)
_PERCENT_OFF = re.compile(rf"\b(?P<percent>{_NUMBER})\s*(?:%|percent)\s+off\b")
_SAVE_PERCENT = re.compile(
    rf"\b(?:(?:bundle\s+and\s+)?save)\s+(?P<percent>{_NUMBER})\s*(?:%|percent)(?![a-z])"
)
_DELIVERY_THRESHOLD = re.compile(
    rf"\bfree\s+(?:delivery|shipping)(?:\s+offer)?\s+"
    rf"(?:(?:over|above)\s+|on\s+orders?\s+over\s+){_MONEY.pattern}",
    re.IGNORECASE,
)
_FREE_DELIVERY = re.compile(r"\bfree\s+(?:delivery|shipping)\b", re.IGNORECASE)
_FREE_GIFT = re.compile(r"\bfree\s+gift\s+with\s+every\s+order\b", re.IGNORECASE)
_DATE_TOKEN = (
    r"(?:"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})|"
    r"(?P<dmy_day>\d{1,2})\s+(?P<dmy_month>[a-z]+)(?:\s+(?P<dmy_year>\d{4}))?|"
    r"(?P<mdy_month>[a-z]+)\s+(?P<mdy_day>\d{1,2})(?:,?\s+(?P<mdy_year>\d{4}))?|"
    r"(?P<num_first>\d{1,2})/(?P<num_second>\d{1,2})/(?P<num_year>\d{4})"
    r")"
)
_END_DATE = re.compile(
    rf"\b(?:ends?|until|valid\s+to)\s+(?P<date>{_DATE_TOKEN})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_EXPIRED_TEXT = re.compile(
    r"\b(?:sale|offer)\s+has\s+ended\b|\bexpired\b|\bno\s+longer\s+available\b"
)
@dataclass(frozen=True)
class _DateValue:
    value: date
    assumed_year: bool = False


@dataclass
class _Claims:
    percents: list[Decimal] = field(default_factory=list)
    multi_buy: list[tuple[int, Decimal]] = field(default_factory=list)
    thresholds: list[Decimal] = field(default_factory=list)
    free_delivery: bool = False
    free_gift: bool = False
    prices: list[Decimal] = field(default_factory=list)
    end_dates: list[_DateValue] = field(default_factory=list)
    ambiguous: set[str] = field(default_factory=set)
    has_other_currency: bool = False
    has_offer_text: bool = False
    expired_phrase: bool = False


def _normalize(text: str, source: str) -> str:
    guarded = sanitize_text(text, source).text
    normalized = unicodedata.normalize("NFKC", guarded)
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    normalized = normalized.replace("\u00a0", " ").casefold()
    return " ".join(normalized.split())


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(match: re.Match[str], as_of: date) -> _DateValue | None:
    groups = match.groupdict()
    try:
        if groups.get("iso_year"):
            return _DateValue(date(int(groups["iso_year"]), int(groups["iso_month"]), int(groups["iso_day"])))

        if groups.get("dmy_day"):
            month_name = groups["dmy_month"]
            month = _MONTHS.get(month_name)
            if month is None:
                month = _MONTHS.get(month_name[:3])
            if month is None:
                return None
            year = int(groups["dmy_year"]) if groups.get("dmy_year") else as_of.year
            return _DateValue(date(year, month, int(groups["dmy_day"])), not groups.get("dmy_year"))

        if groups.get("mdy_month"):
            month_name = groups["mdy_month"]
            month = _MONTHS.get(month_name)
            if month is None:
                month = _MONTHS.get(month_name[:3])
            if month is None:
                return None
            year = int(groups["mdy_year"]) if groups.get("mdy_year") else as_of.year
            return _DateValue(date(year, month, int(groups["mdy_day"])), not groups.get("mdy_year"))

        first, second, year = int(groups["num_first"]), int(groups["num_second"]), int(groups["num_year"])
        if first <= 12 and second <= 12:
            return None
        if first > 12 and second <= 12:
            return _DateValue(date(year, second, first))
        if second > 12 and first <= 12:
            return _DateValue(date(year, first, second))
        return None
    except (ValueError, TypeError):
        return None


def _extract_claims(text: str, as_of: date) -> _Claims:
    claims = _Claims(
        has_other_currency=bool(_NON_USD_CURRENCY.search(text)),
        expired_phrase=bool(_EXPIRED_TEXT.search(text)),
    )

    multi_buy_matches = list(_MULTI_BUY.finditer(text))
    masked = list(text)
    for match in multi_buy_matches:
        try:
            quantity = int(match.group("quantity"))
            percent = Decimal(match.group("percent"))
        except (InvalidOperation, ValueError):
            claims.ambiguous.add("offer")
            continue
        if quantity <= 0 or percent < 0 or percent > 100:
            claims.ambiguous.add("offer")
            continue
        claims.multi_buy.append((quantity, percent))
        for index in range(match.start(), match.end()):
            masked[index] = " "
    masked_text = "".join(masked)

    percent_matches = [*_PERCENT_OFF.finditer(masked_text), *_SAVE_PERCENT.finditer(masked_text)]
    for match in percent_matches:
        try:
            percent = Decimal(match.group("percent"))
        except InvalidOperation:
            claims.ambiguous.add("offer")
            continue
        if percent < 0 or percent > 100:
            claims.ambiguous.add("offer")
        else:
            claims.percents.append(percent)

    threshold_matches = list(_DELIVERY_THRESHOLD.finditer(text))
    threshold_spans = [(match.start(), match.end()) for match in threshold_matches]
    for match in threshold_matches:
        amount_text = next(
            (match.group(name) for name in ("dollar", "prefix", "suffix") if match.group(name)),
            None,
        )
        amount = _decimal(amount_text) if amount_text else None
        if amount is None:
            claims.ambiguous.add("free_delivery_threshold")
        else:
            claims.thresholds.append(amount)
    delivery_matches = list(_FREE_DELIVERY.finditer(text))
    claims.free_delivery = any(
        not any(start <= match.start() < end for start, end in threshold_spans)
        for match in delivery_matches
    )
    claims.has_offer_text = bool(
        claims.percents or claims.multi_buy or claims.thresholds or claims.free_delivery
        or _FREE_GIFT.search(text)
    )
    claims.free_gift = bool(_FREE_GIFT.search(text))

    for match in _MONEY.finditer(text):
        amount_text = next(
            (match.group(name) for name in ("dollar", "prefix", "suffix") if match.group(name)),
            None,
        )
        amount = _decimal(amount_text) if amount_text else None
        if amount is None:
            claims.ambiguous.add("price")
            continue
        if any(start <= match.start() < end for start, end in threshold_spans):
            continue
        claims.prices.append(amount)

    for match in _END_DATE.finditer(text):
        date_value = _parse_date(match, as_of)
        if date_value is None:
            claims.ambiguous.add("end_date")
        else:
            claims.end_dates.append(date_value)

    return claims


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _finding(
    ad: AdContract,
    issue: str,
    field_name: str,
    message: str,
    *,
    file_ref: str | None,
    proposed_fix: str,
) -> FindingContract:
    return FindingContract(
        finding_id=f"{ad.ad_id}:page_offer:{issue}:{field_name}",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type=CheckType.PAGE,
        message=message,
        field=field_name,
        proposed_fix=proposed_fix,
        rule_id=f"page_offer.{issue}",
        file_ref=file_ref,
    )


def _ambiguous_finding(ad: AdContract, field_name: str, file_ref: str | None) -> FindingContract:
    return _finding(
        ad,
        "needs_person",
        field_name,
        f"The {field_name.replace('_', ' ')} claim is ambiguous and needs a person to review.",
        file_ref=file_ref,
        proposed_fix="Review the offer details on the ad and page.",
    )


def check_offer_price_dates(
    ad: AdContract,
    page_snapshot: PageSnapshot,
    *,
    as_of: date,
) -> list[FindingContract]:
    """Compare explicit offer, price, and end-date claims without modifying the ad."""
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise TypeError("as_of must be a date")
    if not page_snapshot["loaded"] or page_snapshot["http_status"] == 404:
        return []
    try:
        final_url = urlsplit(page_snapshot["final_url"])
        if final_url.scheme.casefold() not in {"http", "https"} or not final_url.hostname:
            return []
    except (TypeError, ValueError):
        return []

    page_text = _normalize(page_snapshot["visible_text"], "page_text")
    file_ref = page_snapshot["screenshot_ref"] or page_snapshot["text_ref"]
    if len(page_text.strip()) < 20:
        return [
            _finding(
                ad,
                "page_text_unavailable",
                "page_text",
                "Visible page text is unavailable; a person must review the page.",
                file_ref=file_ref,
                proposed_fix="Open the page and verify the offer details manually.",
            )
        ]

    ad_text = " ".join(
        _normalize(value, field_name)
        for field_name, value in (("ad_text", ad.ad_text), ("headline", ad.headline))
        if value is not None
    )
    ad_claims = _extract_claims(ad_text, as_of)
    page_claims = _extract_claims(page_text, as_of)
    findings: list[FindingContract] = []

    if page_claims.expired_phrase or any(item.value < as_of for item in page_claims.end_dates):
        findings.append(
            _finding(
                ad,
                "expired",
                "offer",
                "The visible page states that the offer has ended or its end date has passed.",
                file_ref=file_ref,
                proposed_fix="Do not launch while the page offer is expired.",
            )
        )

    ambiguous_fields = set(ad_claims.ambiguous) | set(page_claims.ambiguous)
    if ad_claims.has_other_currency or page_claims.has_other_currency:
        ambiguous_fields.add("currency")
    if len(set(ad_claims.percents)) > 1 or len(set(page_claims.percents)) > 1:
        ambiguous_fields.add("percent_off")
    if len(set(ad_claims.thresholds)) > 1 or len(set(page_claims.thresholds)) > 1:
        ambiguous_fields.add("free_delivery_threshold")
    if len({item.value for item in ad_claims.end_dates}) > 1:
        ambiguous_fields.add("end_date")
    if len({item.value for item in page_claims.end_dates}) > 1:
        ambiguous_fields.add("end_date")

    for field_name in sorted(ambiguous_fields):
        findings.append(_ambiguous_finding(ad, field_name, file_ref))

    if "currency" not in ambiguous_fields and "percent_off" not in ambiguous_fields:
        for percent in sorted(set(ad_claims.percents)):
            if percent in page_claims.percents:
                continue
            if not page_claims.percents:
                if not any(f.field == "offer" for f in findings):
                    findings.append(
                        _finding(
                            ad,
                            "offer_not_found",
                            "offer",
                            "The ad claims an offer, but the loaded page shows no offer text to compare.",
                            file_ref=file_ref,
                            proposed_fix="Have a person verify the offer on the page.",
                        )
                    )
                continue
            findings.append(
                _finding(
                    ad,
                    "discount_mismatch",
                    "percent_off",
                    f"Discount differs: ad {_format_decimal(percent)}%; page "
                    f"{', '.join(_format_decimal(value) + '%' for value in sorted(set(page_claims.percents)))}.",
                    file_ref=file_ref,
                    proposed_fix="Make the page offer match the discount stated in the ad.",
                )
            )

    if "currency" not in ambiguous_fields and "free_delivery_threshold" not in ambiguous_fields:
        if ad_claims.thresholds:
            if page_claims.thresholds:
                for threshold in sorted(set(ad_claims.thresholds)):
                    if threshold not in page_claims.thresholds:
                        findings.append(
                            _finding(
                                ad,
                                "threshold_mismatch",
                                "free_delivery_threshold",
                                f"Free-delivery threshold differs: ad ${_format_decimal(threshold)}; "
                                f"page ${', $'.join(_format_decimal(value) for value in sorted(set(page_claims.thresholds)))}.",
                                file_ref=file_ref,
                                proposed_fix="Make the delivery threshold on the page match the ad.",
                            )
                        )
            elif page_claims.free_delivery:
                findings.append(_ambiguous_finding(ad, "free_delivery_threshold", file_ref))
            elif page_claims.has_offer_text:
                findings.append(
                    _finding(
                        ad,
                        "offer_not_found",
                        "free_delivery_threshold",
                        "The ad claims a delivery threshold, but the page does not show one.",
                        file_ref=file_ref,
                        proposed_fix="Have a person verify the delivery threshold on the page.",
                    )
                )
            elif not any(f.field == "offer" for f in findings):
                findings.append(
                    _finding(
                        ad,
                        "offer_not_found",
                        "offer",
                        "The ad claims an offer, but the loaded page shows no offer text to compare.",
                        file_ref=file_ref,
                        proposed_fix="Have a person verify the offer on the page.",
                    )
                )
        elif ad_claims.free_delivery:
            if page_claims.thresholds:
                findings.append(_ambiguous_finding(ad, "free_delivery", file_ref))
            elif not page_claims.free_delivery:
                findings.append(
                    _finding(
                        ad,
                        "offer_not_found",
                        "free_delivery",
                        "The ad claims free delivery, but the page does not show that offer.",
                        file_ref=file_ref,
                        proposed_fix="Have a person verify the delivery offer on the page.",
                    )
                )

    if ad_claims.free_gift and not page_claims.free_gift:
        if page_claims.has_offer_text:
            findings.append(
                _finding(
                    ad,
                    "offer_not_found",
                    "free_gift",
                    "The ad claims a free gift, but the page does not show that offer.",
                    file_ref=file_ref,
                    proposed_fix="Have a person verify the free-gift offer on the page.",
                )
            )
        elif not any(f.field == "offer" for f in findings):
            findings.append(
                _finding(
                    ad,
                    "offer_not_found",
                    "offer",
                    "The ad claims an offer, but the loaded page shows no offer text to compare.",
                    file_ref=file_ref,
                    proposed_fix="Have a person verify the offer on the page.",
                )
            )

    for quantity, percent in sorted(set(ad_claims.multi_buy)):
        if "currency" in ambiguous_fields:
            break
        if (quantity, percent) in page_claims.multi_buy:
            continue
        same_type = page_claims.multi_buy
        if same_type:
            page_values = ", ".join(f"buy {q} save {_format_decimal(p)}%" for q, p in sorted(set(same_type)))
            findings.append(
                _finding(
                    ad,
                    "offer_mismatch",
                    "multi_buy",
                    f"Multi-buy offer differs: ad buy {quantity} save {_format_decimal(percent)}%; "
                    f"page {page_values}.",
                    file_ref=file_ref,
                    proposed_fix="Make the multi-buy offer on the page match the ad.",
                )
            )
        elif page_claims.has_offer_text:
            findings.append(
                _finding(
                    ad,
                    "offer_not_found",
                    "multi_buy",
                    "The ad claims a multi-buy offer, but the page does not show that offer.",
                    file_ref=file_ref,
                    proposed_fix="Have a person verify the multi-buy offer on the page.",
                )
            )
        elif not any(f.field == "offer" for f in findings):
            findings.append(
                _finding(
                    ad,
                    "offer_not_found",
                    "offer",
                    "The ad claims an offer, but the loaded page shows no offer text to compare.",
                    file_ref=file_ref,
                    proposed_fix="Have a person verify the offer on the page.",
                )
            )

    if "currency" not in ambiguous_fields and "price" not in ambiguous_fields:
        if len(set(ad_claims.prices)) > 1:
            findings.append(_ambiguous_finding(ad, "price", file_ref))
        elif ad_claims.prices:
            price = ad_claims.prices[0]
            if price not in page_claims.prices:
                page_values = ", ".join(f"${_format_decimal(value)}" for value in sorted(set(page_claims.prices)))
                findings.append(
                    _finding(
                        ad,
                        "price_mismatch",
                        "price",
                        f"Ad price ${_format_decimal(price)} does not match a visible page price"
                        + (f" ({page_values})." if page_values else ". No visible page price was found."),
                        file_ref=file_ref,
                        proposed_fix="Check that the visible page price matches the ad.",
                    )
                )

    if "end_date" not in ambiguous_fields and ad_claims.end_dates:
        if not page_claims.end_dates:
            findings.append(
                _finding(
                    ad,
                    "offer_not_found",
                    "end_date",
                    "The ad states an end date, but the page does not show an end date to compare.",
                    file_ref=file_ref,
                    proposed_fix="Have a person verify the offer end date on the page.",
                )
            )
        elif len(set(item.value for item in ad_claims.end_dates)) == 1:
            ad_date = ad_claims.end_dates[0]
            latest_page_date = max(item.value for item in page_claims.end_dates)
            if ad_date.value > latest_page_date:
                assumption = " (ad year assumed from as_of)" if ad_date.assumed_year else ""
                page_assumption = " (page year assumed from as_of)" if any(
                    item.assumed_year for item in page_claims.end_dates
                ) else ""
                findings.append(
                    _finding(
                        ad,
                        "date_mismatch",
                        "end_date",
                        f"Ad end date {ad_date.value.isoformat()}{assumption} is later than page end date "
                        f"{latest_page_date.isoformat()}{page_assumption}.",
                        file_ref=file_ref,
                        proposed_fix="Make the ad end date no later than the page offer end date.",
                    )
                )

    return findings
