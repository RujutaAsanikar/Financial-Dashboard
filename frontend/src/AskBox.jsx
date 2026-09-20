import { useState } from 'react';
import { Sparkles, Send, LoaderCircle, ChevronRight, TriangleAlert } from 'lucide-react';
import { askQuestion } from './api';
import Card from './components/Card';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from './components/ui/table';

// Must match the backend's SUGGESTED_QUESTIONS (query.py) exactly — these
// are the wording the backend team pre-verified with the model.
const EXAMPLES = [
  'How much did I spend on food last month?',
  'What is my biggest recurring charge?',
  'How much do I spend on weekends?',
  'Compare August to July spending.',
];

export default function AskBox() {
  const [question, setQuestion] = useState('');
  const [status, setStatus] = useState('idle'); // idle | loading | done | error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const ask = async (q) => {
    const text = (q ?? question).trim();
    if (!text) return;
    setQuestion(text);
    setStatus('loading');
    setError(null);
    try {
      const data = await askQuestion(text);
      setResult(data);
      setStatus('done');
    } catch (err) {
      setError(err.message || 'Something went wrong answering that.');
      setStatus('error');
    }
  };

  return (
    <Card
      title="Ask about your money"
      description="Plain-English questions, answered from your own transactions — with the SQL shown so you can check the work."
      className="relative overflow-hidden bg-gradient-to-br from-secondary/60 via-card to-card"
    >
      <div className="pointer-events-none absolute -right-10 -top-10 size-40 rounded-full bg-primary/10 blur-3xl" aria-hidden="true" />

      <form
        onSubmit={(e) => {
          e.preventDefault();
          ask();
        }}
        className="relative flex flex-col gap-2 sm:flex-row"
      >
        <div className="flex flex-1 items-center gap-2 rounded-full border border-border bg-background px-4 py-2.5">
          <Sparkles className="size-4 shrink-0 text-primary" />
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. How much do I spend on weekends?"
            className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <button
          type="submit"
          disabled={status === 'loading' || !question.trim()}
          className="flex items-center justify-center gap-1.5 rounded-full bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground shadow-sm transition-transform hover:brightness-110 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50"
        >
          {status === 'loading' ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
          Ask
        </button>
      </form>

      <div className="relative mt-3 flex flex-wrap gap-2">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            onClick={() => ask(ex)}
            disabled={status === 'loading'}
            className="rounded-full border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
          >
            {ex}
          </button>
        ))}
      </div>

      <div className="relative mt-4 min-h-[1px]">
        {status === 'loading' && (
          <div className="flex items-center gap-2 rounded-xl bg-muted/60 px-4 py-3 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" />
            Thinking through your transactions…
          </div>
        )}

        {status === 'error' && (
          <div className="flex items-start gap-2 rounded-xl bg-destructive/10 px-4 py-3 text-sm text-destructive">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {status === 'done' && result && (
          <div className="animate-in-up rounded-xl border border-border bg-background p-4">
            <p className="text-sm leading-relaxed text-foreground">{result.answer}</p>

            {result.sql && (
              <details className="group mt-3">
                <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
                  <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" />
                  How this was calculated
                </summary>
                <pre className="tabular mt-2 overflow-x-auto rounded-lg bg-muted p-3 text-[11px] leading-relaxed text-foreground">
                  {result.sql}
                </pre>

                {result.rows?.length > 0 && (
                  <div className="mt-2 overflow-x-auto rounded-lg border border-border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          {Object.keys(result.rows[0]).map((k) => (
                            <TableHead key={k} className="tabular text-[11px]">
                              {k}
                            </TableHead>
                          ))}
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {result.rows.map((row, i) => (
                          <TableRow key={i}>
                            {Object.values(row).map((v, j) => (
                              <TableCell key={j} className="tabular text-[11px]">
                                {String(v)}
                              </TableCell>
                            ))}
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </details>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
