import { useLoaderData, useRevalidator } from 'react-router';

import type { LedgerEntry, Payment, PaymentStatus } from '@/js/types/payments';

import { TopNav } from '@/js/components';

const STATUS_STYLES: Record<PaymentStatus, string> = {
  pending: 'bg-amber-100 text-amber-800 ring-amber-200',
  succeeded: 'bg-emerald-100 text-emerald-800 ring-emerald-200',
  failed: 'bg-rose-100 text-rose-800 ring-rose-200',
};

const formatAmount = (cents: number, currency: string) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(cents / 100);

const formatTime = (iso: string) =>
  new Date(iso).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'medium' });

const StatusBadge = ({ status }: { status: PaymentStatus }) => (
  <span
    className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLES[status]}`}
  >
    {status}
  </span>
);

const LedgerTable = ({ entries }: { entries: LedgerEntry[] }) => (
  <table className="w-full text-xs text-slate-700">
    <thead className="text-left text-slate-500">
      <tr>
        <th className="py-1 pr-3 font-medium">#</th>
        <th className="py-1 pr-3 font-medium">event</th>
        <th className="py-1 pr-3 font-medium">transition</th>
        <th className="py-1 pr-3 font-medium">failure</th>
        <th className="py-1 pr-3 font-medium text-right">Δ cents</th>
        <th className="py-1 font-medium">recorded</th>
      </tr>
    </thead>
    <tbody>
      {entries.map((entry) => (
        <tr key={entry.sequence} className="border-t border-zinc-100">
          <td className="py-1 pr-3 tabular-nums">{entry.sequence}</td>
          <td className="py-1 pr-3 font-mono">{entry.event_type}</td>
          <td className="py-1 pr-3">
            {entry.from_status ?? '∅'} → <strong>{entry.to_status}</strong>
          </td>
          <td className="py-1 pr-3 font-mono">{entry.failure_code ?? '—'}</td>
          <td className="py-1 pr-3 text-right tabular-nums">
            {entry.amount_delta > 0 ? `+${entry.amount_delta}` : entry.amount_delta}
          </td>
          <td className="py-1 tabular-nums">{formatTime(entry.created_at)}</td>
        </tr>
      ))}
    </tbody>
  </table>
);

const PaymentCard = ({ payment }: { payment: Payment }) => (
  <li className="rounded-xl border border-zinc-300 bg-white">
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm text-slate-900">
      <StatusBadge status={payment.status} />
      <span className="font-semibold tabular-nums">
        {formatAmount(payment.amount, payment.currency)}
      </span>
      <span className="text-slate-600">
        {payment.method} · {payment.brand_or_bank_type} ····{payment.last4}
      </span>
      <span className="font-mono text-slate-500">
        {payment.processor_reference ?? 'not submitted'}
      </span>
      {payment.failure_code && (
        <span className="font-mono text-rose-700">{payment.failure_code}</span>
      )}
      <span className="ml-auto text-xs text-slate-500">{formatTime(payment.created_at)}</span>
    </div>
    <details className="border-t border-zinc-200 px-4 py-2">
      <summary className="cursor-pointer text-xs font-medium text-slate-600 select-none">
        Ledger · {payment.ledger.length} {payment.ledger.length === 1 ? 'entry' : 'entries'}
      </summary>
      <div className="pt-2">
        <LedgerTable entries={payment.ledger} />
      </div>
    </details>
  </li>
);

const Payments = () => {
  const payments = useLoaderData<Payment[]>();
  const revalidator = useRevalidator();

  return (
    <>
      <TopNav />
      <section className="mx-auto max-w-4xl px-4">
        <div className="mt-4 mb-3 flex items-center justify-between">
          <div>
            <h1 className="font-semibold text-slate-950">Payments</h1>
            <p className="text-xs text-slate-500">
              Read-only. Status is derived from each payment&apos;s append-only ledger; processor
              references are masked.
            </p>
          </div>
          <button
            className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            disabled={revalidator.state === 'loading'}
            type="button"
            onClick={() => revalidator.revalidate()}
          >
            {revalidator.state === 'loading' ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>

        {payments.length === 0 ? (
          <p className="rounded-xl border border-dashed border-zinc-300 bg-white px-4 py-8 text-center text-sm text-slate-500">
            No payments yet. Tokenize a card at <code>POST /processor/tokenize</code>, then{' '}
            <code>POST /api/payments</code> with the token and an <code>Idempotency-Key</code>.
          </p>
        ) : (
          <ul className="space-y-3">
            {payments.map((payment) => (
              <PaymentCard key={payment.id} payment={payment} />
            ))}
          </ul>
        )}
      </section>
    </>
  );
};

export default Payments;
