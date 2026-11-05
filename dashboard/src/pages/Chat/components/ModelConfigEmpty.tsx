import { Button } from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { OctopEmptyMascot } from "../../../components/EmptyState";
import styles from "../index.module.less";

interface ModelConfigEmptyProps {
  canConfigure: boolean;
  /** The model list could not be fetched; do not claim none are configured. */
  loadFailed?: boolean;
}

export default function ModelConfigEmpty({
  canConfigure,
  loadFailed = false,
}: ModelConfigEmptyProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  return (
    <div className={styles.noAgentsEmpty}>
      <div className={styles.noAgentsEmptyInner}>
        <div className={styles.noAgentsEmptyIcon}>
          <OctopEmptyMascot className={styles.noAgentsEmptyMascot} />
        </div>
        <h1 className={styles.noAgentsEmptyTitle}>
          {loadFailed
            ? t("modelConfig.loadFailedTitle")
            : t("modelConfig.promptTitle")}
        </h1>
        <p className={styles.noAgentsEmptyHint}>
          {loadFailed
            ? t("modelConfig.loadFailedMessage")
            : canConfigure
            ? t("modelConfig.promptMessage")
            : t("modelConfig.promptMessageNoPermission")}
        </p>
        {canConfigure && !loadFailed ? (
          <Button
            type="primary"
            size="large"
            onClick={() => navigate("/admin/models")}
          >
            {t("modelConfig.configureButton")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
