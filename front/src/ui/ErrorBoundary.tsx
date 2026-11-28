import { Component, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { Button } from "./Button";
import { Banner } from "./primitives";

// A render error (an unexpected payload shape, a missing catalog entry) replaces the failing part of
// the screen with a short message and a retry, instead of unmounting the whole app. The boundary
// resets itself when `resetKey` changes (the route's path, the run being shown). React itself
// reports the caught error to the console.

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** What failed, for the message: "The logs", "This page"… */
  what: string;
  resetKey?: unknown;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidUpdate(prev: ErrorBoundaryProps) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <Banner
        tone="failed"
        role="alert"
        title={`${this.props.what} failed to display.`}
        action={
          <Button size="sm" onClick={() => this.setState({ error: null })}>
            Retry
          </Button>
        }
      >
        <span className="font-data text-[12px]">{error.message || error.name}</span>
      </Banner>
    );
  }
}

/** Around the routes: the shell stays usable, and navigating away leaves the crashed page behind. */
export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  return (
    <ErrorBoundary what="This page" resetKey={pathname}>
      {children}
    </ErrorBoundary>
  );
}
