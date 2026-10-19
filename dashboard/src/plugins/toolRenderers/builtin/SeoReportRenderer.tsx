import { Button, Typography } from "antd";
import { FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { ToolRenderProps } from "../types";
import { DefaultToolRenderer } from "./DefaultToolRenderer";

function savedReport(output: string | undefined) {
  if (!output) return null;
  try {
    const data = JSON.parse(output) as Record<string, unknown>;
    if (
      typeof data.report_id !== "string" ||
      typeof data.project_id !== "string"
    )
      return null;
    return {
      reportId: data.report_id,
      projectId: data.project_id,
      title: typeof data.title === "string" ? data.title : "",
    };
  } catch {
    return null;
  }
}

/** ``seo_save_report``: the usual tool details plus a button that opens the saved report. */
export function SeoReportRenderer(props: ToolRenderProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const report = props.status === "done" ? savedReport(props.output) : null;
  return (
    <>
      <DefaultToolRenderer {...props} />
      {report && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginTop: 6,
            minWidth: 0,
          }}
        >
          <Button
            size="small"
            icon={<FileText size={14} aria-hidden />}
            onClick={() =>
              navigate(
                `/workbench/seo-workbench.main?${new URLSearchParams({
                  project: report.projectId,
                  view: "reports",
                  report: report.reportId,
                })}`,
              )
            }
          >
            {t("seo.openReport")}
          </Button>
          <Typography.Text type="secondary" ellipsis>
            {report.title}
          </Typography.Text>
        </div>
      )}
    </>
  );
}
