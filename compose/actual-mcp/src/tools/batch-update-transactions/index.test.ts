import { describe, it, expect, vi, beforeEach } from 'vitest';
import { handler, schema } from './index.js';

vi.mock('../../actual-api.js', () => ({
  updateTransaction: vi.fn(),
  getPayees: vi.fn(),
}));

import { updateTransaction, getPayees } from '../../actual-api.js';

const text = (result: Awaited<ReturnType<typeof handler>>): string => (result.content[0] as { text: string }).text;

describe('batch-update-transactions tool', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('exposes a schema with an updates array', () => {
    expect(schema.name).toBe('batch-update-transactions');
    expect(schema.inputSchema.properties).toHaveProperty('updates');
  });

  it('applies every update in order, resolving transfers', async () => {
    vi.mocked(updateTransaction).mockResolvedValue(undefined);
    vi.mocked(getPayees).mockResolvedValue([
      { id: 'payee-savings', name: 'Transfer: Savings', transfer_acct: 'savings-account-id' },
    ]);

    const result = await handler({
      updates: [
        { id: 'txn-1', category: 'cat-groceries' },
        { id: 'txn-2', transfer_account_id: 'savings-account-id' },
      ],
    });

    expect(vi.mocked(updateTransaction).mock.calls).toEqual([
      ['txn-1', { category: 'cat-groceries' }],
      ['txn-2', { payee: 'payee-savings' }],
    ]);
    expect(result.isError).toBeUndefined();
    expect(text(result)).toContain('2/2 succeeded');
  });

  it('reports per-item failures without stopping the batch', async () => {
    vi.mocked(getPayees).mockResolvedValue([]);
    vi.mocked(updateTransaction).mockRejectedValueOnce(new Error('not found')).mockResolvedValue(undefined);

    const result = await handler({
      updates: [
        { id: 'txn-missing', category: 'cat-a' },
        { id: 'txn-empty' },
        { id: 'txn-bad-transfer', transfer_account_id: 'nope' },
        { id: 'txn-ok', notes: 'fine' },
      ],
    });

    const out = text(result);
    expect(out).toContain('1/4 succeeded');
    expect(out).toContain('txn-missing: not found');
    expect(out).toContain('txn-empty: No fields provided');
    expect(out).toContain('txn-bad-transfer: No transfer payee found');
    expect(out).toContain('Successfully updated: txn-ok');
  });

  it('rejects an empty updates array', async () => {
    const result = await handler({ updates: [] });

    expect(result.isError).toBe(true);
    expect(updateTransaction).not.toHaveBeenCalled();
  });
});
