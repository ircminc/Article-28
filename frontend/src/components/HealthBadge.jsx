import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, AlertTriangle, AlertCircle } from 'lucide-react';
import { getHealth } from '../services/api.js';

// Polls /api/health every 30s. Surfaces three states:
//   green  — backend up, all reference tables loaded
//   amber  — backend up, one or more reference tables empty (init_db not run)
//   red    — backend unreachable
export default function HealthBadge() {
  const { data, isError } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  if (isError) {
    return (
      <span className="pill-danger">
        <AlertCircle className="w-3 h-3" aria-hidden /> Backend unreachable
      </span>
    );
  }
  if (!data) {
    return <span className="pill-slate">Checking…</span>;
  }

  const tables = data.reference_data || {};
  const allLoaded = Object.values(tables).every((s) => s === 'loaded');

  if (!allLoaded) {
    const missing = Object.entries(tables)
      .filter(([, s]) => s !== 'loaded')
      .map(([t]) => t);
    return (
      <span className="pill-warning" title={`Missing: ${missing.join(', ')}`}>
        <AlertTriangle className="w-3 h-3" aria-hidden /> Reference data incomplete
      </span>
    );
  }

  return (
    <span className="pill-success" title={`Backend v${data.version}`}>
      <CheckCircle2 className="w-3 h-3" aria-hidden /> All systems nominal
    </span>
  );
}
