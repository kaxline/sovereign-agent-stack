import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@actual-app/api', () => ({
  init: vi.fn(),
  getBudgets: vi.fn(),
  downloadBudget: vi.fn(),
  sync: vi.fn(),
  shutdown: vi.fn(),
  getAccounts: vi.fn(),
  getAccountBalance: vi.fn(),
  addTransactions: vi.fn(),
}));

import * as api from '@actual-app/api';
import {
  createTransaction,
  getAccounts,
  getAccountBalance,
  getReports,
  initActualApi,
  shutdownActualApi,
} from './actual-api.js';
import { getActualConnection, resetActualConnectionForTests } from './integrations/actual/connection.js';

describe('initActualApi syncing', () => {
  beforeEach(async () => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.sync).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
    vi.useFakeTimers();
  });

  afterEach(async () => {
    vi.useRealTimers();
    await shutdownActualApi();
  });

  it('downloads the budget once and does not sync on the first call', async () => {
    await initActualApi();

    expect(api.downloadBudget).toHaveBeenCalledTimes(1);
    expect(api.sync).not.toHaveBeenCalled();
  });

  it('does not re-download the budget on later calls', async () => {
    await initActualApi();
    vi.advanceTimersByTime(120_000);
    await initActualApi();

    expect(api.downloadBudget).toHaveBeenCalledTimes(1);
  });

  it('syncs once the data is older than the TTL', async () => {
    await initActualApi();

    vi.advanceTimersByTime(30_000);
    await initActualApi();
    expect(api.sync).not.toHaveBeenCalled();

    vi.advanceTimersByTime(31_000);
    await initActualApi();
    expect(api.sync).toHaveBeenCalledTimes(1);
  });

  it('honours ACTUAL_SYNC_TTL_MS', async () => {
    process.env.ACTUAL_SYNC_TTL_MS = '0';
    await initActualApi();

    await initActualApi();
    await initActualApi();
    expect(api.sync).toHaveBeenCalledTimes(2);
  });

  it('never syncs when ACTUAL_SYNC_TTL_MS is negative', async () => {
    process.env.ACTUAL_SYNC_TTL_MS = '-1';
    await initActualApi();

    vi.advanceTimersByTime(600_000);
    await initActualApi();
    expect(api.sync).not.toHaveBeenCalled();
  });

  it('keeps serving when a sync fails', async () => {
    await initActualApi();
    vi.mocked(api.sync).mockRejectedValueOnce(new Error('server unreachable'));

    vi.advanceTimersByTime(61_000);
    await expect(initActualApi()).resolves.toBeUndefined();
  });
});

describe('actual-api wrappers delegate through the connection', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.sync).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
  });

  afterEach(() => resetActualConnectionForTests());

  it('getAccounts ensures init then calls api.getAccounts once', async () => {
    vi.mocked(api.getAccounts).mockResolvedValue([{ id: 'a1', name: 'Checking' }] as never);

    const result = await getAccounts();

    expect(api.init).toHaveBeenCalledTimes(1);
    expect(api.getAccounts).toHaveBeenCalledTimes(1);
    expect(result).toEqual([{ id: 'a1', name: 'Checking' }]);
  });

  it('getAccountBalance passes accountId and cutoff through to api', async () => {
    vi.mocked(api.getAccountBalance).mockResolvedValue(12345 as never);
    const cutoff = new Date('2026-02-28');

    const balance = await getAccountBalance('a1', cutoff);

    expect(api.getAccountBalance).toHaveBeenCalledWith('a1', cutoff);
    expect(balance).toBe(12345);
  });

  it('getReports uses the initialized handle in one queued operation', async () => {
    const send = vi.fn().mockResolvedValue([{ id: 'report-1' }]);
    vi.mocked(api.init).mockResolvedValue({ send } as never);
    const run = vi.spyOn(getActualConnection(), 'run');

    await expect(getReports()).resolves.toEqual([{ id: 'report-1' }]);

    expect(run).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith('report/get', undefined);
  });
});

describe('writes after the session ends', () => {
  const send = vi.fn();

  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.init).mockResolvedValue({ send } as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.sync).mockResolvedValue(undefined as never);
    vi.mocked(api.addTransactions).mockResolvedValue('ok' as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    process.env.ACTUAL_SYNC_TTL_MS = '0';
    vi.useFakeTimers();
  });

  afterEach(async () => {
    vi.useRealTimers();
    delete process.env.ACTUAL_SYNC_TTL_MS;
    await shutdownActualApi();
  });

  const ended = (): Error =>
    Object.assign(new Error('We had an unknown problem opening "budget-1".'), { code: 'token-expired' });

  it('refuses createTransaction before it reaches the local copy', async () => {
    await initActualApi();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(ended());

    await expect(createTransaction('acct-1', { date: '2026-11-01', amount: -100 } as never)).rejects.toThrow(
      'Actual session ended (token-expired)'
    );
    expect(api.addTransactions).not.toHaveBeenCalled();
  });

  it('refuses a write that goes through the internal send path', async () => {
    await initActualApi();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(ended());

    await expect(getReports()).rejects.toThrow('Actual session ended (token-expired)');
    expect(send).not.toHaveBeenCalled();
  });
});
