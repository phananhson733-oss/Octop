import { StrictMode, type ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useHandOffSend } from "./useHandOffSend";

function wrapper({ children }: { children: ReactNode }) {
  return (
    <StrictMode>
      <MemoryRouter
        initialEntries={[
          { pathname: "/chat/a1/t1", state: { autoSendInput: "Analyze it" } },
        ]}
      >
        {children}
      </MemoryRouter>
    </StrictMode>
  );
}

function setup(props: { ready: boolean; failed?: boolean }) {
  const send = vi.fn();
  const fallback = vi.fn();
  const view = renderHook(
    ({ ready, failed }: { ready: boolean; failed: boolean }) => {
      useHandOffSend({ threadId: "t1", ready, failed, send, fallback });
      return useLocation();
    },
    { wrapper, initialProps: { failed: false, ...props } },
  );
  return { send, fallback, view };
}

describe("useHandOffSend", () => {
  it("sends the hand-off once, after the thread is ready", () => {
    const { send, view } = setup({ ready: false });
    expect(send).not.toHaveBeenCalled();
    // Kept on the location until sent, so a reload before then still sends it.
    expect(view.result.current.state).toEqual({ autoSendInput: "Analyze it" });
    view.rerender({ ready: true, failed: false });
    view.rerender({ ready: true, failed: false });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("Analyze it");
    // Taken off the location once sent, so a reload does not send it again.
    expect(view.result.current.state).toEqual({});
  });

  it("does not send twice when effects run again on a ready thread", () => {
    // StrictMode runs every effect twice on mount.
    const { send } = setup({ ready: true });
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("leaves it in the input when the thread did not load", () => {
    const { send, fallback } = setup({ ready: false, failed: true });
    expect(fallback).toHaveBeenCalledWith("Analyze it");
    expect(send).not.toHaveBeenCalled();
  });
});
