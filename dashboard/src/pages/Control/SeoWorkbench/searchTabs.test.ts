import { describe, expect, it } from "vitest";
import {
  closeTab,
  openTabs,
  parseStoredTabs,
  SEARCH_TABS_LIMIT,
  tabId,
} from "./searchTabs";

const empty = { tabs: [], activeId: null };

describe("search tabs", () => {
  it("reuses open tabs and evicts the oldest past the limit", () => {
    let state = openTabs(empty, [
      { runId: "r0", seed: "a" },
      { runId: "r0", seed: "b" },
    ]);
    expect(state.activeId).toBe(tabId("r0", "a"));
    state = openTabs(state, [{ runId: "r0", seed: "b" }]);
    expect(state.tabs).toHaveLength(2);
    expect(state.activeId).toBe(tabId("r0", "b"));
    for (let i = 1; i <= SEARCH_TABS_LIMIT; i++)
      state = openTabs(state, [{ runId: `r${i}`, seed: "a" }]);
    expect(state.tabs).toHaveLength(SEARCH_TABS_LIMIT);
    expect(state.tabs[0].runId).toBe("r1");
    expect(state.activeId).toBe(tabId(`r${SEARCH_TABS_LIMIT}`, "a"));
  });
  it("selects the right neighbour, else the left, when closing the active tab", () => {
    const state = openTabs(
      empty,
      ["a", "b", "c"].map((seed) => ({ runId: "r", seed })),
      { runId: "r", seed: "b" },
    );
    expect(closeTab(state, tabId("r", "b")).activeId).toBe(tabId("r", "c"));
    const last = { ...state, activeId: tabId("r", "c") };
    expect(closeTab(last, tabId("r", "c")).activeId).toBe(tabId("r", "b"));
    expect(closeTab(state, tabId("r", "a"))).toEqual({
      tabs: state.tabs.slice(1),
      activeId: tabId("r", "b"),
    });
  });
  it("drops malformed stored tabs", () => {
    expect(
      parseStoredTabs({
        tabs: [
          { runId: "r", seed: "a" },
          { runId: "", seed: "b" },
          { seed: "c" },
          null,
        ],
        activeId: "bogus",
      }),
    ).toEqual({
      tabs: [{ id: tabId("r", "a"), runId: "r", seed: "a" }],
      activeId: null,
    });
    expect(parseStoredTabs("nope")).toEqual(empty);
  });
});
