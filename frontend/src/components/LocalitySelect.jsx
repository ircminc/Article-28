import { useQuery } from '@tanstack/react-query';
import { listCmsLocalities } from '../services/api.js';

// Dropdown of CMS Medicare localities for a given year, grouped by MAC region.
// Users pick by name ("MANHATTAN") instead of memorizing 7-digit MAC-locality
// codes. When `allowProviderDefault` is true (default), an empty value means
// "fall back to the active provider's cms_locality on the backend."
//
// Usage:
//   <LocalitySelect
//     year={2025}
//     value={localityCode}
//     onChange={setLocalityCode}
//     allowProviderDefault
//   />
//
// The list is cached in the CMS engine for 24h per year, and React Query
// caches it client-side too, so switching between calculator invocations
// doesn't re-fetch.
export default function LocalitySelect({
  year,
  value,
  onChange,
  allowProviderDefault = true,
  emptyLabel,   // override the label of the empty option (default: "— Use provider default —")
  disabled = false,
  id,
  className,
  includeLabel = true,
}) {
  const emptyOptionLabel = emptyLabel
    || (allowProviderDefault ? '— Use provider default —' : '— Not set —');
  const { data, isLoading, isError } = useQuery({
    queryKey: ['cms-localities', year],
    queryFn: () => listCmsLocalities(year),
    staleTime: 60 * 60 * 1000,   // 1 hour on the client
    enabled: !!year,
  });

  const localities = data?.localities || [];

  // Group by mac_description for <optgroup>
  const groups = new Map();
  for (const loc of localities) {
    const label = loc.mac_description || '(unknown region)';
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(loc);
  }

  return (
    <div className={className}>
      {includeLabel && (
        <label className="label" htmlFor={id || 'cms-locality-select'}>
          CMS locality
        </label>
      )}
      <select
        id={id || 'cms-locality-select'}
        className="select"
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || isLoading}
      >
        <option value="">{emptyOptionLabel}</option>
        {isLoading && <option disabled>Loading {year} localities…</option>}
        {isError && <option disabled>Failed to load localities</option>}
        {!isLoading && !isError && [...groups.entries()].map(([label, items]) => (
          <optgroup key={label} label={label}>
            {items.map((loc) => (
              <option key={loc.locality} value={loc.locality}>
                {loc.description || loc.locality}
                {loc.mac ? ` (MAC ${loc.mac}\u2009·\u2009${loc.locality})` : ''}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
