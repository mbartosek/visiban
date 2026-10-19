import { useId } from "react";
import type { ExtensionPanel, PanelCell } from "./parseExtensionPanels";

/**
 * Renders parsed `extension_panels` below the Summary table (#1770).
 * A panel supplies text and numbers only: every string is a React text node,
 * and no class, color, style or link ever comes from the payload.
 */
function Cell({ value }: { value: PanelCell }) {
  return value === null ? <span className="text-fg-muted">—</span> : <>{String(value)}</>;
}

function Panel({ panel }: { panel: ExtensionPanel }) {
  // Never derive the DOM id from the untrusted panel id.
  const hid = useId();
  return (
    <div role="group" aria-labelledby={hid} className="bg-surface border border-line rounded-lg p-4">
      <h3 id={hid} className="text-sm font-semibold text-fg-secondary break-words">{panel.title}</h3>
      {panel.description && <p className="text-xs text-fg-muted mt-1 break-words">{panel.description}</p>}
      {panel.type === "stats" ? (
        <dl className="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4 break-words">
          {panel.items.map((item, i) => (
            <div key={i}>
              <dt className="text-xs text-fg-muted">{item.label}</dt>
              <dd className="font-mono text-lg text-fg">
                <Cell value={item.value} />
              </dd>
              {item.hint && <dd className="text-xs text-fg-muted">{item.hint}</dd>}
            </div>
          ))}
        </dl>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table aria-labelledby={hid} className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-line text-xs text-fg-muted uppercase tracking-wider">
                {panel.columns.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    className={`pb-2 pr-4 font-medium ${c.align === "right" ? "text-right" : "text-left"}`}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {panel.rows.length === 0 ? (
                <tr>
                  <td colSpan={panel.columns.length} className="py-2 text-fg-muted">No data.</td>
                </tr>
              ) : (
                panel.rows.map((row, ri) => (
                  <tr key={ri} className="border-b border-line">
                    {panel.columns.map((c) => {
                      const v = row[c.key];
                      const tone = typeof v === "number" ? "font-mono text-fg-secondary" : "text-fg-secondary";
                      return (
                        <td
                          key={c.key}
                          className={`py-2 pr-4 break-words ${tone} ${c.align === "right" ? "text-right" : "text-left"}`}
                        >
                          <Cell value={v} />
                        </td>
                      );
                    })}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function ExtensionPanels({ panels }: { panels: ExtensionPanel[] }) {
  if (panels.length === 0) return null;
  return (
    <div data-testid="extension-panels" className="mt-6 flex flex-col gap-4">
      {panels.map((p) => (
        <Panel key={p.id} panel={p} />
      ))}
    </div>
  );
}
