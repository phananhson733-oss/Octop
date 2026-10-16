import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Space, Switch, Typography } from "antd";
import { RefreshCw, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  chatgptImageApi,
  type ChatgptImageSettingsInput,
  type ChatgptImageStatus,
  type ChatgptQuotaWindow,
} from "../../../api/modules/mediaGeneration";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { message } from "@/utils/antdMessage";
import styles from "./index.module.less";

const { Text } = Typography;

const WEEK_MINUTES = 7 * 24 * 60;

export function ChatgptImageCard() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [status, setStatus] = useState<ChatgptImageStatus | null>(null);
  const [draft, setDraft] = useState<ChatgptImageSettingsInput | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await chatgptImageApi.get();
      setStatus(next);
      setDraft({
        enabled: next.enabled,
        admin_only: next.admin_only,
        allow_credits: next.allow_credits,
      });
    } catch {
      message.error(t("mediaGeneration.chatgpt.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      const next = await chatgptImageApi.save(draft);
      setStatus(next);
      message.success(t("mediaGeneration.chatgpt.saved"));
    } catch (err) {
      message.error(
        err instanceof Error
          ? err.message
          : t("mediaGeneration.chatgpt.saveFailed"),
      );
    } finally {
      setSaving(false);
    }
  };

  const formatReset = (epoch: number | null) =>
    epoch
      ? formatServerDateTime(epoch, timeZone)
      : t("mediaGeneration.chatgpt.unknownTime");

  const windowLine = (window: ChatgptQuotaWindow) => {
    const minutes = window.window_minutes ?? 0;
    const period =
      minutes >= WEEK_MINUTES
        ? t("mediaGeneration.chatgpt.periodWeek")
        : t("mediaGeneration.chatgpt.periodHours", {
            hours: Math.round(minutes / 60),
          });
    return t("mediaGeneration.chatgpt.window", {
      period,
      percent: Math.round(window.used_percent),
      reset: formatReset(window.reset_at),
    });
  };

  const quota = status?.quota ?? null;

  return (
    <Card className={styles.routePanel} loading={loading && !status}>
      <div className={styles.routeHeader}>
        <div className={styles.routeHeading}>
          <span className={styles.routeIcon}>
            <Sparkles size={19} />
          </span>
          <div>
            <h2 className={styles.routeTitle}>
              {t("mediaGeneration.chatgpt.title")}
            </h2>
            <p className={styles.routeDescription}>
              {t("mediaGeneration.chatgpt.description")}
            </p>
          </div>
        </div>
        {draft && (
          <label className={styles.globalToggle}>
            <Switch
              size="small"
              checked={draft.enabled}
              onChange={(enabled) => setDraft({ ...draft, enabled })}
            />
            <span>{t("mediaGeneration.chatgpt.enable")}</span>
          </label>
        )}
      </div>

      {status && !status.logged_in && (
        <Alert
          type="info"
          showIcon
          message={t("mediaGeneration.chatgpt.notLoggedIn")}
        />
      )}

      {quota && (
        <Space direction="vertical" size={4}>
          {quota.plan_type && (
            <Text type="secondary">
              {t("mediaGeneration.chatgpt.plan", { plan: quota.plan_type })}
            </Text>
          )}
          {quota.windows.map((window, index) => (
            <Text key={index}>{windowLine(window)}</Text>
          ))}
          <Text type="secondary">
            {quota.unlimited_credits
              ? t("mediaGeneration.chatgpt.creditsUnlimited")
              : t("mediaGeneration.chatgpt.credits", {
                  balance: quota.credits_balance ?? "0",
                })}
          </Text>
          {quota.plan_exhausted && (
            <Alert
              type="warning"
              showIcon
              message={
                status?.allow_credits
                  ? t("mediaGeneration.chatgpt.exhaustedCredits")
                  : t("mediaGeneration.chatgpt.exhausted", {
                      reset: formatReset(quota.resets_at),
                    })
              }
            />
          )}
        </Space>
      )}

      {status?.quota_error && (
        <Text type="secondary">
          {t("mediaGeneration.chatgpt.quotaError", {
            error: status.quota_error,
          })}
        </Text>
      )}

      {draft && (
        <Space direction="vertical" size={8} style={{ marginTop: 12 }}>
          <label className={styles.globalToggle}>
            <Switch
              size="small"
              checked={draft.admin_only}
              onChange={(admin_only) => setDraft({ ...draft, admin_only })}
            />
            <span>{t("mediaGeneration.chatgpt.adminOnly")}</span>
          </label>
          <Text type="secondary">
            {t("mediaGeneration.chatgpt.adminOnlyHint")}
          </Text>
          <label className={styles.globalToggle}>
            <Switch
              size="small"
              checked={draft.allow_credits}
              onChange={(allow_credits) =>
                setDraft({ ...draft, allow_credits })
              }
            />
            <span>{t("mediaGeneration.chatgpt.allowCredits")}</span>
          </label>
          <Text type="secondary">
            {t("mediaGeneration.chatgpt.allowCreditsHint")}
          </Text>
        </Space>
      )}

      <div className={styles.routeFooter}>
        <Button
          icon={<RefreshCw size={14} />}
          loading={loading}
          onClick={() => void load()}
        >
          {t("common.refresh")}
        </Button>
        <Button
          type="primary"
          loading={saving}
          disabled={!draft}
          onClick={() => void save()}
        >
          {t("mediaGeneration.chatgpt.save")}
        </Button>
      </div>
    </Card>
  );
}
