import { describe, it, expect, vi, beforeEach } from 'vitest';
import { handler, schema, formatScheduleAmount, formatScheduleFrequency } from './index.js';

vi.mock('../../../actual-api.js', () => ({
  getSchedules: vi.fn(),
  getAccounts: vi.fn(),
  getPayees: vi.fn(),
}));

import { getSchedules, getAccounts, getPayees } from '../../../actual-api.js';

const text = (result: Awaited<ReturnType<typeof handler>>): string => (result.content[0] as { text: string }).text;

describe('get-schedules tool', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAccounts).mockResolvedValue([{ id: 'acc-1', name: 'Checking', offbudget: false, closed: false }]);
    vi.mocked(getPayees).mockResolvedValue([{ id: 'payee-1', name: 'Netflix' }]);
  });

  it('has the expected name', () => {
    expect(schema.name).toBe('get-schedules');
  });

  it('lists active schedules sorted by next date with resolved names', async () => {
    vi.mocked(getSchedules).mockResolvedValue([
      {
        id: 'sch-2',
        name: 'Rent',
        next_date: '2026-11-01',
        amount: -80000,
        amountOp: 'is',
        date: { frequency: 'monthly', interval: 1, start: '2026-01-01', endMode: 'never' },
      },
      {
        id: 'sch-1',
        name: 'Netflix',
        next_date: '2026-10-15',
        payee: 'payee-1',
        account: 'acc-1',
        amount: -1299,
        amountOp: 'is',
        date: { frequency: 'monthly', interval: 1, start: '2026-01-15', endMode: 'never' },
      },
      { id: 'sch-3', name: 'Old', completed: true, amountOp: 'is', date: '2025-01-01' },
    ] as Awaited<ReturnType<typeof getSchedules>>);

    const out = text(await handler({}));

    expect(out).toContain('Found 2 schedule(s)');
    expect(out).toContain('| Netflix | Netflix | Checking |');
    expect(out.indexOf('sch-1')).toBeLessThan(out.indexOf('sch-2'));
    expect(out).not.toContain('sch-3');
  });

  it('includes completed schedules on request', async () => {
    vi.mocked(getSchedules).mockResolvedValue([
      { id: 'sch-3', name: 'Old', completed: true, amountOp: 'is', date: '2025-01-01' },
    ] as Awaited<ReturnType<typeof getSchedules>>);

    const out = text(await handler({ includeCompleted: true }));

    expect(out).toContain('| Old | N/A | N/A | N/A | once (2025-01-01) |');
    expect(out).toContain('Completed');
  });

  it('returns an error result when the API fails', async () => {
    vi.mocked(getSchedules).mockRejectedValue(new Error('Connection failed'));

    const result = await handler({});

    expect(result.isError).toBe(true);
  });

  it('formats range amounts and multi-interval frequencies', () => {
    expect(formatScheduleAmount({ num1: -1000, num2: -2000 })).toBe('-$10.00 – -$20.00');
    expect(formatScheduleFrequency({ frequency: 'weekly', interval: 2, start: '2026-01-01', endMode: 'never' })).toBe(
      'every 2 weeks'
    );
  });
});
