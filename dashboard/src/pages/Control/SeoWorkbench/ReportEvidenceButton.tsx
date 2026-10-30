import { useState } from "react";
import { Alert, Button, Modal, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type SeoReport,
  type SeoReportBundle,
} from "../../../api/modules/seo";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";

export function ReportEvidenceButton({
  projectId,
  report,
}: {
  projectId: string;
  report: SeoReport;
}) {
  const { t } = useTranslation();
  const zone = useServerTimezone();
  const [bundle, setBundle] = useState<SeoReportBundle>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (!report.context_id) return null;
  return (
    <>
      <Button
        loading={busy}
        onClick={() => {
          setBusy(true);
          setError("");
          void seoApi
            .reportBundle(projectId, report.context_id!)
            .then(setBundle)
            .catch((err) =>
              setError(apiErrorMessage(err, t("seo.loadError"), t)),
            )
            .finally(() => setBusy(false));
        }}
      >
        {t("seo.reportEvidence")}
      </Button>
      {error && <Alert showIcon message={error} type="error" />}
      <Modal
        open={!!bundle}
        title={t("seo.reportEvidence")}
        onCancel={() => setBundle(undefined)}
        footer={null}
        width={800}
      >
        <Typography.Paragraph>
          {t("seo.reportEvidenceHelp")}
        </Typography.Paragraph>
        {bundle &&
          (bundle.saved_keywords == null ? (
            <Typography.Paragraph>
              {t("seo.shortlistNotCaptured")}
            </Typography.Paragraph>
          ) : (
            <details>
              <summary>{t("seo.frozenShortlist")}</summary>
              <pre
                style={{
                  whiteSpace: "pre-wrap",
                  overflowWrap: "anywhere",
                  maxHeight: 400,
                  overflow: "auto",
                }}
              >
                {JSON.stringify(bundle.saved_keywords, null, 2)}
              </pre>
            </details>
          ))}
        {bundle?.sources.map((source, index) => (
          <details key={index}>
            <summary>
              {source.ref.kind} · {source.ref.id ?? t("seo.reportQuery")} ·{" "}
              {formatServerIsoDateTime(source.captured_at, zone)}
            </summary>
            <pre
              style={{
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                maxHeight: 400,
                overflow: "auto",
              }}
            >
              {JSON.stringify(source, null, 2)}
            </pre>
          </details>
        ))}
      </Modal>
    </>
  );
}
