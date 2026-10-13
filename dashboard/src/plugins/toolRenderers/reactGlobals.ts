import * as React from "react";
import * as ReactJSX from "react/jsx-runtime";

declare global {
  interface Window {
    __OCTOP_REACT__?: typeof React;
    __OCTOP_JSX__?: typeof ReactJSX;
  }
}

/** Expose React for plugin ESM blobs (no bundler import map). */
export function exposeReactGlobals(): void {
  if (typeof window === "undefined") return;
  window.__OCTOP_REACT__ = React;
  window.__OCTOP_JSX__ = ReactJSX;
}
