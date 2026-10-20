"""Fix SocialFeedModal ad prop safety and dispatch/campaign preview modal calls."""

def patch_modal():
    path = "apps/web/components/SocialFeedModal.jsx"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n")

    old_fn_start = """export default function SocialFeedModal({
  ad,
  adId,
  platform,
  headline: propHeadline,
  adText,
  caption,
  clientHandle,
  handle: propHandle,
  destinationUrl,
  reviewStatus: propReviewStatus,
  salesUsd: propSalesUsd,
  ordersCount: propOrdersCount,
  onClose,
}) {"""

    new_fn_start = """export default function SocialFeedModal(props) {
  const {
    ad: propAd,
    adId,
    platform,
    headline: propHeadline,
    adText,
    caption,
    clientHandle,
    clientId: propClientId,
    handle: propHandle,
    destinationUrl,
    reviewStatus: propReviewStatus,
    salesUsd: propSalesUsd,
    ordersCount: propOrdersCount,
    onClose,
  } = props;

  const ad = propAd || props || {};"""

    if old_fn_start in content:
        content = content.replace(old_fn_start, new_fn_start)

    # Replace ad.client_id with (ad.client_id || propClientId || "LG")
    content = content.replace(
        '{ad.client_id ? ad.client_id.slice(0, 2).toUpperCase() : "LG"}',
        '{(ad.client_id || propClientId || "LG").slice(0, 2).toUpperCase()}'
    )
    content = content.replace(
        '{ad.client_id ? ad.client_id.toUpperCase() : "VERIFIED CAMPAIGN"}',
        '{(ad.client_id || propClientId || "VERIFIED CAMPAIGN").toUpperCase()}'
    )
    content = content.replace(
        'Ad ID: {ad.ad_id}',
        'Ad ID: {adId || ad.ad_id || "meta_ad_live"}'
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("Updated SocialFeedModal.jsx successfully.")


def patch_pages():
    for page_path in ["apps/web/app/dispatches/page.jsx", "apps/web/app/campaigns/page.jsx"]:
        with open(page_path, "r", encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n")

        old_usage = """      {previewAd && (
        <SocialFeedModal
          adId={previewAd.ad_id}
          platform={previewAd.platform}
          headline={previewAd.headline}
          clientHandle={previewAd.handle}
          onClose={() => setPreviewAd(null)}
        />
      )}"""

        new_usage = """      {previewAd && (
        <SocialFeedModal
          ad={previewAd}
          adId={previewAd.ad_id}
          platform={previewAd.platform}
          headline={previewAd.headline}
          clientHandle={previewAd.handle}
          clientId={previewAd.client_id}
          destinationUrl={previewAd.destination_url}
          reviewStatus={previewAd.platform_review_status}
          onClose={() => setPreviewAd(null)}
        />
      )}"""

        if old_usage in content:
            content = content.replace(old_usage, new_usage)
            with open(page_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Updated {page_path} successfully.")
        else:
            print(f"{page_path} usage not matched or already updated.")


if __name__ == "__main__":
    patch_modal()
    patch_pages()
