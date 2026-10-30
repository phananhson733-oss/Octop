import { useEffect, useId, useRef } from "react";
import { Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  Bookmark,
  Brain,
  ChartLine,
  ClipboardCheck,
  FileText,
  Globe,
  ListChecks,
  Search,
  Sparkles,
  TrendingUp,
  type LucideIcon,
} from "lucide-react";
import styles from "./index.module.less";

export type SeoView =
  | "research"
  | "saved"
  | "competitors"
  | "audit"
  | "actions"
  | "rank"
  | "search"
  | "ai"
  | "reports"
  | "context";

/**
 * The workbench's views grouped as OpenSEO groups its sidebar: tools that look
 * anything up, the project's own site, AI answers, and what the expert reads
 * and writes. ``help`` is the line under the view's title.
 */
export const SEO_NAV: {
  group: string;
  views: { key: SeoView; label: string; help: string; icon: LucideIcon }[];
}[] = [
  {
    group: "seo.navGroup.research",
    views: [
      {
        key: "research",
        label: "seo.research",
        help: "seo.viewHelp.research",
        icon: Search,
      },
      {
        key: "saved",
        label: "seo.saved",
        help: "seo.viewHelp.saved",
        icon: Bookmark,
      },
      {
        key: "competitors",
        label: "seo.competitors",
        help: "seo.viewHelp.competitors",
        icon: Globe,
      },
    ],
  },
  {
    group: "seo.navGroup.site",
    views: [
      {
        key: "audit",
        label: "seo.audit",
        help: "seo.auditHelp",
        icon: ClipboardCheck,
      },
      {
        key: "actions",
        label: "seo.actions.title",
        help: "seo.actions.help",
        icon: ListChecks,
      },
      {
        key: "rank",
        label: "seo.rank",
        help: "seo.rankHelp",
        icon: TrendingUp,
      },
      {
        key: "search",
        label: "seo.gsc",
        help: "seo.viewHelp.search",
        icon: ChartLine,
      },
    ],
  },
  {
    group: "seo.navGroup.ai",
    views: [{ key: "ai", label: "seo.ai", help: "seo.aiHelp", icon: Sparkles }],
  },
  {
    group: "seo.navGroup.expert",
    views: [
      {
        key: "reports",
        label: "seo.reports",
        help: "seo.viewHelp.reports",
        icon: FileText,
      },
      {
        key: "context",
        label: "seo.context",
        help: "seo.contextHelp",
        icon: Brain,
      },
    ],
  },
];

export const SEO_VIEWS = SEO_NAV.flatMap((group) => group.views);

export function SeoNav({
  view,
  onChange,
}: {
  view: SeoView;
  onChange: (view: SeoView) => void;
}) {
  const { t } = useTranslation();
  const ids = useId();
  const nav = useRef<HTMLElement>(null);
  // In the narrow layout the nav is one scrolling row: keep the current item in it.
  useEffect(() => {
    const row = nav.current;
    const item = row?.querySelector('[aria-current="page"]');
    if (!row || !item) return;
    const box = row.getBoundingClientRect();
    const at = item.getBoundingClientRect();
    if (at.left < box.left) row.scrollLeft -= box.left - at.left;
    else if (at.right > box.right) row.scrollLeft += at.right - box.right;
  }, [view]);
  return (
    <nav ref={nav} className={styles.nav} aria-label={t("seo.navLabel")}>
      {SEO_NAV.map((group) => (
        <div
          key={group.group}
          role="group"
          aria-labelledby={`${ids}${group.group}`}
          className={styles.navGroup}
        >
          <div id={`${ids}${group.group}`} className={styles.navGroupLabel}>
            {t(group.group)}
          </div>
          {group.views.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              className={styles.navItem}
              aria-current={key === view ? "page" : undefined}
              onClick={() => onChange(key)}
            >
              <Icon size={16} aria-hidden />
              <span>{t(label)}</span>
            </button>
          ))}
        </div>
      ))}
    </nav>
  );
}

/** A view's title and what it does. */
export function ViewHeader({ title, help }: { title: string; help: string }) {
  return (
    <div className={styles.viewHeader}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        {title}
      </Typography.Title>
      <Typography.Text type="secondary">{help}</Typography.Text>
    </div>
  );
}
