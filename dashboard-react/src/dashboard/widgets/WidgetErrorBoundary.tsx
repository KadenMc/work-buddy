import { Component, type ErrorInfo, type ReactNode } from "react";

import { WidgetState } from "./WidgetStates";

export interface WidgetErrorBoundaryProps {
  readonly children: ReactNode;
  readonly resetKey: string;
  readonly onError?: (error: Error, info: ErrorInfo) => void;
  readonly onRetry?: () => void;
  readonly fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface WidgetErrorBoundaryState {
  readonly error: Error | null;
}

export class WidgetErrorBoundary extends Component<
  WidgetErrorBoundaryProps,
  WidgetErrorBoundaryState
> {
  state: WidgetErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): WidgetErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error, info);
  }

  componentDidUpdate(previous: WidgetErrorBoundaryProps): void {
    if (
      this.state.error !== null &&
      previous.resetKey !== this.props.resetKey
    ) {
      this.setState({ error: null });
    }
  }

  reset = (): void => {
    this.setState({ error: null });
    this.props.onRetry?.();
  };

  render(): ReactNode {
    if (this.state.error !== null) {
      return this.props.fallback ? (
        this.props.fallback(this.state.error, this.reset)
      ) : (
        <WidgetState state="error" onRetry={this.reset} />
      );
    }
    return this.props.children;
  }
}
