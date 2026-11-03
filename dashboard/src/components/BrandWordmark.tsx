import type { CSSProperties } from "react";

export const BRAND_NAME = "GGwork";

/** Transparent GenGrowth mark; the 64px tier keeps its optical weight at small sizes. */
export function brandMarkSrc(size: number): string {
  return size <= 36 ? "/logo-mark-64.png" : "/logo-mark-256.png";
}

interface BrandWordmarkProps {
  /** Mark height in px; the name scales with it. */
  size: number;
  className?: string;
  style?: CSSProperties;
}

/** GenGrowth mark + product name, drawn as text so one asset serves both themes. */
export default function BrandWordmark({
  size,
  className,
  style,
}: BrandWordmarkProps) {
  return (
    <span
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: Math.round(size * 0.22),
        flexShrink: 0,
        minWidth: 0,
        ...style,
      }}
    >
      <img
        src={brandMarkSrc(size)}
        alt=""
        aria-hidden
        style={{
          width: size,
          height: size,
          objectFit: "contain",
          display: "block",
          flexShrink: 0,
        }}
      />
      <span
        style={{
          fontSize: Math.round(size * 0.62),
          fontWeight: 700,
          lineHeight: 1,
          letterSpacing: "-0.01em",
          color: "var(--fn-text-primary)",
          whiteSpace: "nowrap",
        }}
      >
        {BRAND_NAME}
      </span>
    </span>
  );
}
