import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { OctopAgent } from "../../../context/AgentContext";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { usePathVisible } from "../../../hooks/usePathVisible";
import { expertTabHidden } from "../../../utils/permissions";
import {
  CHAT_NAV_SCOPES,
  matchesChatNavScope,
  type ChatNavScope,
} from "../utils/chatNavScope";
import styles from "../index.module.less";

const NAV_SCOPE_LABELS: Record<
  ChatNavScope,
  { key: string; fallback: string; emptyKey: string; emptyFallback: string }
> = {
  experts: {
    key: "chat.navScopeExperts",
    fallback: "专家",
    emptyKey: "chat.navScopeEmptyExperts",
    emptyFallback: "还没有专家",
  },
  teams: {
    key: "chat.navScopeTeams",
    fallback: "团队",
    emptyKey: "chat.navScopeEmptyTeams",
    emptyFallback: "还没有团队",
  },
  cloud: {
    key: "chat.navScopeCloud",
    fallback: "云端",
    emptyKey: "chat.navScopeEmptyCloud",
    emptyFallback: "还没有云端协同",
  },
};

export function chatNavScopeEmptyCopy(scope: ChatNavScope): {
  key: string;
  fallback: string;
} {
  const meta = NAV_SCOPE_LABELS[scope];
  return { key: meta.emptyKey, fallback: meta.emptyFallback };
}

const NAV_SCOPE_CREATE: Partial<
  Record<ChatNavScope, { key: string; fallback: string; to: string }>
> = {
  teams: {
    key: "chat.navScopeCreateTeam",
    fallback: "创建第一个团队",
    to: "/experts?tab=teams",
  },
  cloud: {
    key: "chat.navScopeCreateCloud",
    fallback: "添加云端协同",
    to: "/bridge",
  },
};

/** Empty team / cloud list, with a link to create the first one. */
export function ChatNavScopeEmpty({ scope }: { scope: ChatNavScope }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const copy = chatNavScopeEmptyCopy(scope);
  const action = NAV_SCOPE_CREATE[scope];
  return (
    <div className={styles.sessionEmptyAgents}>
      <p className={styles.sessionEmptyAgentsText}>
        {t(copy.key, copy.fallback)}
      </p>
      {action ? (
        <button
          type="button"
          className={styles.sessionEmptyAgentsLink}
          onClick={() => navigate(action.to)}
        >
          {t(action.key, action.fallback)}
        </button>
      ) : null}
    </div>
  );
}

/**
 * Expert / team / cloud switch under the chat nav search box.
 * Team and cloud show only when the role can open their create page or
 * already has such an agent (a chat-and-workbench role sees just experts;
 * a role that hides the Experts-page team tab sees no team tab here either).
 */
export default function ChatNavScopeTabs({
  agents,
  value,
  onChange,
}: {
  agents: readonly OctopAgent[];
  value: ChatNavScope;
  onChange: (scope: ChatNavScope) => void;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const pathVisible = usePathVisible();
  const scopes = CHAT_NAV_SCOPES.filter((scope) => {
    const create = NAV_SCOPE_CREATE[scope];
    const canCreate =
      !!create &&
      pathVisible(create.to) &&
      !(scope === "teams" && expertTabHidden(user, "teams"));
    return (
      !create ||
      canCreate ||
      agents.some((agent) => matchesChatNavScope(agent, scope))
    );
  });
  // The last team or cloud agent went away while its tab was open.
  const valueHidden = !scopes.includes(value);
  useEffect(() => {
    if (valueHidden) onChange("experts");
  }, [valueHidden, onChange]);
  return (
    <div className={styles.sessionScopeTabs} role="tablist">
      {scopes.map((scope) => {
        const meta = NAV_SCOPE_LABELS[scope];
        const selected = value === scope;
        return (
          <button
            key={scope}
            type="button"
            role="tab"
            aria-selected={selected}
            className={`${styles.sessionScopeTab} ${
              selected ? styles.sessionScopeTabActive : ""
            }`}
            onClick={() => onChange(scope)}
          >
            {t(meta.key, meta.fallback)}
          </button>
        );
      })}
    </div>
  );
}
