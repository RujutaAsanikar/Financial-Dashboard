import { useEffect, useRef, useState } from 'react';
import { CloudUpload, X, TriangleAlert, LoaderCircle, CreditCard, Plus } from 'lucide-react';
import { uploadStatement, uploadStatements } from '../api';
import { cn } from '../lib/utils';

// The parser's .json output, or a raw statement file for the backend to read
// itself (via Claude). Kept in sync with raw_extraction.SUPPORTED_RAW_TYPES
// on the backend -- notably no .tif/.tiff/.bmp, which Claude's API doesn't
// accept as image input.
const ACCEPTED_EXT = ['.json', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp'];

const hasAcceptedExt = (filename) =>
  ACCEPTED_EXT.some((ext) => filename.toLowerCase().endsWith(ext));

export default function UploadZone({ open, onClose, onUploaded }) {
  const [files, setFiles] = useState([]);
  // Which file (by index into `files`) is the credit card, if any — at most
  // one, since the backend only accepts a single apr value per upload. Kept
  // as an index rather than checkbox state per file so checking one can
  // cleanly uncheck the others (radio behavior, checkbox appearance).
  const [creditIndex, setCreditIndex] = useState(null);
  const [nickname, setNickname] = useState('');
  const [apr, setApr] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const [status, setStatus] = useState('idle'); // idle | uploading | error
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const reset = () => {
    setFiles([]);
    setCreditIndex(null);
    setApr('');
    setStatus('idle');
    setError(null);
    setDragActive(false);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  // Additive on purpose: a native file-picker dialog can only multi-select
  // within the single folder it's currently showing — navigating to a
  // different folder drops any prior selection. So if checking.json and
  // credit.json live in different folders, the only way to get both is two
  // separate picks, each adding to what's already selected rather than
  // replacing it. Same reasoning applies to drag-drop across two drags.
  const pickFiles = (fileList) => {
    const picked = Array.from(fileList || []);
    if (!picked.length) return;
    const bad = picked.find((f) => !hasAcceptedExt(f.name));
    if (bad) {
      setError(
        `${bad.name} isn't a supported file — use a statement PDF/image (${ACCEPTED_EXT.filter((e) => e !== '.json').join(', ')}) or the parser's .json output.`
      );
      return;
    }
    setError(null);
    setFiles((current) => {
      const existingKeys = new Set(current.map((f) => `${f.name}:${f.size}`));
      const additions = picked.filter((f) => !existingKeys.has(`${f.name}:${f.size}`));
      return [...current, ...additions];
    });
    // Without this, picking the exact same filename again later (e.g. after
    // removing it) wouldn't fire onChange at all — the input's own value
    // never actually changed from the browser's point of view.
    if (inputRef.current) inputRef.current.value = '';
  };

  const removeFile = (index) => {
    setFiles((current) => current.filter((_, i) => i !== index));
    setCreditIndex((current) => {
      if (current === index) return null;
      if (current !== null && current > index) return current - 1;
      return current;
    });
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragActive(false);
    pickFiles(e.dataTransfer.files);
  };

  const toggleCredit = (index) => {
    setCreditIndex((current) => (current === index ? null : index));
    setApr('');
  };

  const submit = async () => {
    if (!files.length) return;
    setStatus('uploading');
    setError(null);
    try {
      const data =
        files.length === 1
          ? await uploadStatement(files[0], { nickname, apr: creditIndex === 0 ? apr : '' })
          : await uploadStatements(files, { apr: creditIndex !== null ? apr : '' });
      onUploaded?.(data);
      handleClose();
    } catch (err) {
      setStatus('error');
      setError(err.message || 'Upload failed. Please try again.');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={handleClose} aria-hidden="true" />

      <div className="relative w-full max-w-md animate-in-up rounded-2xl border border-border bg-card p-6 text-card-foreground shadow-xl">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold">Upload statement</h2>
            <p className="text-xs text-muted-foreground">
              A statement PDF/image, or the parser's JSON output. Select checking and credit together for
              accurate transfer detection right away. We never send this anywhere but our own backend.
            </p>
          </div>
          <button
            type="button"
            onClick={handleClose}
            aria-label="Close"
            className="flex size-8 shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X className="size-4" />
          </button>
        </div>

        <label
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={handleDrop}
          className={cn(
            'flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-6 text-center transition-colors',
            dragActive ? 'border-primary bg-secondary' : 'border-border hover:bg-muted/50'
          )}
        >
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED_EXT.join(',')}
            multiple
            className="sr-only"
            onChange={(e) => pickFiles(e.target.files)}
          />
          {files.length ? (
            <>
              <Plus className="size-5 text-primary" />
              <span className="text-sm font-medium">
                {files.length} file{files.length > 1 ? 's' : ''} selected — click or drop to add another
              </span>
              <span className="text-xs text-muted-foreground">
                In a different folder? Add it separately — each pick adds to the list below.
              </span>
            </>
          ) : (
            <>
              <CloudUpload className="size-6 text-muted-foreground" />
              <span className="text-sm font-medium">Drop one or more statements here or click to browse</span>
              <span className="text-xs text-muted-foreground">PDF, image, or the parser's .json</span>
            </>
          )}
        </label>

        {files.length > 0 && (
          <div className="mt-3 flex flex-col gap-2">
            {files.map((f, i) => {
              const isCredit = creditIndex === i;
              return (
                <div key={`${f.name}-${i}`} className="rounded-xl border border-border bg-background p-3">
                  <div className="flex items-start gap-2.5">
                    <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={isCredit}
                        onChange={() => toggleCredit(i)}
                        className="mt-0.5 size-4 shrink-0 accent-primary"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5 truncate text-sm font-medium">
                          {f.name}
                          <span className="shrink-0 text-xs font-normal text-muted-foreground">
                            ({(f.size / 1024).toFixed(0)} KB)
                          </span>
                        </span>
                        <span className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground">
                          <CreditCard className="size-3" />
                          This is a credit card
                        </span>
                      </span>
                    </label>
                    <button
                      type="button"
                      onClick={() => removeFile(i)}
                      aria-label={`Remove ${f.name}`}
                      className="flex size-6 shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <X className="size-3.5" />
                    </button>
                  </div>

                  {isCredit && (
                    <div className="mt-2.5 pl-[26px]">
                      <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor={`apr-${i}`}>
                        APR (%)
                      </label>
                      <input
                        id={`apr-${i}`}
                        type="number"
                        step="0.01"
                        min="0"
                        max="100"
                        inputMode="decimal"
                        placeholder="e.g. 24.99"
                        autoFocus
                        value={apr}
                        onChange={(e) => setApr(e.target.value)}
                        className="w-full rounded-lg border border-border bg-card px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                      <p className="mt-1 text-xs text-muted-foreground">
                        Printed on your statement. Without it there's no payoff chart for this card.
                      </p>
                    </div>
                  )}
                </div>
              );
            })}
            {files.length > 1 && (
              <p className="text-xs text-muted-foreground">
                Only one card's APR applies per upload — check the one that's actually a credit card.
              </p>
            )}
          </div>
        )}

        {files.length === 1 && (
          <div className="mt-3">
            <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="nickname">
              Nickname
            </label>
            <input
              id="nickname"
              type="text"
              placeholder="e.g. Everyday checking"
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        )}

        {error && (
          <div className="mt-3 flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <button
          type="button"
          onClick={submit}
          disabled={!files.length || status === 'uploading'}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-full bg-primary py-2.5 text-sm font-semibold text-primary-foreground shadow-sm transition-transform hover:brightness-110 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50"
        >
          {status === 'uploading' ? (
            <>
              <LoaderCircle className="size-4 animate-spin" />
              Processing…
            </>
          ) : files.length > 1 ? (
            `Upload ${files.length} statements and analyze`
          ) : (
            'Upload and analyze'
          )}
        </button>
      </div>
    </div>
  );
}
