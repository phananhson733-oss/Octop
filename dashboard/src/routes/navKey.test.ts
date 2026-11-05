import { describe, expect, it } from "vitest";
import { homePath } from "./navKey";

const user = (visible_nav: string[] | null, permissions: string[] = []) => ({
  role: "member",
  permissions,
  visible_nav,
});

describe("homePath", () => {
  it("lands on chat unless the role hides it", () => {
    expect(homePath(null)).toBe("/chat");
    expect(homePath(user(null))).toBe("/chat");
    expect(homePath(user(["chat", "workbench"]))).toBe("/chat");
    expect(homePath(user(["workbench"]), true)).toBe("/workbench");
    expect(homePath(user(["experts", "workbench"], ["experts"]))).toBe(
      "/experts",
    );
  });

  it("skips a kept page the account cannot open", () => {
    expect(
      homePath(user(["experts", "knowledge-bases"], ["knowledge_bases"])),
    ).toBe("/knowledge-bases");
    // No plugin page and no terminal/browser: the Workbench would refuse.
    expect(
      homePath(user(["workbench", "knowledge-bases"], ["knowledge_bases"])),
    ).toBe("/knowledge-bases");
  });

  it("gives up rather than loop back to chat", () => {
    expect(homePath(user(["nope"]))).toBeNull();
    expect(homePath(user(["workbench"]))).toBeNull();
  });
});
