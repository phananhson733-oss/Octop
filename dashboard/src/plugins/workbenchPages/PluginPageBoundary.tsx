import { Component, type ErrorInfo, type ReactNode } from "react";
import { Result } from "antd";

interface Props {
  title: string;
  children: ReactNode;
}

/** A plugin page that throws must not take the Workbench down with it. */
export class PluginPageBoundary extends Component<
  Props,
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.warn("[plugin-ui] workbench page crashed:", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <Result
          status="warning"
          title={this.props.title}
          subTitle={this.state.error.message}
        />
      );
    }
    return this.props.children;
  }
}
