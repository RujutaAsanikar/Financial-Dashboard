import { useEffect, useRef, useState } from 'react';
import { CloudUpload, X, FileSpreadsheet, TriangleAlert, LoaderCircle } from 'lucide-react';
import { uploadStatement } from '../api';
import { cn } from '../lib/utils';

const ACCOUNT_TYPES = ['Checking', 'Savings', 'Credit Card'];

export default function UploadZone({ open, onClose, onUploaded }) {
  const [file, setFile] = useState(null);
  const [accountType, setAccountType] = useState(ACCOUNT_TYPES[0]);
  const [nickname, setNickname] = useState('');
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
    setFile(null);
    setStatus('idle');
    setError(null);
    setDragActive(false);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const pickFile = (f) => {
    if (!f) return;
    if (!/\.csv$/i.test(f.name)) {
      setError('Only .csv files are supported — please export a CSV from your bank.');
      setFile(null);
      return;
    }
    setError(null);
    setFile(f);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragActive(false);
    pickFile(e.dataTransfer.files?.[0]);
  };

  const submit = async () => {
    if (!file) return;
    setStatus('uploading');
    setError(null);
    try {
      const data = await uploadStatement(file, { accountType, nickname });
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
            <p className="text-xs text-muted-foreground">CSV export from your bank. We never send this anywhere but our own backend.</p>
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
            'flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-8 text-center transition-colors',
            dragActive ? 'border-primary bg-secondary' : 'border-border hover:bg-muted/50'
          )}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".csv"
            className="sr-only"
            onChange={(e) => pickFile(e.target.files?.[0])}
          />
          {file ? (
            <>
              <FileSpreadsheet className="size-6 text-primary" />
              <span className="text-sm font-medium">{file.name}</span>
              <span className="text-xs text-muted-foreground">{(file.size / 1024).toFixed(0)} KB · click to replace</span>
            </>
          ) : (
            <>
              <CloudUpload className="size-6 text-muted-foreground" />
              <span className="text-sm font-medium">Drop your CSV here or click to browse</span>
              <span className="text-xs text-muted-foreground">.csv only</span>
            </>
          )}
        </label>

        <div className="mt-4 grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="account-type">
              Account type
            </label>
            <select
              id="account-type"
              value={accountType}
              onChange={(e) => setAccountType(e.target.value)}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {ACCOUNT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div>
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
        </div>

        {error && (
          <div className="mt-3 flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <button
          type="button"
          onClick={submit}
          disabled={!file || status === 'uploading'}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-full bg-primary py-2.5 text-sm font-semibold text-primary-foreground shadow-sm transition-transform hover:brightness-110 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50"
        >
          {status === 'uploading' ? (
            <>
              <LoaderCircle className="size-4 animate-spin" />
              Processing…
            </>
          ) : (
            'Upload and analyze'
          )}
        </button>
      </div>
    </div>
  );
}
