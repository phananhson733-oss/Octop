import { describe, expect, it } from "vitest";
import { groupSharedVolumeRows } from "./groupSharedVolumeRows";
import reference from "../../../../../tests/fixtures/seo/openseo-reference.json";
const base = reference.baseRow;
const row = {
  keyword: base.keyword,
  search_volume: base.searchVolume,
  cpc: base.cpc,
  competition: base.competition,
  keyword_difficulty: base.keywordDifficulty,
  intent: base.intent,
  trend: base.trend.map((entry) => ({
    year: entry.year,
    month: entry.month,
    search_volume: entry.searchVolume,
  })),
};
describe("OpenSEO shared-volume parity", () => {
  it("puts the normalized searched keyword first, indents its close variant, and preserves metrics", () => {
    const input = [{ ...row, keyword: "caregiver" }, row];
    const output = groupSharedVolumeRows(input, " CareGiving ", true);
    expect(output.map((r) => [r.keyword, r.parentKeyword])).toEqual([
      ["caregiving", null],
      ["caregiver", "caregiving"],
    ]);
    expect(output.every((r) => r.search_volume === 110000)).toBe(true);
    expect(input[0].keyword).toBe("caregiver");
  });
  it("never groups low-volume or flat-placeholder series, and respects disabled grouping", () => {
    for (const baseRow of [
      { ...row, search_volume: 999 },
      {
        ...row,
        trend: row.trend.map((entry) => ({ ...entry, search_volume: 110000 })),
      },
    ]) {
      expect(
        groupSharedVolumeRows(
          [baseRow, { ...baseRow, keyword: "other" }],
          undefined,
          true,
        ).every((r) => r.parentKeyword === null),
      ).toBe(true);
    }
    expect(
      groupSharedVolumeRows(
        [{ ...row, keyword: "caregiver" }, row],
        row.keyword,
        false,
      ).map((r) => [r.keyword, r.parentKeyword]),
    ).toEqual([
      ["caregiver", null],
      ["caregiving", null],
    ]);
  });
});
