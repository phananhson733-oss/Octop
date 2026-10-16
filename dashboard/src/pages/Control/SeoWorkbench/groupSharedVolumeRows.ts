/*
 * Adapted from every-app/open-seo d7ea785, groupSharedVolumeRows.ts.
 * MIT License
 *
 * Copyright (c) 2026 Ben Senescu
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 *
 */
import type { KeywordRow as KeywordResearchRow } from "../../../api/modules/seo";

/** A visible keyword row, optionally indented beneath a shared-volume parent. */
export type KeywordResearchDisplayRow = KeywordResearchRow & {
  parentKeyword: string | null;
};

// Only group above this volume: bucketed low volumes (10, 20, ...) collide by
// coincidence, while real close-variant groups share big grouped numbers.
const SHARED_VOLUME_MIN = 1000;

function sharedVolumeKey(row: KeywordResearchRow): string | null {
  if (row.search_volume == null || row.search_volume < SHARED_VOLUME_MIN) {
    return null;
  }
  // A flat series is a bucketed placeholder, not a fingerprint, so two
  // unrelated keywords could share it by coincidence.
  if (new Set(row.trend.map((entry) => entry.search_volume)).size <= 1) {
    return null;
  }
  const series = row.trend
    .map((entry) => `${entry.year}-${entry.month}:${entry.search_volume}`)
    .join(",");
  return `${row.search_volume}|${row.cpc}|${series}`;
}

/**
 * Google Ads reports one volume for a whole close-variant group ("caregiving"
 * / "caregiver" / "caregivers"), so those rows all claim the same searches.
 * Place rows with an identical volume, CPC, and monthly series after the
 * searched keyword when it is in the group, otherwise under the first one in
 * the given order. Display only: every keyword keeps its own metrics
 * when grouping is enabled. With grouping off, preserve the input order.
 */
export function groupSharedVolumeRows(
  rows: KeywordResearchRow[],
  searchedKeyword: string | undefined,
  groupKeywords: boolean,
): KeywordResearchDisplayRow[] {
  if (!groupKeywords) {
    return rows.map((row) => ({ ...row, parentKeyword: null }));
  }
  // Row keywords are lowercase; the searched keyword arrives as typed.
  const seed = searchedKeyword?.trim().toLowerCase();
  const groups = new Map<string, KeywordResearchRow[]>();
  const result: KeywordResearchRow[][] = [];

  for (const row of rows) {
    const key = sharedVolumeKey(row);
    const group = key ? groups.get(key) : undefined;
    if (group) {
      if (row.keyword === seed) group.unshift(row);
      else group.push(row);
      continue;
    }
    const newGroup = [row];
    if (key) groups.set(key, newGroup);
    result.push(newGroup);
  }

  return result.flatMap((group) =>
    group.map((row, index) => ({
      ...row,
      parentKeyword: index === 0 ? null : group[0].keyword,
    })),
  );
}
