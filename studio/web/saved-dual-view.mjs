const sections = ["question", "explanation"];

export function createDualViewState() {
  return Object.fromEntries(sections.map((section) => [section, { mode: null, assetId: null, scrollTop: 0 }]));
}

export function resetDualViewState(view) {
  for (const section of sections) {
    view[section].assetId = null;
    view[section].scrollTop = 0;
  }
}

export function readingMode(view, hasSource, hasContent) {
  return view.mode ?? (hasSource ? "source" : hasContent ? "read" : "source");
}

export function readingAsset(view, assets, preferredAssetId = null) {
  return assets.find((asset) => asset.asset_id === view.assetId)
    ?? assets.find((asset) => asset.asset_id === preferredAssetId)
    ?? assets[0] ?? null;
}

export async function saveDualViewDraft(request, revision, packageData) {
  return request({ expected_revision: revision, package: packageData });
}
