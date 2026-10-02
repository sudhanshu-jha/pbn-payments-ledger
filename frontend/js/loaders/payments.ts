import axios from 'axios';

import type { Payment } from '@/js/types/payments';

export async function paymentsLoader(): Promise<Payment[]> {
  const response = await axios.get<Payment[]>('/api/payments');
  return response.data;
}
