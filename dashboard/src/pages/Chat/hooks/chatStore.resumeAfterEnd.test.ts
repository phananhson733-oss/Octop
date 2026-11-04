import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getSnapshot,
  isHistoryStale,
  onSessionEvent,
  removeSession,
  sendTurn,
  setHistoryPage,
  type SessionEvent,
} from "./chatStore";

const THREAD = "thr_resume_after_end";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
    });
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  receive(frame: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

describe("stream resume after the turn ended", () => {
  const RealWS = globalThis.WebSocket;
  afterEach(() => {
    globalThis.WebSocket = RealWS;
    vi.useRealTimers();
    removeSession(THREAD);
  });

  it("reloads the stored turn when the socket dropped and the turn finished meanwhile", async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const events: SessionEvent[] = [];
    const stop = onSessionEvent((event) => events.push(event));
    try {
      setHistoryPage(THREAD, [], { hasMore: false, nextOffset: 0 });
      const turn = sendTurn(
        THREAD,
        "write the report",
        "agent-1",
        "",
        undefined,
        undefined,
        null,
        THREAD,
      );
      await Promise.resolve();
      const first = FakeWebSocket.instances[0];
      first.receive({ type: "token", content: "Writing the report now." });
      first.receive({
        type: "tool_call_chunk",
        id: "call-save",
        name: "seo_save_report",
        args: '{"html": "<html>',
      });
      // The socket drops mid-turn; the server finishes the turn before the
      // tab is back.
      first.close();
      await turn;
      expect(getSnapshot(THREAD).isStreaming).toBe(true);

      vi.advanceTimersByTime(400);
      await Promise.resolve();
      const second = FakeWebSocket.instances[1];
      expect(second.sent.some((row) => row.includes('"subscribe"'))).toBe(true);
      second.receive({ type: "turn_status", thread_id: THREAD, active: false });

      expect(getSnapshot(THREAD).isStreaming).toBe(false);
      expect(isHistoryStale(THREAD)).toBe(true);
      expect(events).toContainEqual({
        kind: "sessionsChanged",
        sessionId: THREAD,
        agentId: "agent-1",
      });
    } finally {
      stop();
      for (const ws of FakeWebSocket.instances) ws.readyState = 3;
    }
  });

  it("reloads the stored turn when a rejoined turn ends, since the gap never arrived", async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const events: SessionEvent[] = [];
    const stop = onSessionEvent((event) => events.push(event));
    try {
      setHistoryPage(THREAD, [], { hasMore: false, nextOffset: 0 });
      const turn = sendTurn(
        THREAD,
        "cut one highlight",
        "agent-1",
        "",
        undefined,
        undefined,
        null,
        THREAD,
      );
      await Promise.resolve();
      const first = FakeWebSocket.instances[0];
      first.receive({ type: "token", content: "Rendering now." });
      // The socket drops while the turn keeps running on the server.
      first.close();
      await turn;

      vi.advanceTimersByTime(400);
      await Promise.resolve();
      const second = FakeWebSocket.instances[1];
      second.receive({ type: "turn_status", thread_id: THREAD, active: true });
      second.receive({
        type: "steer.queue_snapshot",
        seq: 1,
        turn_id: "turn-1",
        queue_length: 0,
        continuation_count: 0,
        items: [],
        thread_id: THREAD,
      });
      second.receive({ type: "token", content: "Here is the MP4." });
      second.receive({ type: "done" });
      expect(second.readyState).toBe(1);
      expect(events).not.toContainEqual(
        expect.objectContaining({ kind: "sessionsChanged" }),
      );
      second.receive({ type: "turn.ended", thread_id: THREAD });

      expect(getSnapshot(THREAD).isStreaming).toBe(false);
      expect(isHistoryStale(THREAD)).toBe(true);
      expect(events).toContainEqual({
        kind: "sessionsChanged",
        sessionId: THREAD,
        agentId: "agent-1",
      });
    } finally {
      stop();
      for (const ws of FakeWebSocket.instances) ws.readyState = 3;
    }
  });
});
