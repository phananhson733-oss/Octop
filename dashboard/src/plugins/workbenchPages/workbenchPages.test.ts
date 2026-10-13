import { afterEach, describe, expect, it } from "vitest";
import {
  getWorkbenchPages,
  registerWorkbenchPage,
  resetWorkbenchPages,
  unregisterWorkbenchPages,
  workbenchPageTitle,
} from "./registry";
import { workbenchAccess, workbenchTabOf } from "./access";
import { loadInstalledPluginUis } from "../toolRenderers/loader";

const Page = () => null;
const member = { role: "user", permissions: [] as string[] };
const operator = { role: "user", permissions: ["terminal"] };
const admin = { role: "admin", permissions: [] as string[] };

afterEach(() => {
  resetWorkbenchPages();
});

describe("workbench page registry", () => {
  it("keys a page by plugin and id, and replaces it on re-register", () => {
    registerWorkbenchPage({
      id: "board",
      pluginId: "pick",
      title: "A",
      component: Page,
    });
    const before = getWorkbenchPages();
    registerWorkbenchPage({
      id: "board",
      pluginId: "pick",
      title: "B",
      component: Page,
    });
    expect(getWorkbenchPages()).not.toBe(before);
    expect(getWorkbenchPages().map((page) => [page.slug, page.title])).toEqual([
      ["pick.board", "B"],
    ]);
  });

  it("drops a plugin's pages when the plugin unloads", () => {
    registerWorkbenchPage({
      id: "a",
      pluginId: "one",
      title: "A",
      component: Page,
    });
    registerWorkbenchPage({
      id: "b",
      pluginId: "two",
      title: "B",
      component: Page,
    });
    unregisterWorkbenchPages("one");
    expect(getWorkbenchPages().map((page) => page.slug)).toEqual(["two.b"]);
  });

  it("refuses an id that would not survive as a URL segment", () => {
    registerWorkbenchPage({
      id: "a/b",
      pluginId: "pick",
      title: "A",
      component: Page,
    });
    expect(getWorkbenchPages()).toEqual([]);
  });

  it("picks the title for the dashboard language", () => {
    expect(workbenchPageTitle("Picks", "zh-CN")).toBe("Picks");
    expect(workbenchPageTitle({ zh: "选剧", en: "Picks" }, "zh-CN")).toBe(
      "选剧",
    );
    expect(workbenchPageTitle({ zh: "选剧", en: "Picks" }, "en")).toBe("Picks");
    expect(workbenchPageTitle({ zh: "选剧" }, "en")).toBe("选剧");
  });
});

describe("workbenchAccess", () => {
  it("reads the tab segment", () => {
    expect(workbenchTabOf("/workbench")).toBeNull();
    expect(workbenchTabOf("/workbench/pick.board")).toBe("pick.board");
    expect(workbenchTabOf("/workbench/terminal/x")).toBe("terminal");
  });

  it("keeps terminal and browser behind their permissions", () => {
    const pages = ["pick.board"];
    expect(workbenchAccess(member, "/workbench/terminal", pages, true)).toBe(
      "deny",
    );
    expect(workbenchAccess(member, "/workbench/browser", pages, true)).toBe(
      "deny",
    );
    expect(workbenchAccess(operator, "/workbench/terminal", pages, true)).toBe(
      "allow",
    );
    expect(workbenchAccess(operator, "/workbench/browser", pages, true)).toBe(
      "deny",
    );
    expect(workbenchAccess(admin, "/workbench/browser", [], true)).toBe(
      "allow",
    );
  });

  it("opens plugin pages to an account with neither permission", () => {
    const pages = ["pick.board"];
    expect(workbenchAccess(member, "/workbench", pages, true)).toBe("allow");
    expect(workbenchAccess(member, "/workbench/pick.board", pages, true)).toBe(
      "allow",
    );
    expect(workbenchAccess(member, "/workbench/other.page", pages, true)).toBe(
      "deny",
    );
    expect(workbenchAccess(member, "/workbench", [], true)).toBe("deny");
  });

  it("waits for plugin UIs before denying a page that may still register", () => {
    expect(workbenchAccess(member, "/workbench", [], false)).toBe("pending");
    expect(workbenchAccess(member, "/workbench/pick.board", [], false)).toBe(
      "pending",
    );
    expect(workbenchAccess(member, "/workbench/terminal", [], false)).toBe(
      "deny",
    );
    expect(workbenchAccess(null, "/workbench", [], true)).toBe("pending");
  });
});

describe("plugin UI loader", () => {
  it("exposes React before any plugin module can be evaluated", async () => {
    // The layout loads plugin UIs on pages where the chat page never mounts.
    delete window.__OCTOP_REACT__;
    await loadInstalledPluginUis([]);
    expect(window.__OCTOP_REACT__?.createElement).toBeTypeOf("function");
  });
});
