export type PaymentStatus = 'pending' | 'succeeded' | 'failed';

export interface LedgerEntry {
  sequence: number;
  event_type: string;
  from_status: PaymentStatus | null;
  to_status: PaymentStatus;
  failure_code: string | null;
  amount_delta: number;
  occurred_at: string;
  created_at: string;
}

export interface Payment {
  id: string;
  status: PaymentStatus;
  failure_code: string | null;
  amount: number;
  currency: string;
  method: string;
  last4: string;
  brand_or_bank_type: string;
  /** Always masked by the API, e.g. "pr_…efcb". */
  processor_reference: string | null;
  created_at: string;
  updated_at: string;
  ledger: LedgerEntry[];
}
