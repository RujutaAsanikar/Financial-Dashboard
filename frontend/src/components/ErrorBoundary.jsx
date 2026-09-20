import { Component } from 'react';
import { TriangleAlert } from 'lucide-react';

// Wraps a single card so a render error in one section degrades to a small
// message instead of taking the whole dashboard down.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error, info) {
    console.error('[ErrorBoundary]', this.props.label ?? 'section', error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-[140px] flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border bg-muted/40 p-6 text-center">
          <TriangleAlert className="size-5 text-destructive" />
          <p className="text-sm text-muted-foreground">
            {this.props.label ? `${this.props.label} couldn't load.` : "This section couldn't load."}
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}
