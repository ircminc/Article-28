import { useCallback } from 'react';
import { useDropzone } from 'react-dropzone';
import { UploadCloud, FileText, X } from 'lucide-react';

// A single drop zone for a given file type (835I, 835P, 837). Uses
// react-dropzone; accepts text-like files and the conventional .edi/.835/.837
// extensions. Parent controls the file list so multiple zones can show a
// staged-files view before upload.
export default function FileDropZone({
  label,
  description,
  files,
  onFilesChange,
  accent = 'primary',
  disabled = false,
}) {
  const onDrop = useCallback(
    (accepted) => {
      if (disabled) return;
      const combined = [...files];
      for (const f of accepted) {
        if (!combined.find((x) => x.name === f.name && x.size === f.size)) {
          combined.push(f);
        }
      }
      onFilesChange(combined);
    },
    [files, onFilesChange, disabled]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    disabled,
    multiple: true,
    accept: {
      'text/plain': ['.edi', '.835', '.837', '.txt'],
      'application/octet-stream': ['.edi', '.835', '.837'],
    },
  });

  const accentClasses = {
    primary: 'border-primary/40 bg-primary-50/40',
    brand: 'border-brand-300 bg-brand-50/60',
    success: 'border-success/40 bg-success-50/40',
  };
  const activeClasses = {
    primary: 'border-primary bg-primary-50',
    brand: 'border-brand-600 bg-brand-100',
    success: 'border-success bg-success-50',
  };

  return (
    <div className="space-y-2">
      <div
        {...getRootProps()}
        className={[
          'rounded-lg border-2 border-dashed p-6 text-center transition-colors cursor-pointer',
          isDragActive ? activeClasses[accent] : accentClasses[accent],
          disabled ? 'opacity-60 cursor-not-allowed' : '',
        ].join(' ')}
      >
        <input {...getInputProps()} />
        <UploadCloud className="w-6 h-6 mx-auto text-brand-600" aria-hidden />
        <div className="mt-2 font-semibold text-brand-900 text-sm">{label}</div>
        <div className="mt-0.5 text-xs text-slate-500">{description}</div>
        <div className="mt-2 text-xs text-slate-400">
          Drag and drop, or click to browse. .edi, .835, .837, .txt accepted.
        </div>
      </div>

      {files.length > 0 && (
        <ul className="space-y-1">
          {files.map((f) => (
            <li
              key={`${f.name}-${f.size}`}
              className="flex items-center justify-between text-sm px-3 py-1.5 bg-white border border-slate-200 rounded-md"
            >
              <span className="flex items-center gap-2 text-slate-700 truncate">
                <FileText className="w-4 h-4 text-slate-400 shrink-0" aria-hidden />
                <span className="truncate">{f.name}</span>
                <span className="text-xs text-slate-400">
                  {(f.size / 1024).toFixed(1)} KB
                </span>
              </span>
              <button
                type="button"
                className="text-slate-400 hover:text-danger"
                onClick={() => onFilesChange(files.filter((x) => x !== f))}
                aria-label={`Remove ${f.name}`}
              >
                <X className="w-4 h-4" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
